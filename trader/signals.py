"""Entry-signal detection on daily candles.

Each detector looks only at the latest candle (which may still be forming)
and reports whether an entry condition is true right now.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import indicators
from .data import History

MIN_BARS = 60  # below this there is too little history for anything useful


@dataclass
class Signal:
    symbol: str
    name: str
    kind: str  # stable id, used for de-duplication
    title: str
    reason: str
    date: str
    price: float
    change_pct: float
    facts: dict[str, str] = field(default_factory=dict)


def scan(history: History, config: dict) -> list[Signal]:
    """Return every enabled entry signal that is firing on the latest candle."""
    if len(history.closes) < MIN_BARS:
        return []
    ctx = _Context(history)
    cfg = config.get("signals", {})
    found: list[tuple[str, str, str]] = []

    for name, detector in (
        ("trend_pullback", _trend_pullback),
        ("breakout", _breakout),
        ("oversold_bounce", _oversold_bounce),
        ("ma_cross", _ma_cross),
    ):
        settings = cfg.get(name, {})
        if settings.get("enabled", True):
            found.extend(detector(ctx, settings))

    return [
        Signal(
            symbol=history.symbol,
            name=history.name,
            kind=kind,
            title=title,
            reason=reason,
            date=history.dates[-1],
            price=ctx.close[-1],
            change_pct=(ctx.close[-1] / ctx.close[-2] - 1) * 100,
            facts=ctx.facts(),
        )
        for kind, title, reason in found
    ]


class _Context:
    def __init__(self, history: History):
        self.h = history
        self.close = history.closes
        self.high = history.highs
        self.low = history.lows
        self.sma20 = indicators.sma(self.close, 20)
        self.sma50 = indicators.sma(self.close, 50)
        self.sma200 = indicators.sma(self.close, 200)
        self.rsi = indicators.rsi(self.close, 14)
        self.atr = indicators.atr(self.high, self.low, self.close, 14)
        self.avg_volume = indicators.sma(history.volumes, 20)

    def volume_ratio(self) -> float | None:
        """Today's volume vs. the 20-day average before today.

        While the session is open, today's volume is scaled up to a full-day
        estimate so a morning scan is comparable with a full day.
        """
        average = self.avg_volume[-2]
        if not average:
            return None
        fraction = self.h.session_fraction
        if fraction < 0.1:
            return None  # too early in the session to judge volume
        return self.h.volumes[-1] / fraction / average

    def facts(self) -> dict[str, str]:
        facts: dict[str, str] = {}
        if self.rsi[-1] is not None:
            facts["RSI(14)"] = f"{self.rsi[-1]:.0f}"
        for label, series in (("20-day avg", self.sma20), ("50-day avg", self.sma50),
                              ("200-day avg", self.sma200)):
            if series[-1] is not None:
                facts[label] = f"{series[-1]:.2f}"
        ratio = self.volume_ratio()
        if ratio is not None:
            estimate = " (projected)" if self.h.session_fraction < 1 else ""
            facts["Volume"] = f"{ratio:.1f}x average{estimate}"
        facts["10-day low"] = f"{min(self.low[-10:]):.2f}"
        if self.atr[-1] is not None:
            facts["ATR(14)"] = f"{self.atr[-1]:.2f}"
        return facts


def _trend_pullback(ctx: _Context, settings: dict) -> list[tuple[str, str, str]]:
    lookback = int(settings.get("lookback_days", 5))
    touch = 1 + float(settings.get("touch_pct", 1.0)) / 100
    rsi_below = float(settings.get("rsi_below", 40))

    if ctx.sma200[-1] is None or len(ctx.close) < lookback + 2:
        return []
    uptrend = ctx.close[-1] > ctx.sma200[-1] and ctx.sma50[-1] > ctx.sma200[-1]
    if not uptrend or ctx.close[-1] <= ctx.high[-2]:
        return []

    # Did the stock pull back on any of the days just before today?
    pulled_back_to = None
    for i in range(len(ctx.close) - 1 - lookback, len(ctx.close) - 1):
        if ctx.low[i] <= ctx.sma50[i] * touch:
            pulled_back_to = "its 50-day average"
        elif ctx.low[i] <= ctx.sma20[i] * touch:
            pulled_back_to = pulled_back_to or "its 20-day average"
        elif ctx.rsi[i] is not None and ctx.rsi[i] < rsi_below:
            pulled_back_to = pulled_back_to or f"RSI below {rsi_below:.0f}"
    if pulled_back_to is None:
        return []
    return [(
        "trend_pullback",
        "Trend pullback",
        f"Uptrend (above the 200-day average) pulled back to {pulled_back_to} "
        f"and is now trading above yesterday's high of {ctx.high[-2]:.2f}.",
    )]


def _breakout(ctx: _Context, settings: dict) -> list[tuple[str, str, str]]:
    lookback = int(settings.get("lookback_days", 20))
    min_ratio = float(settings.get("volume_ratio", 1.5))
    if len(ctx.close) < lookback + 2:
        return []

    level = max(ctx.high[-1 - lookback : -1])
    if ctx.close[-1] <= level:
        return []
    # Only the first day above the level counts as the breakout.
    previous_level = max(ctx.high[-2 - lookback : -2])
    if ctx.close[-2] > previous_level:
        return []
    ratio = ctx.volume_ratio()
    if ratio is None or ratio < min_ratio:
        return []
    return [(
        "breakout",
        "Breakout",
        f"Price moved above its {lookback}-day high of {level:.2f} "
        f"on {ratio:.1f}x average volume.",
    )]


def _oversold_bounce(ctx: _Context, settings: dict) -> list[tuple[str, str, str]]:
    level = float(settings.get("rsi_level", 30))
    today, yesterday = ctx.rsi[-1], ctx.rsi[-2]
    if today is None or yesterday is None or not (yesterday < level <= today):
        return []
    return [(
        "oversold_bounce",
        "Oversold bounce",
        f"RSI(14) crossed back above {level:.0f} "
        f"(yesterday {yesterday:.0f}, now {today:.0f}).",
    )]


def _ma_cross(ctx: _Context, settings: dict) -> list[tuple[str, str, str]]:
    fast_n, slow_n = int(settings.get("fast", 20)), int(settings.get("slow", 50))
    found = []

    fast = indicators.sma(ctx.close, fast_n)
    slow = indicators.sma(ctx.close, slow_n)
    if _crossed_above(fast, slow):
        found.append((
            "ma_cross",
            "Moving-average cross",
            f"The {fast_n}-day average ({fast[-1]:.2f}) crossed above "
            f"the {slow_n}-day average ({slow[-1]:.2f}).",
        ))

    if settings.get("macd", True):
        line, signal = indicators.macd(ctx.close)
        if _crossed_above(line, signal):
            side = "below" if line[-1] < 0 else "above"
            found.append((
                "macd_cross",
                "MACD cross",
                f"MACD crossed above its signal line ({side} the zero line).",
            ))
    return found


def _crossed_above(a: list[float | None], b: list[float | None]) -> bool:
    if len(a) < 2 or None in (a[-1], a[-2], b[-1], b[-2]):
        return False
    return a[-2] <= b[-2] and a[-1] > b[-1]
