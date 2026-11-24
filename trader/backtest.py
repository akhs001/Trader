"""Backtesting: replay past candles and trade every signal by its own plan.

For each past day the scanner runs exactly as it would have that day. A signal
opens a trade at that day's close with the plan's SL and TP, and the following
days decide how it ends. No costs, slippage or position sizing are modelled;
results are in R, where 1R is the distance from entry to SL.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass

from .data import History
from .scanner import wanted
from .signals import STRATEGIES, Analysis, Signal


@dataclass
class Options:
    strategy: str = "all"  # a key of STRATEGIES, or "all"
    direction: str = "both"  # "long", "short" or "both"
    years: float = 2  # how far back to take signals
    target: int = 1  # which TP ends the trade: 1 or 2
    max_days: int = 30  # close the trade after this many trading days


@dataclass
class Trade:
    symbol: str
    strategy: str
    title: str
    direction: str
    entry_date: str
    entry: float
    stop: float
    target: float
    exit_date: str
    exit: float
    reason: str  # "target", "stop", "time" or "open"
    r: float  # result in R (multiples of the risk)
    pct: float  # result in percent of the entry price
    days: int  # trading days held


def backtest_stock(history: History, config: dict, options: Options) -> list[Trade]:
    """All trades the signals would have produced for one stock."""
    analysis = Analysis(history)
    count = len(history.closes)
    first = max(0, count - int(options.years * 252))  # about 252 trading days a year
    busy_until: dict[tuple[str, str], int] = {}  # one trade at a time per strategy and direction
    trades = []
    for day in range(first, count):
        for signal in analysis.scan(config, end=day + 1):
            key = (signal.strategy, signal.direction)
            if (not wanted(signal, options.strategy, options.direction)
                    or signal.plan is None or busy_until.get(key, -1) >= day):
                continue
            trade, last_day = _simulate(history, day, signal, options)
            busy_until[key] = last_day
            trades.append(trade)
    return trades


def _simulate(history: History, day: int, signal: Signal, options: Options) -> tuple[Trade, int]:
    """Follow one trade from its entry day to its exit."""
    plan = signal.plan
    sign = 1 if signal.direction == "long" else -1
    target = plan.targets[min(options.target, len(plan.targets)) - 1]
    exit_price, reason, last = history.closes[-1], "open", len(history.closes) - 1

    for i in range(day + 1, len(history.closes)):
        open_, high, low = history.opens[i], history.highs[i], history.lows[i]
        # The worst and best prices of the day, seen from the trade's side.
        against = low if sign > 0 else high
        in_favour = high if sign > 0 else low
        if sign * (open_ - plan.stop) <= 0:
            exit_price, reason = open_, "stop"  # gapped through the stop
        elif sign * (open_ - target) >= 0:
            exit_price, reason = open_, "target"  # gapped through the target
        elif sign * (against - plan.stop) <= 0:
            # If stop and target are both touched in one day, assume the stop
            # came first: daily candles cannot tell, so take the worse case.
            exit_price, reason = plan.stop, "stop"
        elif sign * (in_favour - target) >= 0:
            exit_price, reason = target, "target"
        elif i - day >= options.max_days:
            exit_price, reason = history.closes[i], "time"
        else:
            continue
        last = i
        break

    gain = sign * (exit_price - plan.entry)
    return Trade(
        symbol=history.symbol, strategy=signal.strategy, title=signal.title,
        direction=signal.direction, entry_date=history.dates[day], entry=plan.entry,
        stop=plan.stop, target=target, exit_date=history.dates[last], exit=exit_price,
        reason=reason, r=gain / plan.risk, pct=gain / plan.entry * 100, days=last - day,
    ), last


def summarize(trades: list[Trade]) -> dict:
    """Statistics for a set of trades. Trades still open are left out."""
    closed = sorted((t for t in trades if t.reason != "open"), key=lambda t: t.exit_date)
    stats = {"trades": len(closed), "open": len(trades) - len(closed)}
    if not closed:
        return stats
    wins = [t.r for t in closed if t.r > 0]
    losses = [-t.r for t in closed if t.r <= 0]
    total = peak = drawdown = 0.0
    for trade in closed:
        total += trade.r
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
    stats.update(
        win_rate=len(wins) / len(closed) * 100,
        avg_r=total / len(closed),
        total_r=total,
        # Gains divided by losses; None when there were no losses to divide by.
        profit_factor=sum(wins) / sum(losses) if sum(losses) else None,
        max_drawdown_r=drawdown,
        avg_days=sum(t.days for t in closed) / len(closed),
        avg_pct=sum(t.pct for t in closed) / len(closed),
    )
    return stats


def report(trades: list[Trade]) -> dict:
    """Everything the Backtest page shows, as plain data."""
    rows = []
    for strategy, name in STRATEGIES.items():
        for direction in ("long", "short"):
            group = [t for t in trades if t.strategy == strategy and t.direction == direction]
            if group:
                rows.append({"strategy": strategy, "name": name, "direction": direction,
                             **summarize(group)})
    closed = sorted((t for t in trades if t.reason != "open"), key=lambda t: t.exit_date)
    curve, total = [], 0.0
    for trade in closed:
        total += trade.r
        curve.append({"date": trade.exit_date, "r": round(total, 3)})
    return {
        "total": summarize(trades),
        "rows": rows,
        "curve": curve,
        "trades": [asdict(t) for t in sorted(trades, key=lambda t: t.entry_date, reverse=True)],
    }
