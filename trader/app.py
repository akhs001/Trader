"""The Trader app: a local web page with a "Check entries" button.

Usage:
    python -m trader.app            start the app and open it in the browser
    python -m trader.app --no-browser --port 8765
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import smtplib
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import backtest, emailer, market, universe
from .data import (DataError, SetupError, check_setup, fetch_history, fetch_hourly, fetch_many,
                   source_of)
from .main import GMAIL_LOGIN_HELP, ROOT, load_env_file
from .scanner import (StockResult, check_watchlist, email_signals, load_config,
                      load_watchlist, new_signals, scan_history)
from .signals import STRATEGIES
from .state import AlertState

UI_FILE = Path(__file__).with_name("ui.html")
MIN_INTERVAL, MAX_INTERVAL = 1, 15  # minutes between automatic checks
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-:/]{0,14}$")


def code_fingerprint() -> str:
    """Identifies the program files on disk, to notice an update."""
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        try:
            digest.update(path.read_bytes())
        except OSError:
            pass  # a file being replaced right now
    return digest.hexdigest()


RUNNING_CODE = code_fingerprint()  # the version this process loaded


class App:
    """Everything the page can show or do. One instance per running app."""

    def __init__(self, watchlist: Path, config: Path, state_dir: Path):
        self.watchlist_path = watchlist
        self.config_path = config
        self.state_path = state_dir / "alerts.json"
        self.settings_path = state_dir / "app.json"
        self.lock = threading.Lock()
        self.scan = {"running": False, "results": [], "total": 0, "current": "",
                     "message": "", "finished": "", "error": "", "email": "",
                     "strategy": "all", "direction": "both", "timeframe": "1d"}
        settings = self._load_settings()
        self.auto = bool(settings.get("auto"))
        self.interval = settings.get("interval")  # minutes; None = the default from the settings file
        self.last_auto = 0.0
        # Which signals checks look for: one strategy or all, long, short or both.
        self.strategy, self.direction, self.hourly = "all", "both", False
        self.set_selection(settings.get("strategy"), settings.get("direction"),
                           settings.get("timeframe"), save=False)
        self.backtest = {"running": False, "done": 0, "total": 0, "current": "", "message": "",
                         "error": "", "run": 0}
        self.backtest_result: dict | None = None
        self.screen = {"running": False, "done": 0, "total": 0, "message": "", "error": "", "run": 0}
        self.screen_result: dict | None = None

    # --- what the page shows -------------------------------------------------

    def snapshot(self) -> dict:
        now = market.new_york_time()
        with self.lock:
            scan = {**self.scan, "results": list(self.scan["results"])}
        return {
            "watchlist": load_watchlist(self.watchlist_path),
            "scan": scan,
            "auto": {"enabled": self.auto, "interval": self._interval(),
                     "min": MIN_INTERVAL, "max": MAX_INTERVAL},
            "strategies": [{"id": key, "name": name} for key, name in STRATEGIES.items()],
            "selection": {"strategy": self.strategy, "direction": self.direction,
                          "timeframe": "1h" if self.hourly else "1d"},
            "backtest": dict(self.backtest),
            "screen": dict(self.screen),
            "universes": [{"id": key, "name": name} for key, (name, _) in universe.UNIVERSES.items()],
            "market": {"open": market.is_scan_time(), "time": now.strftime("%a %H:%M")},
            "setup": self._setup(),
            # True once update.bat has replaced the program files: the page is
            # then newer than this running program, which must be restarted.
            "stale": code_fingerprint() != RUNNING_CODE,
        }

    def _setup(self) -> dict:
        setup = {"data": True, "data_problem": "", "email": True, "email_to": ""}
        try:
            check_setup(source_of(self._config()))
        except SetupError as problem:
            setup["data"] = False
            setup["data_problem"] = str(problem)
        try:
            setup["email_to"] = emailer.credentials()[2]
        except emailer.EmailConfigError:
            setup["email"] = False
        return setup

    def _config(self) -> dict:
        return load_config(self.config_path)

    def _interval(self) -> int:
        """Minutes between automatic checks, between 1 and 15."""
        try:
            minutes = int(self.interval or self._config().get("schedule", {}).get("interval_minutes", 15))
        except (TypeError, ValueError):
            minutes = 15
        return min(max(minutes, MIN_INTERVAL), MAX_INTERVAL)

    # --- checking ------------------------------------------------------------

    def start_check(self, send_email: bool) -> bool:
        """Start checking the watchlist in the background. False if one is running."""
        symbols = load_watchlist(self.watchlist_path)
        with self.lock:
            if self.scan["running"]:
                return False
            self.scan.update(running=True, results=[], total=len(symbols), current="",
                             message="", error="", email="",
                             strategy=self.strategy, direction=self.direction,
                             timeframe="1h" if self.hourly else "1d")
        threading.Thread(target=self._check, args=(symbols, send_email), daemon=True).start()
        return True

    def _check(self, symbols: list[str], send_email: bool) -> None:
        error = email = ""
        try:
            config = self._config()
            state = AlertState(self.state_path)
            results = check_watchlist(
                symbols, config,
                on_start=lambda symbol, _: self._update(current=symbol, message=""),
                on_result=lambda result: self._add_result(result, state, config),
                notify=lambda text: self._update(message=text),
                strategy=self.scan["strategy"], direction=self.scan["direction"],
                hourly=self.scan["timeframe"] == "1h",
            )
            fresh = new_signals(results, state, config)
            if send_email and fresh:
                try:
                    recipient = email_signals(fresh, state)
                    email = f"Emailed {_count(emailer.by_entry(fresh), 'new entry', 'new entries')} to {recipient}."
                except emailer.EmailConfigError as problem:
                    email = str(problem)
                except smtplib.SMTPAuthenticationError:
                    email = GMAIL_LOGIN_HELP
                except (smtplib.SMTPException, OSError) as problem:
                    email = f"The email could not be sent: {problem}"
        except SetupError as problem:
            error = str(problem)
        except Exception as problem:  # show the problem on the page instead of dying silently
            error = f"The check failed: {problem}"
        self._update(running=False, current="", message="", error=error, email=email,
                     finished=datetime.now().strftime("%H:%M:%S"))

    def _update(self, **changes) -> None:
        with self.lock:
            self.scan.update(changes)

    def _add_result(self, result: StockResult, state: AlertState, config: dict) -> None:
        row = _row(result, {id(s) for s in new_signals([result], state, config)})
        with self.lock:
            self.scan["results"].append(row)

    # --- automatic checking --------------------------------------------------

    def set_auto(self, enabled: bool, interval=None) -> None:
        self.auto = enabled
        try:
            self.interval = min(max(int(interval), MIN_INTERVAL), MAX_INTERVAL)
        except (TypeError, ValueError):
            pass  # no usable number sent: keep the current interval
        self._save_settings()

    def set_selection(self, strategy, direction, timeframe=None, save: bool = True) -> None:
        """Choose which signals checks look for, and on which candles.

        Unknown values are ignored.
        """
        if strategy == "all" or strategy in STRATEGIES:
            self.strategy = strategy
        if direction in ("both", "long", "short"):
            self.direction = direction
        if timeframe in ("1d", "1h"):
            self.hourly = timeframe == "1h"
        if save:
            self._save_settings()

    def _save_settings(self) -> None:
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps({
            "auto": self.auto, "interval": self.interval,
            "strategy": self.strategy, "direction": self.direction,
            "timeframe": "1h" if self.hourly else "1d",
        }))

    # --- scanning a whole market ---------------------------------------------

    def start_screen(self, body: dict) -> str:
        """Start scanning a list of stocks in the background. Returns an error or ''."""
        market_id = body.get("universe")
        if market_id not in universe.UNIVERSES:
            return "Choose which stocks to scan."
        strategy = body.get("strategy") if body.get("strategy") in STRATEGIES else "all"
        direction = body.get("direction") if body.get("direction") in ("long", "short") else "both"
        with self.lock:
            if self.screen["running"]:
                return "A scan is already running."
            self.screen.update(running=True, done=0, total=0, error="",
                               message="Loading the list of stocks")
        threading.Thread(target=self._screen, args=(market_id, strategy, direction),
                         daemon=True).start()
        return ""

    def _screen(self, market_id: str, strategy: str, direction: str) -> None:
        error = ""
        try:
            config = self._config()
            symbols = universe.load(market_id)
            self._screen_progress(total=len(symbols), message="")
            hits, failed = [], 0
            for done, (symbol, history) in enumerate(fetch_many(symbols, source_of(config)), start=1):
                if isinstance(history, DataError):
                    failed += 1
                else:
                    result = scan_history(history, config, strategy, direction)
                    if result.signals:
                        hits.append(_row(result, set()))
                self._screen_progress(done=done)
            self.screen_result = {
                "universe": market_id, "name": universe.UNIVERSES[market_id][0],
                "strategy": strategy, "direction": direction, "hits": hits,
                "scanned": len(symbols) - failed, "failed": failed,
                "finished": datetime.now().strftime("%H:%M:%S"),
            }
        except (SetupError, DataError) as problem:
            error = str(problem)
        except Exception as problem:  # show the problem on the page instead of dying silently
            error = f"The scan failed: {problem}"
        with self.lock:
            self.screen.update(running=False, message="", error=error, run=self.screen["run"] + 1)

    def _screen_progress(self, **changes) -> None:
        with self.lock:
            self.screen.update(changes)

    # --- backtesting ---------------------------------------------------------

    def start_backtest(self, body: dict) -> str:
        """Start a backtest of one stock in the background. Returns an error message or ''."""
        symbol = str(body.get("symbol") or "").strip().upper()
        if not SYMBOL.match(symbol):
            return "Type the ticker to test, for example NVDA."
        in_hours = body.get("hold_unit") == "hours"
        hourly_signals = body.get("timeframe") == "1h"
        options = backtest.Options(
            strategy=body.get("strategy") if body.get("strategy") in STRATEGIES else "all",
            direction=body.get("direction") if body.get("direction") in ("long", "short") else "both",
            # Hourly prices only reach back about two years.
            years=_number(body.get("years"), 2, 1, 2 if in_hours or hourly_signals else 5),
            target=_number(body.get("target"), 1, 1, 2),
            hold=_number(body.get("hold"), 30, 1, 250),
            hold_unit="hours" if in_hours else "days",
            budget=_decimal(body.get("budget"), 10_000.0, 1.0, 1e12),
            risk_pct=_decimal(body.get("risk_pct"), 1.0, 0.01, 100.0),
        )
        with self.lock:
            if self.backtest["running"]:
                return "A backtest is already running."
            self.backtest.update(running=True, done=0, total=1, current=symbol,
                                 message="", error="")
        threading.Thread(target=self._backtest, args=(symbol, options, hourly_signals),
                         daemon=True).start()
        return ""

    def _backtest(self, symbol: str, options: backtest.Options, hourly_signals: bool) -> None:
        error = ""
        try:
            config = self._config()
            source = source_of(config)
            notify = lambda text: self._progress(message=text)
            hourly = None
            if hourly_signals:
                # Signals and exits both come from hourly candles.
                history = fetch_hourly(symbol, source=source, notify=notify)
            else:
                history = fetch_history(symbol, source=source, years=options.years, notify=notify)
                if options.hold_unit == "hours":
                    self._progress(message=f"Downloading hourly prices for {symbol}")
                    hourly = fetch_hourly(symbol, source=source, notify=notify)
            self._progress(message=f"Testing {symbol}")
            trades = backtest.backtest_stock(history, config, options, hourly)
            result = backtest.report(trades, options)
            per_day = backtest.HOURS_PER_DAY if hourly_signals else 1
            wanted_from = history.dates[max(0, len(history.dates) - int(options.years * 252 * per_day))][:10]
            # The test cannot start before the prices do.
            covered_from = max(wanted_from, hourly.dates[0][:10]) if hourly else wanted_from
            result.update(options=vars(options), symbol=symbol, tested_from=covered_from,
                          timeframe="1h" if hourly_signals else "1d",
                          tested_to=history.dates[-1][:10], candles=len(history.dates),
                          finished=datetime.now().strftime("%H:%M:%S"))
            self.backtest_result = result
        except (SetupError, DataError) as problem:
            error = str(problem)
        except Exception as problem:  # show the problem on the page instead of dying silently
            error = f"The backtest failed: {problem}"
        with self.lock:
            self.backtest.update(running=False, current="", message="", error=error,
                                 done=1, run=self.backtest["run"] + 1)

    def _progress(self, **changes) -> None:
        with self.lock:
            self.backtest.update(changes)

    def run_auto(self) -> None:
        """Background loop: while switched on, check on a timer during market hours."""
        while True:
            time.sleep(5)
            try:
                interval = self._interval()
                due = time.monotonic() - self.last_auto >= interval * 60
                if self.auto and due and market.is_scan_time(grace_minutes=interval):
                    if self.start_check(send_email=True):
                        self.last_auto = time.monotonic()
            except Exception as problem:
                print(f"Automatic check skipped: {problem}")

    def _load_settings(self) -> dict:
        try:
            return dict(json.loads(self.settings_path.read_text()))
        except (OSError, ValueError, TypeError):
            return {}

    # --- watchlist -----------------------------------------------------------

    def add_symbol(self, raw: str) -> str:
        """Add a ticker. Returns an error message, or '' on success."""
        symbol = str(raw).strip().upper()
        if not SYMBOL.match(symbol):
            return "That does not look like a ticker symbol."
        symbols = load_watchlist(self.watchlist_path)
        if symbol in symbols:
            return f"{symbol} is already on the watchlist."
        self._save_watchlist(symbols + [symbol])
        return ""

    def remove_symbol(self, raw: str) -> None:
        symbol = str(raw).strip().upper()
        self._save_watchlist([s for s in load_watchlist(self.watchlist_path) if s != symbol])
        with self.lock:
            self.scan["results"] = [r for r in self.scan["results"] if r["symbol"] != symbol]

    def _save_watchlist(self, symbols: list[str]) -> None:
        header = "# Your watchlist: one US ticker per line.\n"
        self.watchlist_path.write_text(header + "".join(f"{s}\n" for s in symbols))

    def send_test_email(self) -> str:
        """Returns a message describing what happened."""
        try:
            sender, password, recipient = emailer.credentials()
            emailer.send(emailer.test_message(sender, recipient), sender, password)
        except emailer.EmailConfigError as problem:
            return str(problem)
        except smtplib.SMTPAuthenticationError:
            return GMAIL_LOGIN_HELP
        except (smtplib.SMTPException, OSError) as problem:
            return f"The email could not be sent: {problem}"
        return f"Test email sent to {recipient}."


def _number(value, default: int, low: int, high: int) -> int:
    try:
        return min(max(int(value), low), high)
    except (TypeError, ValueError):
        return default


def _decimal(value, default: float, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return min(max(number, low), high) if number == number else default


def _count(items: list, one: str, many: str) -> str:
    return f"{len(items)} {one if len(items) == 1 else many}"


def _row(result: StockResult, new_ids: set[int]) -> dict:
    """One stock, in the shape the page displays."""
    row = {
        "symbol": result.symbol, "price": result.price, "change_pct": result.change_pct,
        "date": result.date, "facts": result.facts, "note": result.note,
        "error": result.error, "entries": [],
    }
    # One entry per direction; a stock can show a long and a short at once.
    for group in emailer.by_entry(result.signals):
        first = group[0]
        entry = {
            "direction": first.direction, "plan": None,
            "signals": [{"title": s.title, "reason": s.reason, "new": id(s) in new_ids}
                        for s in group],
        }
        if first.plan:
            entry["plan"] = {
                "levels": [{"label": label, "price": price, "pct": pct}
                           for label, price, pct in emailer.plan_rows(first)],
                "note": emailer.plan_note(first, len(group) > 1),
            }
        row["entries"].append(entry)
    return row


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if not self._local():
                return
            if self.path in ("/", "/index.html"):
                self._send(200, UI_FILE.read_bytes(), "text/html; charset=utf-8")
            elif self.path == "/api/state":
                self._json(app.snapshot())
            elif self.path == "/api/backtest":
                self._json({"result": app.backtest_result})
            elif self.path == "/api/screen":
                self._json({"result": app.screen_result})
            else:
                self._json({"error": "Not found"}, 404)

        def do_POST(self):
            # The custom header cannot be sent by another website's page, so
            # only this app's own page can press its buttons.
            if not self._local() or self.headers.get("X-Trader") != "1":
                if self._local():
                    self._json({"error": "Forbidden"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                return self._json({"error": "Bad request"}, 400)

            if self.path == "/api/check":
                started = app.start_check(send_email=bool(body.get("email")))
                self._json({"error": "" if started else "A check is already running."})
            elif self.path == "/api/watchlist/add":
                self._json({"error": app.add_symbol(body.get("symbol", ""))})
            elif self.path == "/api/watchlist/remove":
                app.remove_symbol(body.get("symbol", ""))
                self._json({"error": ""})
            elif self.path == "/api/auto":
                app.set_auto(bool(body.get("enabled")), body.get("interval"))
                self._json({"error": ""})
            elif self.path == "/api/selection":
                app.set_selection(body.get("strategy"), body.get("direction"), body.get("timeframe"))
                self._json({"error": ""})
            elif self.path == "/api/backtest":
                self._json({"error": app.start_backtest(body)})
            elif self.path == "/api/screen":
                self._json({"error": app.start_screen(body)})
            elif self.path == "/api/test-email":
                self._json({"message": app.send_test_email()})
            elif self.path == "/api/quit":
                # Sent by a newer copy that is starting up and needs the port.
                self._json({"error": ""})
                threading.Thread(target=self.server.shutdown, daemon=True).start()
            else:
                self._json({"error": "Not found"}, 404)

        def _local(self) -> bool:
            """Refuse requests that reach this server under another site's name."""
            host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
            if host in ("127.0.0.1", "localhost"):
                return True
            self._json({"error": "Forbidden"}, 403)
            return False

        def _json(self, payload: dict, status: int = 200) -> None:
            self._send(status, json.dumps(payload).encode(), "application/json")

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args) -> None:
            pass  # keep the console quiet

    return Handler


