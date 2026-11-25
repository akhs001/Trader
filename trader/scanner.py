"""Checks the watchlist and emails new signals. Shared by the console and the app."""

from __future__ import annotations

import time
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import emailer, signals
from .data import DataError, fetch_history, fetch_hourly, source_of
from .signals import MIN_BARS, Signal


def wanted(signal: Signal, strategy: str, direction: str) -> bool:
    """Whether a signal belongs to the chosen strategy and direction."""
    return (strategy in ("all", signal.strategy)
            and direction in ("both", signal.direction))
from .state import AlertState


@dataclass
class StockResult:
    symbol: str
    price: float | None = None
    change_pct: float | None = None
    date: str = ""
    facts: dict[str, str] = field(default_factory=dict)
    signals: list[Signal] = field(default_factory=list)
    note: str = ""  # why the stock was not analysed, if it was not
    error: str = ""  # why its prices could not be read, if they could not


def fetch_benchmark(config: dict, notify=print, years: float = 1):
    """The market's daily history for the market filter, or None if it is off or unavailable."""
    market = config.get("market", {})
    if not market.get("filter", True):
        return None
    try:
        return fetch_history(str(market.get("symbol", "SPY")), source=source_of(config),
                             notify=notify, years=years)
    except DataError as error:
        notify(f"The market filter is off for this check: {error}")
        return None


def check_stock(symbol: str, config: dict, notify=print, strategy: str = "all",
                direction: str = "both", hourly: bool = False, benchmark=None) -> StockResult:
    """Download one stock and look for entry signals. Raises SetupError.

    `strategy` (a key of signals.STRATEGIES) and `direction` ("long" or
    "short") narrow the result to the signals the user asked for.
    """
    try:
        if hourly:
            # About 60 days of hours: enough for the 200-hour average.
            history = fetch_hourly(symbol, source=source_of(config), notify=notify, days=60)
        else:
            history = fetch_history(symbol, source=source_of(config), notify=notify)
    except DataError as error:
        return StockResult(symbol, error=str(error).removeprefix(f"{symbol}: "))
    return scan_history(history, config, strategy, direction, benchmark)


def scan_history(history, config: dict, strategy: str = "all",
                 direction: str = "both", benchmark=None) -> StockResult:
    """Look for entry signals in prices that are already downloaded.

    "all" runs the strategies switched on in the settings; a strategy's name
    runs that one, switched on or not.
    """
    only = None if strategy == "all" else strategy
    found = signals.scan(history, config, benchmark, only=only)
    result = StockResult(
        history.symbol,
        price=history.closes[-1],
        date=history.dates[-1],
        facts=signals.facts(history),
        signals=[s for s in found if wanted(s, strategy, direction)],
    )
    if len(history.closes) >= 2:
        result.change_pct = signals.day_change(history)
    if len(history.closes) < MIN_BARS:
        unit = "hours" if history.hourly else "days"
        result.note = f"only {len(history.closes)} {unit} of history"
    return result


def check_watchlist(symbols: list[str], config: dict, on_start=None, on_result=None,
                    notify=print, strategy: str = "all", direction: str = "both",
                    hourly: bool = False, benchmark=None) -> list[StockResult]:
    results = []
    for index, symbol in enumerate(symbols):
        if on_start:
            on_start(symbol, index)
        if index:
            time.sleep(0.3)  # stay polite to the data provider
        results.append(check_stock(symbol, config, notify, strategy, direction, hourly, benchmark))
        if on_result:
            on_result(results[-1])
    return results


def new_signals(results: list[StockResult], state: AlertState, config: dict) -> list[Signal]:
    """Signals that have not been emailed within the cooldown period."""
    cooldown = int(config.get("alerts", {}).get("cooldown_days", 5))
    hours = int(config.get("alerts", {}).get("cooldown_hours", 24))
    return [s for r in results for s in r.signals if state.is_new(s, cooldown, hours)]


def email_signals(fresh: list[Signal], state: AlertState) -> str:
    """Email the signals and remember them. Returns the recipient address."""
    recipient = emailer.send_alerts(fresh)
    # Recorded only after a successful send, so a failed send is retried.
    state.record(fresh)
    state.save()
    return recipient


def load_config(path: Path, local: Path | None = None) -> dict:
    """Settings from config.toml, with config.local.toml (if any) laid on top.

    The local file holds only the settings the user changed, so updates can
    replace config.toml without losing them.
    """
    config = tomllib.loads(path.read_text())
    local = local or path.with_name("config.local.toml")
    if local.exists():
        _merge(config, tomllib.loads(local.read_text()))
    return config


def _merge(base: dict, changes: dict) -> None:
    for key, value in changes.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], value)
        else:
            base[key] = value


def load_watchlist(path: Path) -> list[str]:
    symbols = []
    if not path.exists():
        return symbols
    for line in path.read_text().splitlines():
        symbol = line.split("#", 1)[0].strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols
