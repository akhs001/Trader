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

# The strategies a user can pick. Each has a long and a short form.
STRATEGIES = {
    "trend_continuation": "Trend continuation",
    "trend_pullback": "Trend pullback",
    "breakout": "Breakout",
    "rsi_reversal": "RSI reversal",
    "ma_cross": "Moving-average cross",
    "macd_cross": "MACD cross",
}
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
    strategy: str  # key of STRATEGIES
    date: str
    price: float
    change_pct: float
    direction: str = "long"  # "long" (buy) or "short" (sell)
    plan: Plan | None = None
    facts: dict[str, str] = field(default_factory=dict)


@dataclass
class _Found:
    strategy: str
    kind: str
    title: str
    reason: str
    # The price (in the view) that would prove the setup wrong, e.g. the
    # pullback low. None when the stop is set by volatility alone.
    stop_level: float | None = None
    stop_name: str = ""


def scan(history: History, config: dict) -> list[Signal]:
    """Return every enabled entry signal that is firing on the latest candle."""
    return Analysis(history).scan(config)


def facts(history: History) -> dict[str, str]:
    """Indicator readings for a stock, whether or not a signal is firing."""
    if len(history.closes) < 2:
        return {}
    return Analysis(history).long.view().facts()


class Analysis:
    """A stock's history prepared for scanning, as of today or any earlier day.

    Indicators are computed once for the whole history. Because each value
    depends only on earlier candles, looking at the first `end` candles gives
    exactly what a live scan saw on that day, which is what a backtest needs.
    """

    def __init__(self, history: History):
        self.history = history
        self.long = _Market(history, short=False)
        self.short = _Market(history, short=True)

    def scan(self, config: dict, end: int | None = None) -> list[Signal]:
        """Signals firing on candle `end - 1` (the latest candle by default)."""
        history = self.history
        end = len(history.closes) if end is None else end
        if end < MIN_BARS:
            return []
        cfg = config.get("signals", {})
        readings = self.long.view(end).facts()
        change_pct = (history.closes[end - 1] / history.closes[end - 2] - 1) * 100
        signals = []

        for direction, market in (("long", self.long), ("short", self.short)):
            if not cfg.get(direction, True):
                continue
            view = market.view(end)
            for name, detector in (
                ("trend_continuation", _trend_continuation),
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
                        strategy=found.strategy,
                        date=history.dates[end - 1],
                        price=history.closes[end - 1],
                        change_pct=change_pct,
                        direction=direction,
                        plan=_plan(view, found, config.get("plan", {})),
                        facts=readings,
                    ))
        return signals


WINDOW = 300  # candles a detector may look back over (enough for a 52-week lookback)


class _Market:
    """One direction's full price history, with indicators computed once.

    With short=True everything is mirrored: closes are negated and highs and
    lows swap, so a falling market looks like a rising one.
    """

    def __init__(self, history: History, short: bool):
        self.h = history
        self.short = short
        if short:
            self.close = [-c for c in history.closes]
            self.high = [-low for low in history.lows]
            self.low = [-high for high in history.highs]
        else:
            self.close, self.high, self.low = history.closes, history.highs, history.lows
        self.rsi = indicators.rsi(self.close, 14)  # mirrored: 100 - the real RSI
        self.atr = indicators.atr(self.high, self.low, self.close, 14)
        self.avg_volume = indicators.sma(history.volumes, 20)
        self._sma: dict[int, list] = {}
        self._macd = None

    def sma(self, period: int) -> list:
        if period not in self._sma:
            self._sma[period] = indicators.sma(self.close, period)
        return self._sma[period]

    def macd(self) -> tuple[list, list]:
        if self._macd is None:
            self._macd = indicators.macd(self.close)
        return self._macd

    def view(self, end: int | None = None) -> "_View":
        return _View(self, len(self.close) if end is None else end)


class _View:
    """What a detector sees: the last candles up to one day, plus indicators.

    All lists end at the day being scanned, so [-1] is "today" and [-2] is
    "yesterday". `price()` turns a value back into a real price (it differs
    from the value itself only for the mirrored, short view).
    """

    def __init__(self, market: _Market, end: int):
        self.market = market
        self.short = market.short
        self.end = end
        self.start = max(0, end - WINDOW)
        self.close = market.close[self.start:end]
        self.high = market.high[self.start:end]
        self.low = market.low[self.start:end]
        self.volumes = market.h.volumes[self.start:end]
        self.sma20, self.sma50, self.sma200 = self.sma(20), self.sma(50), self.sma(200)
        self.rsi = market.rsi[self.start:end]
        self.atr = market.atr[self.start:end]
        self.avg_volume = market.avg_volume[self.start:end]
        # Only today's candle can still be forming.
        latest = end == len(market.close)
        self.session_fraction = market.h.session_fraction if latest else 1.0

    def sma(self, period: int) -> list:
        return self.market.sma(period)[self.start:self.end]

    def macd(self) -> tuple[list, list]:
        line, signal = self.market.macd()
        return line[self.start:self.end], signal[self.start:self.end]

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
        fraction = self.session_fraction
        if fraction < 0.1:
            return None  # too early in the session to judge volume
        return self.volumes[-1] / fraction / average

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
            estimate = " (projected)" if self.session_fraction < 1 else ""
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