def serve(app: App, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app))
    threading.Thread(target=app.run_auto, daemon=True).start()
    return server


def stop_running_copy(url: str) -> bool:
    """Ask the Trader already running at `url` to stop. False if it cannot."""
    request = urllib.request.Request(
        url + "/api/quit", data=b"{}",
        headers={"Content-Type": "application/json", "X-Trader": "1"},
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(request, timeout=5) as response:
            return "error" in json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError):
        return False  # not Trader, or a version from before it could be asked


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    load_env_file(ROOT / ".env")

    url = f"http://127.0.0.1:{args.port}"
    app = App(ROOT / "watchlist.txt", ROOT / "config.toml", ROOT / ".state")
    try:
        server = serve(app, args.port)
    except OSError:
        # Something already uses the port, normally a copy of Trader started
        # earlier (perhaps before an update). Take over from it.
        server = None
        if stop_running_copy(url):
            for _ in range(50):
                try:
                    server = serve(app, args.port)
                    break
                except OSError:
                    time.sleep(0.1)
        if server is None:
            print("An older copy of Trader is still running and has to be closed first.")
            print("Close the other Trader window, then start Trader again.")
            return 1
        print("Replaced the copy of Trader that was already running.")

    print(f"Trader is running at {url}")
    print("Keep this window open while you use it. Close it (or press Ctrl+C) to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
