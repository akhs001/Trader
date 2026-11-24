"""Backtesting: replay past candles and trade every signal by its own plan.

For each past day the scanner runs exactly as it would have that day. A signal
opens a trade at that day's close with the plan's SL and TP, and the candles
that follow decide how it ends. Results are in R, where 1R is the distance
from entry to SL, and in money for a given starting budget. Costs and slippage
are not modelled.
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
    hold: int = 30  # close the trade after this long...
    hold_unit: str = "days"  # ...counted in trading "days" or market "hours"
    budget: float = 10_000.0  # starting budget
    risk_pct: float = 1.0  # share of the budget lost if a trade hits its SL


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
    exit_date: str  # a day, or "day hour:minute" when tested on hourly candles
    exit: float
    reason: str  # "target", "stop", "time" or "open"
    r: float  # result in R (multiples of the risk)
    pct: float  # result in percent of the entry price
    held: int  # candles held: trading days, or market hours
    shares: float = 0.0  # position size for the starting budget
    pnl: float | None = None  # result in money; None while the trade is open


@dataclass
class _Bar:
    label: str
    open: float
    high: float
    low: float
    close: float


def backtest_stock(history: History, config: dict, options: Options,
                   hourly: History | None = None) -> list[Trade]:
    """All trades the signals would have produced for one stock.

    With `hourly` candles given, trades are followed hour by hour (needed when
    the holding time is in hours); otherwise day by day.
    """
    analysis = Analysis(history)
    count = len(history.closes)
    first = max(0, count - int(options.years * 252))  # about 252 trading days a year
    day_index = {day: i for i, day in enumerate(history.dates)}
    hours = _HourlyBars(hourly) if hourly else None
    busy_until: dict[tuple[str, str], int] = {}  # one trade at a time per strategy and direction
    trades = []
    for day in range(first, count):
        if hours and not hours.covers(history.dates[day]):
            continue  # before the hourly candles begin
        for signal in analysis.scan(config, end=day + 1):
            key = (signal.strategy, signal.direction)
            if (not wanted(signal, options.strategy, options.direction)
                    or signal.plan is None or busy_until.get(key, -1) >= day):
                continue
            if hours:
                bars = hours.after(history.dates[day], history.closes[day])
            else:
                bars = [_Bar(history.dates[i], history.opens[i], history.highs[i],
                             history.lows[i], history.closes[i]) for i in range(day + 1, count)]
            trade = _simulate(history, day, signal, bars, options)
            if trade.reason == "open":
                busy_until[key] = count
            elif hours:
                # Ended during a day: that day's close may open the next trade.
                busy_until[key] = day_index.get(trade.exit_date[:10], count) - 1
            else:
                busy_until[key] = day_index[trade.exit_date]
            trades.append(trade)
    return trades


class _HourlyBars:
    """Hourly candles, grouped by day, for following a trade hour by hour."""

    def __init__(self, hourly: History):
        self.h = hourly
        self.last_of_day: dict[str, int] = {}
        for i, stamp in enumerate(hourly.dates):
            self.last_of_day[stamp[:10]] = i

    def covers(self, day: str) -> bool:
        return day in self.last_of_day

    def after(self, day: str, daily_close: float) -> list[_Bar]:
        """The hourly candles after `day`'s close.

        Daily prices are adjusted for dividends and hourly ones are not, so the
        hourly prices are rescaled to line up with that day's daily close.
        """
        last = self.last_of_day[day]
        scale = daily_close / self.h.closes[last]
        h = self.h
        return [_Bar(h.dates[i], h.opens[i] * scale, h.highs[i] * scale,
                     h.lows[i] * scale, h.closes[i] * scale)
                for i in range(last + 1, len(h.dates))]


def _simulate(history: History, day: int, signal: Signal, bars: list[_Bar],
              options: Options) -> Trade:
    """Follow one trade from its entry at the close of `day` to its exit."""
    plan = signal.plan
    sign = 1 if signal.direction == "long" else -1
    target = plan.targets[min(options.target, len(plan.targets)) - 1]
    exit_price, reason, held = history.closes[-1], "open", len(bars)
    exit_label = bars[-1].label if bars else history.dates[day]

    for number, bar in enumerate(bars, start=1):
        # The worst and best prices of the candle, seen from the trade's side.
        against = bar.low if sign > 0 else bar.high
        in_favour = bar.high if sign > 0 else bar.low
        if sign * (bar.open - plan.stop) <= 0:
            exit_price, reason = bar.open, "stop"  # gapped through the stop
        elif sign * (bar.open - target) >= 0:
            exit_price, reason = bar.open, "target"  # gapped through the target
        elif sign * (against - plan.stop) <= 0:
            # If stop and target are both touched in one candle, assume the
            # stop came first: the candle cannot tell, so take the worse case.
            exit_price, reason = plan.stop, "stop"
        elif sign * (in_favour - target) >= 0:
            exit_price, reason = target, "target"
        elif number >= options.hold:
            exit_price, reason = bar.close, "time"
        else:
            continue
        held, exit_label = number, bar.label
        break

    gain = sign * (exit_price - plan.entry)
    return Trade(
        symbol=history.symbol, strategy=signal.strategy, title=signal.title,
        direction=signal.direction, entry_date=history.dates[day], entry=plan.entry,
        stop=plan.stop, target=target, exit_date=exit_label, exit=exit_price,
        reason=reason, r=gain / plan.risk, pct=gain / plan.entry * 100, held=held,
    )


def apply_budget(trades: list[Trade], budget: float, risk_pct: float) -> None:
    """Size every trade for a starting budget and fill in its money result.

    Each trade risks `risk_pct` percent of the budget as it stands when the
    trade is entered: if the SL is hit, that is what it loses. A position is
    never larger than the budget itself (no borrowing), and gains and losses
    are added to the budget when a trade ends.
    """
    done: list[Trade] = []
    for trade in sorted(trades, key=lambda t: t.entry_date):
        # Money available at the entry: the start plus every trade ended by then.
        equity = budget + sum(t.pnl for t in done if t.exit_date[:10] <= trade.entry_date)
        risk_per_share = abs(trade.entry - trade.stop)
        if equity <= 0 or risk_per_share <= 0:
            trade.shares = 0.0
        else:
            trade.shares = min(equity * risk_pct / 100 / risk_per_share, equity / trade.entry)
        if trade.reason == "open":
            trade.pnl = None
            continue
        sign = 1 if trade.direction == "long" else -1
        trade.pnl = trade.shares * sign * (trade.exit - trade.entry)
        done.append(trade)


def summarize(trades: list[Trade], budget: float | None = None) -> dict:
    """Statistics for a set of trades. Trades still open are left out."""
    closed = sorted((t for t in trades if t.reason != "open"), key=lambda t: t.exit_date)
    stats = {"trades": len(closed), "open": len(trades) - len(closed)}
    profit = sum(t.pnl or 0.0 for t in closed)
    stats["profit"] = profit
    if budget is not None:
        stats.update(start_budget=budget, end_budget=budget + profit,
                     return_pct=profit / budget * 100 if budget else 0.0)
    if not closed:
        return stats
    wins = [t.r for t in closed if t.r > 0]
    losses = [-t.r for t in closed if t.r <= 0]
    total = peak = drawdown = 0.0
    money = money_peak = budget or 0.0
    money_drawdown = 0.0
    for trade in closed:
        total += trade.r
        peak = max(peak, total)
        drawdown = max(drawdown, peak - total)
        money += trade.pnl or 0.0
        money_peak = max(money_peak, money)
        if money_peak > 0:
            money_drawdown = max(money_drawdown, (money_peak - money) / money_peak * 100)
    stats.update(
        win_rate=len(wins) / len(closed) * 100,
        avg_r=total / len(closed),
        total_r=total,
        # Gains divided by losses; None when there were no losses to divide by.
        profit_factor=sum(wins) / sum(losses) if sum(losses) else None,
        max_drawdown_r=drawdown,
        max_drawdown_pct=money_drawdown,  # largest fall of the budget from a peak
        avg_held=sum(t.held for t in closed) / len(closed),
    )
    return stats


def report(trades: list[Trade], options: Options | None = None) -> dict:
    """Everything the Backtest page shows, as plain data."""
    options = options or Options()
    apply_budget(trades, options.budget, options.risk_pct)
    rows = []
    for strategy, name in STRATEGIES.items():
        for direction in ("long", "short"):
            group = [t for t in trades if t.strategy == strategy and t.direction == direction]
            if group:
                rows.append({"strategy": strategy, "name": name, "direction": direction,
                             **summarize(group)})
    closed = sorted((t for t in trades if t.reason != "open"), key=lambda t: t.exit_date)
    curve, total, money = [], 0.0, options.budget
    for trade in closed:
        total += trade.r
        money += trade.pnl or 0.0
        curve.append({"date": trade.exit_date, "r": round(total, 3), "budget": round(money, 2)})
    return {
        "total": summarize(trades, options.budget),
        "rows": rows,
        "curve": curve,
        "trades": [asdict(t) for t in sorted(trades, key=lambda t: t.entry_date, reverse=True)],
    }
