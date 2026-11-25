"""Entry signals built on rules that professional and systematic traders publish.

The rules come from four well-documented schools:

* Trade with the market. Longs only while the S&P 500 is above its 200-day
  average, shorts only while it is below (the "200-day rule" used by Paul
  Tudor Jones and in Meb Faber's timing model).
* Buy leaders, not laggards. A stock must already be in a healthy uptrend
  (Mark Minervini's "trend template") before a breakout or pullback counts.
* Enter at a defined point: a breakout from a tight base on heavy volume
  (William O'Neil, Minervini), a pullback in an uptrend, a short-term dip
  (Larry Connors' 2-period RSI), a 55-day breakout (the Turtles), or a gap on
  heavy volume.
* Leave by rule: a tight stop, and an exit that lets a winner run until its
  trend breaks, instead of a fixed target.

Each detector looks only at the latest candle (which may still be forming).
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
YEAR = 252  # trading days in a year

# The strategies a user can pick. Each has a long and a short form.
STRATEGIES = {
    "dip": "Dip in a leader",
    "gap": "Gap on heavy volume",
    "base_breakout": "Base breakout",
    "pullback": "Pullback with a turn",
    "channel_breakout": "55-day breakout",
}
# Tested on the S&P 500 members over nine years (see README), the long forms of
# the first two beat entering on a random day; these three did not. They are
# left out of "all strategies" and run only when picked by name or switched on.
UNPROVEN = {"base_breakout", "pullback", "channel_breakout"}
STOP_BUFFER_ATR = 0.25  # room left beyond the stop level, in ATRs


@dataclass
class Plan:
    """A mechanical trade plan: where to get in, where to get out."""

    direction: str  # "long" or "short"
    entry: float
    stop: float
    targets: list[float]
    stop_basis: str  # why the stop is where it is
    # The strategy's own way out of the trade: "sma" (the close falls through an
    # average), "lowest" (through a recent low) or "recover" (the close climbs
    # back above an average), with the number of candles. ("", 0) means none.
    exit_kind: str = ""
    exit_period: int = 0
    exit_text: str = ""

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
    date: str  # the candle: "YYYY-MM-DD", or "YYYY-MM-DD HH:MM" on hourly candles
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
    stop_atr: float | None = None  # stop distance in ATRs when there is no level
    max_stop_pct: float | None = None  # never risk more than this share of the price
    exit: tuple[str, int] = ("", 0)  # see Plan.exit_kind


def scan(history: History, config: dict, benchmark: History | None = None,
         only: str | None = None) -> list[Signal]:
    """Return every enabled entry signal that is firing on the latest candle."""
    return Analysis(history, benchmark).scan(config, only=only)


def day_change(history: History, end: int | None = None) -> float:
    """Percent change of the latest price against the previous day's close."""
    end = len(history.closes) if end is None else end
    before = end - 2
    if history.hourly:
        # Step back to the last candle of the day before.
        day = history.dates[end - 1][:10]
        while before > 0 and history.dates[before][:10] == day:
            before -= 1
    return (history.closes[end - 1] / history.closes[before] - 1) * 100


def facts(history: History) -> dict[str, str]:
    """Indicator readings for a stock, whether or not a signal is firing."""
    if len(history.closes) < 2:
        return {}
    return Analysis(history).long.view().facts()


def market_trend(benchmark: History | None) -> dict[str, bool]:
    """For each day, whether the market closed above its 200-day average."""
    if benchmark is None:
        return {}
    average = indicators.sma(benchmark.closes, 200)
    return {day[:10]: close > mean
            for day, close, mean in zip(benchmark.dates, benchmark.closes, average)
            if mean is not None}


