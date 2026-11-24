"""The Trader app: a local web page with a "Check entries" button.

Usage:
    python -m trader.app            start the app and open it in the browser
    python -m trader.app --no-browser --port 8765
"""

from __future__ import annotations

import argparse
import json
import re
import smtplib
import threading
import time
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import emailer, market
from .data import SetupError, check_setup, source_of
from .main import GMAIL_LOGIN_HELP, ROOT, load_env_file
from .scanner import (StockResult, check_watchlist, email_signals, load_config,
                      load_watchlist, new_signals)
from .state import AlertState

UI_FILE = Path(__file__).with_name("ui.html")
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.\-:/]{0,14}$")


class App:
    """Everything the page can show or do. One instance per running app."""

    def __init__(self, watchlist: Path, config: Path, state_dir: Path):
        self.watchlist_path = watchlist
        self.config_path = config
        self.state_path = state_dir / "alerts.json"
        self.settings_path = state_dir / "app.json"
        self.lock = threading.Lock()
        self.scan = {"running": False, "results": [], "total": 0, "current": "",
                     "message": "", "finished": "", "error": "", "email": ""}
        self.auto = bool(self._load_settings().get("auto"))
        self.last_auto = 0.0

    # --- what the page shows -------------------------------------------------

    def snapshot(self) -> dict:
        now = market.new_york_time()
        with self.lock:
            scan = {**self.scan, "results": list(self.scan["results"])}
        return {
            "watchlist": load_watchlist(self.watchlist_path),
            "scan": scan,
            "auto": {"enabled": self.auto, "interval": self._interval()},
            "market": {"open": market.is_scan_time(), "time": now.strftime("%a %H:%M")},
            "setup": self._setup(),
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
        return max(int(self._config().get("schedule", {}).get("interval_minutes", 15)), 1)

    # --- checking ------------------------------------------------------------

    def start_check(self, send_email: bool) -> bool:
        """Start checking the watchlist in the background. False if one is running."""
        symbols = load_watchlist(self.watchlist_path)
        with self.lock:
            if self.scan["running"]:
                return False
            self.scan.update(running=True, results=[], total=len(symbols), current="",
                             message="", error="", email="")
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
            )
            fresh = new_signals(results, state, config)
            if send_email and fresh:
                try:
                    recipient = email_signals(fresh, state)
                    email = f"Emailed {_count(fresh, 'new entry', 'new entries')} to {recipient}."
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
                     finished=datetime.now().strftime("%H:%M"))

    def _update(self, **changes) -> None:
        with self.lock:
            self.scan.update(changes)

    def _add_result(self, result: StockResult, state: AlertState, config: dict) -> None:
        row = _row(result, {id(s) for s in new_signals([result], state, config)})
        with self.lock:
            self.scan["results"].append(row)

    # --- automatic checking --------------------------------------------------

    def set_auto(self, enabled: bool) -> None:
        self.auto = enabled
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        self.settings_path.write_text(json.dumps({"auto": enabled}))

    def run_auto(self) -> None:
        """Background loop: while switched on, check on a timer during market hours."""
        while True:
            time.sleep(20)
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


def _count(items: list, one: str, many: str) -> str:
    return f"{len(items)} {one if len(items) == 1 else many}"


def _row(result: StockResult, new_ids: set[int]) -> dict:
    """One stock, in the shape the page displays."""
    row = {
        "symbol": result.symbol, "price": result.price, "change_pct": result.change_pct,
        "date": result.date, "facts": result.facts, "note": result.note,
        "error": result.error, "signals": [], "plan": None,
    }
    for signal in result.signals:
        row["signals"].append({"title": signal.title, "reason": signal.reason,
                               "new": id(signal) in new_ids})
    first = result.signals[0] if result.signals else None
    if first and first.plan:
        row["plan"] = {
            "levels": [{"label": label, "price": price, "pct": pct}
                       for label, price, pct in emailer.plan_rows(first)],
            "note": emailer.plan_note(first, len(result.signals) > 1),
        }
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
                app.set_auto(bool(body.get("enabled")))
                self._json({"error": ""})
            elif self.path == "/api/test-email":
                self._json({"message": app.send_test_email()})
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
        print(f"Trader seems to be running already. Opening {url}")
        if not args.no_browser:
            webbrowser.open(url)
        return 0

    print(f"Trader is running at {url}")
    print("Keep this window open while you use it. Close it (or press Ctrl+C) to stop.")
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
