"""Entry-signal detection on daily candles, for long and short entries.

Each detector looks only at the latest candle (which may still be forming)
and reports whether an entry condition is true right now.

Short setups are the long setups turned upside down, so every detector is
written once. It runs on a `_View` of the market: for longs that is the real
prices; for shorts it is the prices mirrored (negated), where "up" means the
real price falling. A detector therefore always looks for the bullish pattern
and only its wording differs by direction.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import indicators
from .data import History

MIN_BARS = 60  # below this there is too little history for anything useful
STOP_BUFFER_ATR = 0.25  # room left beyond the stop level, in ATRs


@dataclass
class Plan:
    """A mechanical trade plan: where to get in, where to get out."""

    direction: str  # "long" or "short"
    entry: float
    stop: float
    targets: list[float]
    stop_basis: str  # why the stop is where it is

    @property
    def risk(self) -> float:
        return abs(self.entry - self.stop)


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
    direction: str = "long"  # "long" (buy) or "short" (sell)
    plan: Plan | None = None
    facts: dict[str, str] = field(default_factory=dict)


@dataclass
class _Found:
    kind: str
    title: str
    reason: str
    # The price (in the view) that would prove the setup wrong, e.g. the
    # pullback low. None when the stop is set by volatility alone.
    stop_level: float | None = None
    stop_name: str = ""


def scan(history: History, config: dict) -> list[Signal]:
    """Return every enabled entry signal that is firing on the latest candle."""
    if len(history.closes) < MIN_BARS:
        return []
    cfg = config.get("signals", {})
    readings = _View(history).facts()
    change_pct = (history.closes[-1] / history.closes[-2] - 1) * 100
    signals = []

    for direction in ("long", "short"):
        if not cfg.get(direction, True):
            continue
        view = _View(history, short=direction == "short")
        for name, detector in (
            ("trend_pullback", _trend_pullback),
            ("breakout", _breakout),
            ("oversold_bounce", _oversold_bounce),
            ("ma_cross", _ma_cross),
        ):
            settings = cfg.get(name, {})
            if not settings.get("enabled", True):
                continue
            for found in detector(view, settings):
                signals.append(Signal(
                    symbol=history.symbol,
                    name=history.name,
                    kind=found.kind,
                    title=found.title,
                    reason=found.reason,
                    date=history.dates[-1],
                    price=history.closes[-1],
                    change_pct=change_pct,
                    direction=direction,
                    plan=_plan(view, found, config.get("plan", {})),
                    facts=readings,
                ))
    return signals


def facts(history: History) -> dict[str, str]:
    """Indicator readings for a stock, whether or not a signal is firing."""
    if len(history.closes) < 2:
        return {}
    return _View(history).facts()


class _View:
    """Prices and indicators as a detector sees them.

    With short=True everything is mirrored: closes are negated and highs and
    lows swap, so a falling market looks like a rising one. `price()` turns a
    value from the view back into a real price.
    """

    def __init__(self, history: History, short: bool = False):
        self.h = history
        self.short = short
        if short:
            self.close = [-c for c in history.closes]
            self.high = [-low for low in history.lows]
            self.low = [-high for high in history.highs]
        else:
            self.close, self.high, self.low = history.closes, history.highs, history.lows
        self.sma20 = indicators.sma(self.close, 20)
        self.sma50 = indicators.sma(self.close, 50)
        self.sma200 = indicators.sma(self.close, 200)
        self.rsi = indicators.rsi(self.close, 14)  # mirrored: 100 - the real RSI
        self.atr = indicators.atr(self.high, self.low, self.close, 14)
        self.avg_volume = indicators.sma(history.volumes, 20)

    def price(self, value: float) -> float:
        return -value if self.short else value

    def real_rsi(self, value: float) -> float:
        return 100 - value if self.short else value

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
        """Readings for display. Only meaningful on the real (long) view."""
        readings: dict[str, str] = {}
        if self.rsi[-1] is not None:
            readings["RSI(14)"] = f"{self.rsi[-1]:.0f}"
        for label, series in (("20-day avg", self.sma20), ("50-day avg", self.sma50),
                              ("200-day avg", self.sma200)):
            if series[-1] is not None:
                readings[label] = f"{series[-1]:.2f}"
        ratio = self.volume_ratio()
        if ratio is not None:
            estimate = " (projected)" if self.h.session_fraction < 1 else ""
            readings["Volume"] = f"{ratio:.1f}x average{estimate}"
        if self.atr[-1] is not None:
            readings["ATR(14)"] = f"{self.atr[-1]:.2f}"
        return readings


def _plan(view: _View, found: _Found, settings: dict) -> Plan | None:
    """Entry at the current price, stop from the setup, targets as multiples of the risk."""
    atr = view.atr[-1]
    if not atr:
        return None
    entry = view.close[-1]
    side = "above" if view.short else "below"  # where the stop sits, in real prices
    min_atr = float(settings.get("min_stop_atr", 1.0))
    max_atr = max(float(settings.get("max_stop_atr", 3.0)), min_atr)
    default_atr = min(max(float(settings.get("default_stop_atr", 2.0)), min_atr), max_atr)

    if found.stop_level is None:
        distance = default_atr * atr
        basis = f"{default_atr:g} ATR {side} entry"
    else:
        level = f"{view.price(found.stop_level):.2f}"
        distance = entry - (found.stop_level - STOP_BUFFER_ATR * atr)
        basis = f"just {side} {found.stop_name} ({level})"
        # Keep the stop out of everyday noise, and the risk from getting too large.
        if distance < min_atr * atr:
            distance = min_atr * atr
            basis = f"{min_atr:g} ATR {side} entry ({found.stop_name} at {level} is too close)"
        elif distance > max_atr * atr:
            distance = max_atr * atr
            basis = f"{max_atr:g} ATR {side} entry ({found.stop_name} at {level} is too far)"

    multiples = settings.get("targets_r", [2.0, 3.0])
    return Plan(
        direction="short" if view.short else "long",
        entry=view.price(entry),
        stop=view.price(entry - distance),
        targets=[view.price(entry + float(r) * distance) for r in multiples],
        stop_basis=basis,
    )


def _trend_pullback(view: _View, settings: dict) -> list[_Found]:
    """Long: a dip in an uptrend that turns back up. Short: a rally in a downtrend that turns down."""
    lookback = int(settings.get("lookback_days", 5))
    touch = float(settings.get("touch_pct", 1.0)) / 100
    rsi_below = float(settings.get("rsi_below", 40))
    min_depth_atr = float(settings.get("min_depth_atr", 2.0))

    if view.sma200[-1] is None or len(view.close) < lookback + 3:
        return []
    with_trend = view.close[-1] > view.sma200[-1] and view.sma50[-1] > view.sma200[-1]
    if not with_trend or view.close[-1] <= view.high[-2]:
        return []
    # Only the first day of the turn counts, not every day that follows.
    if view.close[-2] > view.high[-3]:
        return []
    # A real move against the trend, not just prices drifting along the average.
    extreme = min(view.low[-1 - lookback :])
    depth = max(view.high[-21:-1]) - extreme
    if view.atr[-1] is None or depth < min_depth_atr * view.atr[-1]:
        return []

    # Did the stock reach an average, or a stretched RSI, on the days just before today?
    reached = None
    for i in range(len(view.close) - 1 - lookback, len(view.close) - 1):
        if view.low[i] <= view.sma50[i] + abs(view.sma50[i]) * touch:
            reached = "its 50-day average"
        elif view.low[i] <= view.sma20[i] + abs(view.sma20[i]) * touch:
            reached = reached or "its 20-day average"
        elif view.rsi[i] is not None and view.rsi[i] < rsi_below:
            side = "above" if view.short else "below"
            reached = reached or f"RSI {side} {view.real_rsi(rsi_below):.0f}"
    if reached is None:
        return []

    trigger = view.price(view.high[-2])
    if view.short:
        return [_Found(
            "trend_rally", "Downtrend rally",
            f"Downtrend (below the 200-day average) rallied to {reached} "
            f"and is now trading below yesterday's low of {trigger:.2f}.",
            extreme, "the rally high",
        )]
    return [_Found(
        "trend_pullback", "Trend pullback",
        f"Uptrend (above the 200-day average) pulled back to {reached} "
        f"and is now trading above yesterday's high of {trigger:.2f}.",
        extreme, "the pullback low",
    )]


def _breakout(view: _View, settings: dict) -> list[_Found]:
    """Long: a close above the recent highs on volume. Short: below the recent lows."""
    lookback = int(settings.get("lookback_days", 20))
    min_ratio = float(settings.get("volume_ratio", 1.5))
    if len(view.close) < lookback + 2:
        return []

    level = max(view.high[-1 - lookback : -1])
    if view.close[-1] <= level:
        return []
    # Only the first day beyond the level counts.
    previous_level = max(view.high[-2 - lookback : -2])
    if view.close[-2] > previous_level:
        return []
    ratio = view.volume_ratio()
    if ratio is None or ratio < min_ratio:
        return []

    if view.short:
        return [_Found(
            "breakdown", "Breakdown",
            f"Price moved below its {lookback}-day low of {view.price(level):.2f} "
            f"on {ratio:.1f}x average volume.",
            level, "the breakdown level",
        )]
    return [_Found(
        "breakout", "Breakout",
        f"Price moved above its {lookback}-day high of {view.price(level):.2f} "
        f"on {ratio:.1f}x average volume.",
        level, "the breakout level",
    )]


def _oversold_bounce(view: _View, settings: dict) -> list[_Found]:
    """Long: RSI climbs back out of oversold. Short: RSI drops back out of overbought."""
    level = float(settings.get("rsi_level", 30))
    today, yesterday = view.rsi[-1], view.rsi[-2]
    if today is None or yesterday is None or not (yesterday < level <= today):
        return []
    was, now = view.real_rsi(yesterday), view.real_rsi(today)
    if view.short:
        return [_Found(
            "overbought_drop", "Overbought reversal",
            f"RSI(14) crossed back below {view.real_rsi(level):.0f} "
            f"(yesterday {was:.0f}, now {now:.0f}).",
            min(view.low[-5:]), "the recent high",
        )]
    return [_Found(
        "oversold_bounce", "Oversold bounce",
        f"RSI(14) crossed back above {level:.0f} (yesterday {was:.0f}, now {now:.0f}).",
        min(view.low[-5:]), "the recent low",
    )]


def _ma_cross(view: _View, settings: dict) -> list[_Found]:
    """Long: the fast average or MACD crosses up. Short: it crosses down."""
    fast_n, slow_n = int(settings.get("fast", 20)), int(settings.get("slow", 50))
    way = "below" if view.short else "above"
    found = []

    fast = indicators.sma(view.close, fast_n)
    slow = indicators.sma(view.close, slow_n)
    if _crossed_above(fast, slow):
        found.append(_Found(
            "ma_cross_down" if view.short else "ma_cross",
            "Moving-average cross down" if view.short else "Moving-average cross",
            f"The {fast_n}-day average ({view.price(fast[-1]):.2f}) crossed {way} "
            f"the {slow_n}-day average ({view.price(slow[-1]):.2f}).",
        ))

    if settings.get("macd", True):
        line, signal = indicators.macd(view.close)
        if _crossed_above(line, signal):
            zero_side = "below" if view.price(line[-1]) < 0 else "above"
            found.append(_Found(
                "macd_cross_down" if view.short else "macd_cross",
                "MACD cross down" if view.short else "MACD cross",
                f"MACD crossed {way} its signal line ({zero_side} the zero line).",
            ))
    return found


def _crossed_above(a: list[float | None], b: list[float | None]) -> bool:
    if len(a) < 2 or None in (a[-1], a[-2], b[-1], b[-2]):
        return False
    return a[-2] <= b[-2] and a[-1] > b[-1]