def _trend_continuation(view: _View, settings: dict) -> list[_Found]:
    """A big move, a partial retrace of it, and the first day the move resumes.

    Long: a strong run up, a dip that gives back part of it, then a turn up.
    Short: a strong drop, a bounce that recovers part of it, then a turn down.
    """
    leg_days = int(settings.get("leg_days", 60))
    min_leg_atr = float(settings.get("min_leg_atr", 10))
    min_retrace = float(settings.get("min_retrace_pct", 30)) / 100
    max_retrace = float(settings.get("max_retrace_pct", 65)) / 100
    lookback = int(settings.get("lookback_days", 5))

    count = len(view.close)
    atr = view.atr[-1]
    if count < leg_days + 3 or view.sma200[-1] is None or not atr:
        return []
    # The move resumes: the first close beyond the previous day's extreme.
    if view.close[-1] <= view.high[-2] or view.close[-2] > view.high[-3]:
        return []
    # An established trend: the averages are stacked in its direction.
    if not view.sma20[-1] > view.sma50[-1] > view.sma200[-1]:
        return []

    # The big move ("leg"): from its start to its top, inside the window.
    first = count - 1 - leg_days
    top_at = max(range(first, count - 1), key=lambda i: view.high[i])
    if top_at == first or top_at > count - 3:
        return []  # no start before the top, or no room for a retrace after it
    start_at = min(range(first, top_at), key=lambda i: view.low[i])
    top, start = view.high[top_at], view.low[start_at]
    leg = top - start
    if leg < min_leg_atr * atr:
        return []

    # The retrace: how much of the leg was given back, and how recently.
    dip_at = min(range(top_at + 1, count), key=lambda i: view.low[i])
    dip = view.low[dip_at]
    retrace = (top - dip) / leg
    if dip_at < count - 1 - lookback or not min_retrace <= retrace <= max_retrace:
        return []

    move_pct = abs(view.price(top) / view.price(start) - 1) * 100
    days = top_at - start_at
    trigger = view.price(view.high[-2])
    if view.short:
        return [_Found(
            "trend_continuation", "trend_continuation_down", "Downtrend continuation",
            f"Strong downtrend (down {move_pct:.0f}% in {days} days, 20-day average below the "
            f"50-day below the 200-day) bounced {retrace:.0%} of the drop to "
            f"{view.price(dip):.2f} and is now trading below yesterday's low of {trigger:.2f}.",
            dip, "the bounce high",
        )]
    return [_Found(
        "trend_continuation", "trend_continuation", "Uptrend continuation",
        f"Strong uptrend (up {move_pct:.0f}% in {days} days, 20-day average above the "
        f"50-day above the 200-day) retraced {retrace:.0%} of the rise to "
        f"{view.price(dip):.2f} and is now trading above yesterday's high of {trigger:.2f}.",
        dip, "the retrace low",
    )]


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
            "trend_pullback", "trend_rally", "Downtrend rally",
            f"Downtrend (below the 200-day average) rallied to {reached} "
            f"and is now trading below yesterday's low of {trigger:.2f}.",
            extreme, "the rally high",
        )]
    return [_Found(
        "trend_pullback", "trend_pullback", "Trend pullback",
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
            "breakout", "breakdown", "Breakdown",
            f"Price moved below its {lookback}-day low of {view.price(level):.2f} "
            f"on {ratio:.1f}x average volume.",
            level, "the breakdown level",
        )]
    return [_Found(
        "breakout", "breakout", "Breakout",
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
            "rsi_reversal", "overbought_drop", "Overbought reversal",
            f"RSI(14) crossed back below {view.real_rsi(level):.0f} "
            f"(yesterday {was:.0f}, now {now:.0f}).",
            min(view.low[-5:]), "the recent high",
        )]
    return [_Found(
        "rsi_reversal", "oversold_bounce", "Oversold bounce",
        f"RSI(14) crossed back above {level:.0f} (yesterday {was:.0f}, now {now:.0f}).",
        min(view.low[-5:]), "the recent low",
    )]


def _ma_cross(view: _View, settings: dict) -> list[_Found]:
    """Long: the fast average or MACD crosses up. Short: it crosses down."""
    fast_n, slow_n = int(settings.get("fast", 20)), int(settings.get("slow", 50))
    way = "below" if view.short else "above"
    found = []

    fast, slow = view.sma(fast_n), view.sma(slow_n)
    if _crossed_above(fast, slow):
        found.append(_Found(
            "ma_cross",
            "ma_cross_down" if view.short else "ma_cross",
            "Moving-average cross down" if view.short else "Moving-average cross",
            f"The {fast_n}-day average ({view.price(fast[-1]):.2f}) crossed {way} "
            f"the {slow_n}-day average ({view.price(slow[-1]):.2f}).",
        ))

    if settings.get("macd", True):
        line, signal = view.macd()
        if _crossed_above(line, signal):
            zero_side = "below" if view.price(line[-1]) < 0 else "above"
            found.append(_Found(
                "macd_cross",
                "macd_cross_down" if view.short else "macd_cross",
                "MACD cross down" if view.short else "MACD cross",
                f"MACD crossed {way} its signal line ({zero_side} the zero line).",
            ))
    return found


def _crossed_above(a: list[float | None], b: list[float | None]) -> bool:
    if len(a) < 2 or None in (a[-1], a[-2], b[-1], b[-2]):
        return False
    return a[-2] <= b[-2] and a[-1] > b[-1]