class Analysis:
    """A stock's history prepared for scanning, as of today or any earlier day.

    Indicators are computed once for the whole history. Because each value
    depends only on earlier candles, looking at the first `end` candles gives
    exactly what a live scan saw on that day, which is what a backtest needs.

    `benchmark` is the market the stock is judged against (the S&P 500 fund).
    Without it the market filter and the relative-strength test are skipped.
    """

    def __init__(self, history: History, benchmark: History | None = None):
        self.history = history
        self.long = _Market(history, short=False)
        self.short = _Market(history, short=True)
        trend = market_trend(benchmark)
        # Per candle: is the market in an uptrend? None when unknown.
        self.market_up = [trend.get(day[:10]) for day in history.dates]
        self.strength = _relative_strength(history, benchmark)

    def market_state(self, end: int | None = None) -> bool | None:
        end = len(self.history.closes) if end is None else end
        return self.market_up[end - 1] if end else None

    def scan(self, config: dict, end: int | None = None, only: str | None = None) -> list[Signal]:
        """Signals firing on candle `end - 1` (the latest candle by default).

        Normally the strategies switched on in the settings run. `only` names
        one strategy to run instead, switched on or not.
        """
        history = self.history
        end = len(history.closes) if end is None else end
        if end < MIN_BARS:
            return []
        cfg = config.get("signals", {})
        use_market = config.get("market", {}).get("filter", True)
        market_up = self.market_up[end - 1]
        strength = self.strength[end - 1]
        readings = self.long.view(end).facts()
        change_pct = day_change(history, end)
        signals = []

        for direction, market in (("long", self.long), ("short", self.short)):
            if not cfg.get(direction, True):
                continue
            short = direction == "short"
            # Trade with the market: no longs in a falling market, no shorts in a rising one.
            if use_market and market_up is not None and market_up == short:
                continue
            view = market.view(end)
            # Stronger than the market for longs, weaker for shorts. None when unknown.
            view.leading = None if strength is None else (strength < 0 if short else strength > 0)
            for name, detector in DETECTORS:
                settings = cfg.get(name, {})
                if name != only and (only or not settings.get("enabled", name not in UNPROVEN)):
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

    def exit_hit(self, direction: str, kind: str, period: int, index: int) -> bool:
        """Whether candle `index` closed beyond a plan's exit line.

        ("sma", n): the trend broke, a close below the n-candle average.
        ("lowest", n): a close below the lowest low of the n candles before it.
        ("recover", n): the bounce came, a close back above the n-candle average.
        For shorts read all of these upside down.
        """
        market = self.short if direction == "short" else self.long
        close = market.close[index]
        if kind in ("sma", "recover"):
            line = market.sma(period)[index]
            if line is None:
                return False
            return close > line if kind == "recover" else close < line
        if kind == "lowest" and index >= period:
            return close < min(market.low[index - period:index])
        return False


