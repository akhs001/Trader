"""The momentum portfolio: each month, hold the strongest stocks of the past year.

A list of stocks (an index) is ranked by its return over the last 12 months;
the top few are held in equal amounts until the next monthly ranking. Trader
remembers the portfolio you saved, tells you what to buy, keep and sell at the
next ranking, and keeps a record of how each period did against the market.

Returns here are price changes only; dividends are left out, for the portfolio
and for the market alike.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

from .data import History

YEAR = 252  # trading days
HALF_YEAR = 126
MONTH = 21


@dataclass
class Ranked:
    symbol: str
    price: float
    year_pct: float  # return over the last 12 months: what the ranking uses
    half_pct: float  # the last 6 months, for information
    month_pct: float  # the last month, for information


def measure(history: History) -> Ranked | None:
    """A stock's momentum figures, or None without a full year of prices."""
    closes = history.closes
    if len(closes) <= YEAR or closes[-1 - YEAR] <= 0:
        return None
    change = lambda days: (closes[-1] / closes[-1 - days] - 1) * 100
    return Ranked(history.symbol, closes[-1], change(YEAR), change(HALF_YEAR), change(MONTH))


def rank(histories: dict[str, History]) -> list[Ranked]:
    """Every stock with a year of prices, strongest 12-month return first."""
    measured = [m for m in (measure(h) for h in histories.values()) if m is not None]
    return sorted(measured, key=lambda m: m.year_pct, reverse=True)


class Portfolio:
    """The saved momentum portfolio and its record, kept in a small JSON file.

    Each record is one holding period: the day it started, what was bought at
    which price, the market's price that day and, once the next period has
    started, the prices everything was sold at.
    """

    def __init__(self, path: Path):
        self.path = path
        try:
            self.records: list[dict] = list(json.loads(path.read_text())["records"])
        except (OSError, ValueError, KeyError, TypeError):
            self.records = []

    def current(self) -> dict | None:
        return self.records[-1] if self.records else None

    def holdings(self) -> dict[str, float]:
        """Symbol -> price paid, for the portfolio held now."""
        record = self.current()
        return {} if record is None else dict(record["holdings"])

    def save(self, day: str, universe: str, holdings: dict[str, float], market_price: float,
             prices_now: dict[str, float]) -> None:
        """Start a new holding period, closing the one before it at today's prices."""
        previous = self.current()
        if previous is not None and previous["date"] == day:
            self.records.pop()  # saved twice in a day: the later list replaces the earlier
            previous = self.current()
        if previous is not None:
            previous["sold"] = {symbol: prices_now[symbol] for symbol in previous["holdings"]
                                if symbol in prices_now}
            previous["end"] = day
            previous["market_end"] = market_price
        self.records.append({"date": day, "universe": universe, "holdings": dict(holdings),
                             "market": market_price})
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"records": self.records}, indent=1))

    def track(self, prices_now: dict[str, float], market_now: float, today: str) -> dict:
        """How every holding period did against the market, and the totals.

        The last period is still running and is valued at today's prices.
        """
        periods = []
        total = market_total = 1.0
        for record in self.records:
            running = "sold" not in record
            exits = prices_now if running else record["sold"]
            changes = [exits[symbol] / paid - 1 for symbol, paid in record["holdings"].items()
                       if symbol in exits and paid > 0]
            if not changes:
                continue
            result = sum(changes) / len(changes)  # equal amounts in every stock
            market_exit = market_now if running else record["market_end"]
            market_result = market_exit / record["market"] - 1
            total *= 1 + result
            market_total *= 1 + market_result
            periods.append({
                "start": record["date"], "end": today if running else record["end"],
                "running": running, "stocks": len(record["holdings"]),
                "missing": len(record["holdings"]) - len(changes),
                "portfolio_pct": result * 100, "market_pct": market_result * 100,
            })
        return {"periods": periods, "portfolio_pct": (total - 1) * 100,
                "market_pct": (market_total - 1) * 100}


def plan(ranked: list[Ranked], top: int, held: dict[str, float]) -> dict:
    """What this month's list means for the saved portfolio: buy, keep, sell.

    Returns the top list (each marked "buy" or "keep") and the holdings that
    have dropped out of it and should be sold.
    """
    place = {entry.symbol: i + 1 for i, entry in enumerate(ranked)}
    by_symbol = {entry.symbol: entry for entry in ranked}
    chosen = ranked[:top]
    picks = []
    for i, entry in enumerate(chosen, start=1):
        row = {**asdict(entry), "rank": i, "action": "keep" if entry.symbol in held else "buy"}
        if entry.symbol in held:
            row["since_bought_pct"] = (entry.price / held[entry.symbol] - 1) * 100
        picks.append(row)
    names = {entry.symbol for entry in chosen}
    sells = []
    for symbol, paid in held.items():
        if symbol in names:
            continue
        entry = by_symbol.get(symbol)
        sells.append({
            "symbol": symbol, "paid": paid,
            "price": entry.price if entry else None,
            "since_bought_pct": (entry.price / paid - 1) * 100 if entry and paid > 0 else None,
            "rank": place.get(symbol),  # None: no longer in this list of stocks
        })
    return {"picks": picks, "sells": sells}