def _relative_strength(history: History, benchmark: History | None, span: int = YEAR // 2) -> list:
    """Per candle: the stock's half-year return minus the market's. None when unknown."""
    result: list[float | None] = [None] * len(history.closes)
    if benchmark is None or history.hourly:
        return result
    position = {day: i for i, day in enumerate(benchmark.dates)}
    for i in range(span, len(history.closes)):
        j = position.get(history.dates[i])
        if j is None or j < span:
            continue
        stock = history.closes[i] / history.closes[i - span]
        market = benchmark.closes[j] / benchmark.closes[j - span]
        result[i] = stock - market
    return result


WINDOW = 300  # candles a detector may look back over (enough for a 52-week lookback)


class _Market:
    """One direction's full price history, with indicators computed once.

    With short=True everything is mirrored: prices are negated and highs and
    lows swap, so a falling market looks like a rising one.
    """

    def __init__(self, history: History, short: bool):
        self.h = history
        self.short = short
        if short:
            self.open = [-o for o in history.opens]
            self.close = [-c for c in history.closes]
            self.high = [-low for low in history.lows]
            self.low = [-high for high in history.highs]
        else:
            self.open, self.close = history.opens, history.closes
            self.high, self.low = history.highs, history.lows
        self.rsi = indicators.rsi(self.close, 14)  # mirrored: 100 - the real RSI
        self.rsi2 = indicators.rsi(self.close, 2)
        self.atr = indicators.atr(self.high, self.low, self.close, 14)
        self._sma: dict[int, list] = {}
        self._volume: dict[int, list] = {}

    def sma(self, period: int) -> list:
        if period not in self._sma:
            self._sma[period] = indicators.sma(self.close, period)
        return self._sma[period]

    def average_volume(self, period: int) -> list:
        if period not in self._volume:
            self._volume[period] = indicators.sma(self.h.volumes, period)
        return self._volume[period]

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
        self.leading: bool | None = None  # set by the scan; see Analysis.scan
        # Words for the candle size, so the same sentences fit days and hours.
        hourly = market.h.hourly
        self.unit = "hour" if hourly else "day"
        self.previous = "the previous hour's" if hourly else "yesterday's"
        self.before = "an hour ago" if hourly else "yesterday"
        self.end = end
        self.start = max(0, end - WINDOW)
        self.open = market.open[self.start:end]
        self.close = market.close[self.start:end]
        self.high = market.high[self.start:end]
        self.low = market.low[self.start:end]
        self.volumes = market.h.volumes[self.start:end]
        self.sma20, self.sma50, self.sma200 = self.sma(20), self.sma(50), self.sma(200)
        self.rsi = market.rsi[self.start:end]
        self.rsi2 = market.rsi2[self.start:end]
        self.atr = market.atr[self.start:end]
        # Only today's candle can still be forming.
        latest = end == len(market.close)
        self.session_fraction = market.h.session_fraction if latest else 1.0

    def sma(self, period: int) -> list:
        return self.market.sma(period)[self.start:self.end]

    def price(self, value: float) -> float:
        return -value if self.short else value

    def real_rsi(self, value: float) -> float:
        return 100 - value if self.short else value

    def volume_ratio(self, period: int = 20) -> float | None:
        """Today's volume vs. the average of the `period` candles before today.

        While the session is open, today's volume is scaled up to a full-day
        estimate so a morning scan is comparable with a full day.
        """
        average = self.market.average_volume(period)[self.end - 2] if self.end >= 2 else None
        if not average:
            return None
        fraction = self.session_fraction
        if fraction < 0.1:
            return None  # too early in the session to judge volume
        return self.volumes[-1] / fraction / average

    def trending(self, near_high_pct: float = 25, above_low_pct: float = 30) -> bool:
        """Minervini's trend template: the stock is already a leader in an uptrend.

        Price above the 50-, 150- and 200-candle averages, the averages stacked
        in that order, the 200 rising, price within `near_high_pct` percent of
        its one-year high and at least `above_low_pct` percent above its
        one-year low, and not weaker than the market.
        """
        if len(self.close) < YEAR:
            return False
        close, sma150 = self.close[-1], self.sma(150)[-1]
        sma50, sma200, sma200_before = self.sma50[-1], self.sma200[-1], self.sma200[-22]
        if None in (sma50, sma150, sma200, sma200_before):
            return False
        top, bottom = max(self.high[-YEAR:]), min(self.low[-YEAR:])
        return (close > sma50 > sma150 > sma200 > sma200_before
                and close >= top - near_high_pct / 100 * abs(top)
                and close >= bottom + above_low_pct / 100 * abs(bottom)
                and self.leading is not False)

    def facts(self) -> dict[str, str]:
        """Readings for display. Only meaningful on the real (long) view."""
        readings: dict[str, str] = {}
        if self.rsi[-1] is not None:
            readings["RSI(14)"] = f"{self.rsi[-1]:.0f}"
        for label, series in ((f"20-{self.unit} avg", self.sma20), (f"50-{self.unit} avg", self.sma50),
                              (f"200-{self.unit} avg", self.sma200)):
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

    if found.stop_level is None:
        stop_atr = found.stop_atr or float(settings.get("default_stop_atr", 2.0))
        distance = stop_atr * atr
        basis = f"{stop_atr:g} ATR {side} entry"
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
    if found.max_stop_pct:
        cap = found.max_stop_pct / 100 * abs(entry)
        # The percentage limit wins unless it would sit inside one ATR of noise.
        if distance > cap >= min_atr * atr:
            distance = cap
            basis = f"{found.max_stop_pct:g}% {side} entry, the most this setup may lose"

    kind, period = found.exit
    average = view.sma(period)[-1] if kind == "recover" else None
    if average is not None:
        # The trade aims at the average it should bounce back to. That line
        # moves a little every day, so the target is where it stands now.
        targets = [max(average, entry + 0.5 * atr)]
    else:
        targets = [entry + float(r) * distance for r in settings.get("targets_r", [2.0, 3.0])]
    return Plan(
        direction="short" if view.short else "long",
        entry=view.price(entry),
        stop=view.price(entry - distance),
        targets=[view.price(target) for target in targets],
        stop_basis=basis,
        exit_kind=kind,
        exit_period=period,
        exit_text=_exit_text(view, kind, period),
    )


def _exit_text(view: _View, kind: str, period: int) -> str:
    beyond = "above" if view.short else "below"
    if kind == "sma":
        return f"Exit when a {view.unit} closes {beyond} the {period}-{view.unit} average."
    if kind == "recover":
        back = "below" if view.short else "above"
        return f"Take the profit when a {view.unit} closes back {back} the {period}-{view.unit} average."
    if kind == "lowest":
        extreme = "high" if view.short else "low"
        return f"Exit when a {view.unit} closes {beyond} the {extreme} of the {period} {view.unit}s before it."
    return ""


def _turned(view: _View) -> bool:
    """The first candle that closes beyond the previous candle's extreme."""
    return view.close[-1] > view.high[-2] and view.close[-2] <= view.high[-3]


def _base_breakout(view: _View, settings: dict) -> list[_Found]:
    """O'Neil / Minervini: a leader leaves a tight base on heavy volume.

    Long: an uptrending leader closes above the top of a base that has been
    tightening, on volume well above normal, and is not yet extended.
    Short: a downtrending laggard closes below the bottom of such a base.
    """
    base = int(settings.get("base_days", 25))
    tight = int(settings.get("tight_days", 10))
    if len(view.close) < base + 3 or not view.trending():
        return []
    pivot = max(view.high[-1 - base:-1])
    if view.close[-1] <= pivot or view.close[-2] > max(view.high[-2 - base:-2]):
        return []  # not through the pivot, or this is not the first day through
    if view.close[-1] - pivot > float(settings.get("max_chase_pct", 5)) / 100 * abs(pivot):
        return []  # already too far past the pivot to buy
    depth = pivot - min(view.low[-1 - base:-1])
    if depth > float(settings.get("max_depth_pct", 25)) / 100 * abs(pivot):
        return []  # too deep to be a base
    tight_low = min(view.low[-1 - tight:-1])
    if max(view.high[-1 - tight:-1]) - tight_low > float(settings.get("tightness", 0.6)) * depth:
        return []  # the base is not tightening
    ratio = view.volume_ratio(50)
    if ratio is None or ratio < float(settings.get("volume_ratio", 1.4)):
        return []

    level, unit = view.price(pivot), view.unit
    common = dict(stop_level=tight_low, max_stop_pct=float(settings.get("max_stop_pct", 8)),
                  exit=("sma", int(settings.get("exit_average", 20))))
    if view.short:
        return [_Found(
            "base_breakout", "base_breakdown", "Base breakdown",
            f"A laggard in a downtrend closed below the bottom of its {base}-{unit} base "
            f"({level:.2f}) on {ratio:.1f}x average volume.",
            stop_name="the high of the base's last days", **common,
        )]
    return [_Found(
        "base_breakout", "base_breakout", "Base breakout",
        f"A leader in an uptrend closed above the top of its {base}-{unit} base "
        f"({level:.2f}) on {ratio:.1f}x average volume.",
        stop_name="the low of the base's last days", **common,
    )]


def _pullback(view: _View, settings: dict) -> list[_Found]:
    """Buy a dip in a leader: a pullback to a rising average that turns back up.

    Long: an uptrending leader dips to its 20- or 50-candle average and then
    closes above the previous candle's high. Short: the mirror image.
    """
    lookback = int(settings.get("lookback_days", 5))
    touch = float(settings.get("touch_pct", 1.0)) / 100
    min_depth_atr = float(settings.get("min_depth_atr", 2.0))
    if len(view.close) < lookback + 3 or not view.trending() or not _turned(view):
        return []
    # A real dip, not just prices drifting along the average.
    extreme = min(view.low[-1 - lookback:])
    depth = max(view.high[-21:-1]) - extreme
    if view.atr[-1] is None or depth < min_depth_atr * view.atr[-1]:
        return []

    reached = None
    for i in range(len(view.close) - 1 - lookback, len(view.close) - 1):
        if view.low[i] <= view.sma50[i] + abs(view.sma50[i]) * touch:
            reached = f"its 50-{view.unit} average"
        elif view.low[i] <= view.sma20[i] + abs(view.sma20[i]) * touch:
            reached = reached or f"its 20-{view.unit} average"
    if reached is None:
        return []

    trigger = view.price(view.high[-2])
    exit_rule = ("lowest", int(settings.get("exit_low", 10)))
    if view.short:
        return [_Found(
            "pullback", "rally", "Rally in a downtrend",
            f"A laggard in a downtrend rallied to {reached} and is now trading below "
            f"{view.previous} low of {trigger:.2f}.",
            extreme, "the rally high", exit=exit_rule,
        )]
    return [_Found(
        "pullback", "pullback", "Pullback in an uptrend",
        f"A leader in an uptrend pulled back to {reached} and is now trading above "
        f"{view.previous} high of {trigger:.2f}.",
        extreme, "the pullback low", exit=exit_rule,
    )]


def _dip(view: _View, settings: dict) -> list[_Found]:
    """Connors' 2-period RSI: a sharp, short dip in a stock that is in an uptrend.

    Long: a leader above its 200-candle average with RSI(2) below 10; take the
    profit on the first close back above the 10-candle average. Short: the
    mirror image.
    """
    level = float(settings.get("rsi_level", 10))
    today, sma200 = view.rsi2[-1], view.sma200[-1]
    if today is None or sma200 is None or view.close[-1] <= sma200 or today >= level:
        return []
    if settings.get("leaders_only", True) and not view.trending():
        return []  # dips in leaders recovered best in testing
    exit_average = int(settings.get("exit_average", 10))
    common = dict(stop_atr=float(settings.get("stop_atr", 2.0)), exit=("recover", exit_average))
    reading = view.real_rsi(today)
    if view.short:
        return [_Found(
            "dip", "pop", "Pop in a laggard",
            f"A laggard in a downtrend bounced sharply: its 2-{view.unit} RSI has spiked to {reading:.0f} "
            f"(above {view.real_rsi(level):.0f}).", **common,
        )]
    return [_Found(
        "dip", "dip", "Dip in a leader",
        f"A leader in an uptrend dipped sharply: its 2-{view.unit} RSI has dropped to {reading:.0f} "
        f"(below {level:.0f}).", **common,
    )]


def _channel_breakout(view: _View, settings: dict) -> list[_Found]:
    """The Turtles' long-term system: a new 55-candle high (low for shorts).

    The stop is 2 ATR away and the trade is left when a candle closes below
    the low of the 20 candles before it.
    """
    span = int(settings.get("days", 55))
    if len(view.close) < span + 3:
        return []
    level = max(view.high[-1 - span:-1])
    if view.close[-1] <= level or view.close[-2] > max(view.high[-2 - span:-2]):
        return []  # no new extreme, or this is not the first day beyond it
    common = dict(stop_atr=float(settings.get("stop_atr", 2.0)),
                  exit=("lowest", int(settings.get("exit_low", 20))))
    if view.short:
        return [_Found(
            "channel_breakout", "channel_breakdown", f"{span}-{view.unit} breakdown",
            f"Price closed below its {span}-{view.unit} low of {view.price(level):.2f}.", **common,
        )]
    return [_Found(
        "channel_breakout", "channel_breakout", f"{span}-{view.unit} breakout",
        f"Price closed above its {span}-{view.unit} high of {view.price(level):.2f}.", **common,
    )]


def _gap(view: _View, settings: dict) -> list[_Found]:
    """A gap on heavy volume that holds: the market repricing a stock on news.

    Long: the stock opens far above the previous close, trades several times
    its normal volume, and closes in the upper half of the day's range.
    Short: the mirror image, a gap down that stays down.
    """
    atr = view.atr[-2] if len(view.atr) >= 2 else None
    if not atr or len(view.close) < 3:
        return []
    gap = view.open[-1] - view.close[-2]
    if gap < float(settings.get("gap_atr", 1.5)) * atr or view.low[-1] <= view.close[-2]:
        return []  # too small, or the gap was already filled
    if view.close[-1] < (view.high[-1] + view.low[-1]) / 2:
        return []  # sold off after the open
    ratio = view.volume_ratio(50)
    if ratio is None or ratio < float(settings.get("volume_ratio", 2.0)):
        return []
    size = abs(gap / view.close[-2]) * 100
    common = dict(stop_level=view.low[-1], exit=("lowest", int(settings.get("exit_low", 20))))
    if view.short:
        return [_Found(
            "gap", "gap_down", "Gap down on heavy volume",
            f"Opened {size:.1f}% below {view.previous} close on {ratio:.1f}x average volume "
            f"and stayed in the lower half of the {view.unit}'s range.",
            stop_name=f"the {view.unit}'s high", **common,
        )]
    return [_Found(
        "gap", "gap_up", "Gap up on heavy volume",
        f"Opened {size:.1f}% above {view.previous} close on {ratio:.1f}x average volume "
        f"and held the upper half of the {view.unit}'s range.",
        stop_name=f"the {view.unit}'s low", **common,
    )]


# The settings section each detector reads, and the detector itself.
DETECTORS = (
    ("dip", _dip),
    ("gap", _gap),
    ("base_breakout", _base_breakout),
    ("pullback", _pullback),
    ("channel_breakout", _channel_breakout),
)
