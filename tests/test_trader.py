import tempfile
import unittest
import unittest.mock
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from trader import emailer, indicators, market
from trader.data import ApiKeyError, DataError, History, parse_time_series
from trader.main import load_env_file, load_watchlist
from trader.signals import STRATEGIES, UNPROVEN, Analysis, scan
from trader.state import AlertState

# Every strategy switched on, including the ones that are off by default.
CONFIG = {"signals": {name: {"enabled": True} for name in STRATEGIES}}


def make_history(closes, volumes=None, highs=None, lows=None, session_fraction=1.0, opens=None):
    n = len(closes)
    start = date(2024, 1, 1)
    return History(
        symbol="TEST",
        name="Test Corp",
        dates=[(start + timedelta(days=i)).isoformat() for i in range(n)],
        opens=list(opens or closes),
        highs=highs or [c * 1.005 for c in closes],
        lows=lows or [c * 0.995 for c in closes],
        closes=list(closes),
        volumes=volumes or [1_000_000.0] * n,
        session_fraction=session_fraction,
    )


def kinds(history, config=CONFIG, benchmark=None):
    return {s.kind for s in scan(history, config, benchmark)}


def find(history, kind, config=CONFIG, benchmark=None):
    return next((s for s in scan(history, config, benchmark) if s.kind == kind), None)


def mirrored(history):
    """The same chart upside down: every rise becomes an equal fall."""
    top = 2 * max(history.highs)
    return History(
        symbol=history.symbol, name=history.name, dates=history.dates,
        opens=[top - v for v in history.opens], highs=[top - v for v in history.lows],
        lows=[top - v for v in history.highs], closes=[top - v for v in history.closes],
        volumes=history.volumes, session_fraction=history.session_fraction,
    )


# --- Charts that set up each kind of entry ------------------------------------

def leader(days=300, rate=0.003):
    """A steady climber: passes the trend template on its last day."""
    return [100 * (1 + rate) ** i for i in range(days)]


def flat_breakout():
    """A year at 100, then a jump to 103 on double volume: a 55-day breakout and a gap."""
    return make_history([100.0] * 259 + [103.0], volumes=[1_000_000.0] * 259 + [2_000_000.0])


def dip_setup(drops=(0.985, 0.985)):
    """A leader that sells off sharply for two days."""
    closes = leader()
    for factor in drops:
        closes.append(closes[-1] * factor)
    return make_history(closes)


def gap_setup(open_=103.0, close=103.8, low=102.9, volume=3_000_000.0):
    """A quiet stock that opens far above yesterday's close on heavy volume and holds."""
    closes = [100.0] * 259 + [close]
    history = make_history(closes, volumes=[1_000_000.0] * 259 + [volume],
                           opens=[100.0] * 259 + [open_])
    history.highs[-1], history.lows[-1] = max(open_, close) + 0.2, low
    return history


def base_setup(volume=2_000_000.0, final=0.995):
    """A leader builds a five-week base that tightens, then closes above it on volume."""
    closes = leader()
    top = closes[-1]
    wide = [0.97, 0.95, 0.96, 0.94, 0.96, 0.97, 0.95, 0.96, 0.97, 0.96, 0.95, 0.96, 0.97, 0.96, 0.97]
    tight = [0.975, 0.98, 0.975, 0.98, 0.978, 0.98, 0.977, 0.98, 0.979, 0.98]
    closes += [top * level for level in wide + tight] + [top * final]
    return make_history(closes, volumes=[1_000_000.0] * (len(closes) - 1) + [volume])


def pullback_setup(turn=3.0):
    """A leader dips for four days to its 20-day average, then closes above the prior high."""
    closes = [100 + i * 0.5 for i in range(250)]
    peak = closes[-1]
    closes += [peak - 3, peak - 6, peak - 8, peak - 9, peak - 9 + turn]
    return make_history(closes, highs=[c + 0.5 for c in closes], lows=[c - 0.5 for c in closes])


def market_like(history, rising=True):
    """A benchmark covering the same days, above (or below) its 200-day average at the end."""
    n = len(history.dates)
    closes = [100 + i * 0.1 for i in range(n)] if rising else [300 - i * 0.1 for i in range(n)]
    benchmark = make_history(closes)
    benchmark.symbol = "SPY"
    return benchmark


class IndicatorTests(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(indicators.sma([1, 2, 3, 4, 5], 3), [None, None, 2, 3, 4])

    def test_rsi_extremes(self):
        self.assertEqual(indicators.rsi(list(range(1, 40)))[-1], 100)
        self.assertEqual(indicators.rsi(list(range(40, 1, -1)))[-1], 0)

    def test_rsi_known_value(self):
        # Reference series from Wilder's worked example; RSI(14) starts at ~70.5.
        closes = [44.34, 44.09, 44.15, 43.61, 44.33, 44.83, 45.10, 45.42, 45.84,
                  46.08, 45.89, 46.03, 45.61, 46.28, 46.28, 46.00, 46.03, 46.41]
        out = indicators.rsi(closes)
        self.assertAlmostEqual(out[14], 70.46, places=1)
        self.assertAlmostEqual(out[15], 66.25, places=1)

    def test_two_day_rsi_reacts_fast(self):
        out = indicators.rsi(leader(40) + [100.0, 98.0], 2)
        self.assertEqual(out[39], 100)
        self.assertLess(out[-1], 5)

    def test_atr_constant_range(self):
        n = 30
        out = indicators.atr([11.0] * n, [9.0] * n, [10.0] * n)
        self.assertAlmostEqual(out[-1], 2.0)


class StrategyTests(unittest.TestCase):
    def test_quiet_charts_have_no_signals(self):
        steady = leader()
        self.assertEqual(kinds(make_history(steady, highs=[c * 1.01 for c in steady])), set())
        self.assertEqual(kinds(make_history([100.0] * 300)), set())
        self.assertEqual(scan(make_history([100.0] * 30), CONFIG), [])   # too little history

    def test_dip_in_a_leader(self):
        signal = find(dip_setup(), "dip")
        self.assertEqual((signal.strategy, signal.direction, signal.title), ("dip", "long", "Dip in a leader"))
        self.assertRegex(signal.reason, r"2-day RSI has dropped to \d \(below 10\)")
        # One down day is not enough, and a stock that is not a leader does not count.
        self.assertNotIn("dip", kinds(dip_setup(drops=(0.985,))))
        choppy = make_history([100 + (i % 9) for i in range(298)] + [100.0, 97.0])
        self.assertNotIn("dip", kinds(choppy))
        anywhere = {"signals": {**CONFIG["signals"], "dip": {"enabled": True, "leaders_only": False}}}
        self.assertNotIn("dip", kinds(choppy, anywhere))   # still needs to be above its 200-day average

    def test_dip_plan_aims_at_the_average(self):
        history = dip_setup()
        plan = find(history, "dip").plan
        atr = indicators.atr(history.highs, history.lows, history.closes)[-1]
        average = indicators.sma(history.closes, 10)[-1]
        self.assertAlmostEqual(plan.entry - plan.stop, 2 * atr)
        self.assertEqual(plan.stop_basis, "2 ATR below entry")
        self.assertEqual(len(plan.targets), 1)
        self.assertAlmostEqual(plan.targets[0], average)
        self.assertGreater(plan.targets[0], plan.entry)
        self.assertEqual((plan.exit_kind, plan.exit_period), ("recover", 10))
        self.assertEqual(plan.exit_text, "Take the profit when a day closes back above the 10-day average.")

    def test_gap_on_heavy_volume(self):
        history = gap_setup()
        signal = find(history, "gap_up")
        self.assertEqual((signal.strategy, signal.title), ("gap", "Gap up on heavy volume"))
        self.assertIn("Opened 3.0% above yesterday's close on 3.0x average volume", signal.reason)
        self.assertIn("the day's low at 102.90", signal.plan.stop_basis)
        self.assertEqual((signal.plan.exit_kind, signal.plan.exit_period), ("lowest", 20))
        self.assertEqual(signal.plan.exit_text, "Exit when a day closes below the low of the 20 days before it.")
        # Not on thin volume, not when the gap was filled, not when it sold off after the open.
        self.assertNotIn("gap_up", kinds(gap_setup(volume=1_500_000.0)))
        self.assertNotIn("gap_up", kinds(gap_setup(low=99.9)))
        self.assertNotIn("gap_up", kinds(gap_setup(open_=104.0, close=102.95)))
        self.assertNotIn("gap_up", kinds(gap_setup(open_=101.0, close=101.5, low=100.9)))   # too small

    def test_base_breakout(self):
        signal = find(base_setup(), "base_breakout")
        self.assertEqual(signal.title, "Base breakout")
        self.assertRegex(signal.reason, r"closed above the top of its 25-day base \(\d+\.\d\d\) on 2\.0x average volume")
        self.assertEqual((signal.plan.exit_kind, signal.plan.exit_period), ("sma", 20))
        self.assertLessEqual(signal.plan.entry - signal.plan.stop, 0.08 * signal.plan.entry + 1e-9)
        self.assertNotIn("base_breakout", kinds(base_setup(volume=1_100_000.0)))   # no volume
        self.assertNotIn("base_breakout", kinds(base_setup(final=1.06)))           # too far past the base
        self.assertNotIn("base_breakout", kinds(base_setup(final=0.98)))           # not through it

    def test_pullback_with_a_turn(self):
        signal = find(pullback_setup(), "pullback")
        self.assertEqual(signal.title, "Pullback in an uptrend")
        self.assertRegex(signal.reason, r"pulled back to its (20|50)-day average")
        self.assertIn("the pullback low (215.00)", signal.plan.stop_basis)
        self.assertEqual((signal.plan.exit_kind, signal.plan.exit_period), ("lowest", 10))
        self.assertNotIn("pullback", kinds(pullback_setup(turn=0.5)))   # no close above the prior high
        falling = make_history([300 - i * 0.5 for i in range(250)] + [180.0])
        self.assertNotIn("pullback", kinds(falling))

    def test_55_day_breakout(self):
        history = flat_breakout()
        signal = find(history, "channel_breakout")
        atr = indicators.atr(history.highs, history.lows, history.closes)[-1]
        self.assertEqual(signal.title, "55-day breakout")
        self.assertEqual(signal.reason, "Price closed above its 55-day high of 100.50.")
        self.assertAlmostEqual(signal.plan.entry - signal.plan.stop, 2 * atr)
        self.assertEqual(signal.plan.stop_basis, "2 ATR below entry")
        self.assertAlmostEqual(signal.plan.targets[0], 103 + 4 * atr)
        self.assertAlmostEqual(signal.plan.targets[1], 103 + 6 * atr)
        self.assertEqual((signal.plan.exit_kind, signal.plan.exit_period), ("lowest", 20))
        # Only the first day through counts.
        second_day = make_history([100.0] * 258 + [103.0, 106.0])
        self.assertNotIn("channel_breakout", kinds(second_day))

    def test_signal_fields(self):
        history = flat_breakout()
        signal = find(history, "channel_breakout")
        self.assertEqual((signal.symbol, signal.price, signal.date), ("TEST", 103.0, history.dates[-1]))
        self.assertAlmostEqual(signal.change_pct, 3.0)
        self.assertIn("RSI(14)", signal.facts)
        self.assertIn("200-day avg", signal.facts)

    def test_volume_of_an_unfinished_day_is_projected(self):
        def at(fraction):
            history = gap_setup(volume=1_000_000.0)   # average volume so far...
            history.session_fraction = fraction
            return kinds(history)
        self.assertIn("gap_up", at(0.25))      # ...by a quarter of the day is four times the pace
        self.assertNotIn("gap_up", at(1.0))
        self.assertNotIn("gap_up", at(0.05))   # too early to judge volume at all

    def test_unproven_strategies_are_off_unless_asked_for(self):
        self.assertEqual(UNPROVEN, {"base_breakout", "pullback", "channel_breakout"})
        history = flat_breakout()
        self.assertEqual(kinds(history, {}), {"gap_up"})                          # the default set
        self.assertEqual({s.kind for s in scan(history, {}, only="channel_breakout")}, {"channel_breakout"})
        self.assertEqual({s.kind for s in scan(history, {}, only="gap")}, {"gap_up"})
        off = {"signals": {"gap": {"enabled": False}}}
        self.assertEqual(kinds(history, off), set())
        self.assertEqual({s.kind for s in scan(history, off, only="gap")}, {"gap_up"})   # asked for by name


class MarketFilterTests(unittest.TestCase):
    def test_filtered_strategies_go_long_only_in_a_rising_market(self):
        history = flat_breakout()
        self.assertEqual(kinds(history, benchmark=market_like(history, rising=True)), {"gap_up", "channel_breakout"})
        # In a falling market the breakout waits; the gap does not use the filter.
        self.assertEqual(kinds(history, benchmark=market_like(history, rising=False)), {"gap_up"})
        # Without a benchmark the filter cannot judge and lets the signal through.
        self.assertIn("channel_breakout", kinds(history))

    def test_filtered_strategies_go_short_only_in_a_falling_market(self):
        history = mirrored(flat_breakout())
        self.assertEqual(kinds(history, benchmark=market_like(history, rising=False)), {"gap_down", "channel_breakdown"})
        self.assertEqual(kinds(history, benchmark=market_like(history, rising=True)), {"gap_down"})

    def test_filter_settings(self):
        history = flat_breakout()
        falling = market_like(history, rising=False)
        everything_off = {**CONFIG, "market": {"filter": False}}
        self.assertEqual(kinds(history, everything_off, falling), {"gap_up", "channel_breakout"})
        gap_filtered = {"signals": {**CONFIG["signals"], "gap": {"enabled": True, "market_filter": True}}}
        self.assertEqual(kinds(history, gap_filtered, falling), set())

    def test_dips_wait_for_the_market(self):
        history = dip_setup()
        slower = lambda rising: make_history(
            [100 * 1.0005 ** i for i in range(len(history.dates))] if rising
            else [300 * 0.9995 ** i for i in range(len(history.dates))])
        self.assertIn("dip", kinds(history, benchmark=slower(True)))
        self.assertNotIn("dip", kinds(history, benchmark=slower(False)))

    def test_a_leader_must_not_be_weaker_than_the_market(self):
        history = dip_setup()
        slower = make_history([100 * 1.0005 ** i for i in range(len(history.dates))])
        faster = make_history([100 * 1.004 ** i for i in range(len(history.dates))])
        self.assertIn("dip", kinds(history, benchmark=slower))
        self.assertNotIn("dip", kinds(history, benchmark=faster))

    def test_market_trend_by_day(self):
        from trader.signals import market_trend
        benchmark = make_history([100.0] * 250 + [90.0, 110.0])
        trend = market_trend(benchmark)
        self.assertEqual(len(trend), 53)                      # known once 200 days exist
        self.assertEqual(list(trend.values())[-2:], [False, True])
        self.assertEqual(market_trend(None), {})


class PlanTests(unittest.TestCase):
    def test_stop_sits_beyond_the_setup_level_within_limits(self):
        history = pullback_setup()
        atr = indicators.atr(history.highs, history.lows, history.closes)[-1]
        plan = find(history, "pullback").plan
        self.assertAlmostEqual(plan.stop, 215.0 - 0.25 * atr)   # a quarter ATR below the pullback low
        tight = {**CONFIG, "plan": {"min_stop_atr": 5.0, "max_stop_atr": 6.0}}
        plan = find(history, "pullback", tight).plan
        self.assertAlmostEqual(plan.risk, 5 * atr)
        self.assertIn("too close", plan.stop_basis)
        wide = {**CONFIG, "plan": {"min_stop_atr": 0.5, "max_stop_atr": 1.0}}
        plan = find(history, "pullback", wide).plan
        self.assertAlmostEqual(plan.risk, 1 * atr)
        self.assertIn("too far", plan.stop_basis)

    def test_custom_targets(self):
        plan = find(flat_breakout(), "channel_breakout", {**CONFIG, "plan": {"targets_r": [1.5]}}).plan
        self.assertEqual(len(plan.targets), 1)
        self.assertAlmostEqual(plan.targets[0], plan.entry + 1.5 * plan.risk)

    def test_percentage_limit_on_the_stop(self):
        history = base_setup()
        atr = indicators.atr(history.highs, history.lows, history.closes)[-1]
        config = {"signals": {"base_breakout": {"enabled": True, "max_stop_pct": 2.0}}}
        plan = find(history, "base_breakout", config).plan
        self.assertAlmostEqual(plan.risk, 0.02 * plan.entry)
        self.assertIn("2% below entry", plan.stop_basis)
        # A limit tighter than one ATR of everyday noise is not applied.
        config = {"signals": {"base_breakout": {"enabled": True, "max_stop_pct": 0.5}}}
        self.assertGreater(find(history, "base_breakout", config).plan.risk, atr)


class ShortTests(unittest.TestCase):
    """Short signals are the long ones upside down."""

    PAIRS = {"dip": "pop", "gap_up": "gap_down", "base_breakout": "base_breakdown",
             "pullback": "rally", "channel_breakout": "channel_breakdown"}

    def test_every_long_signal_has_a_mirrored_short(self):
        seen = set()
        for history in (dip_setup(), gap_setup(), base_setup(), pullback_setup(), flat_breakout()):
            longs = {s.kind for s in scan(history, CONFIG) if s.direction == "long"}
            shorts = {s.kind for s in scan(mirrored(history), CONFIG) if s.direction == "short"}
            self.assertEqual({self.PAIRS[kind] for kind in longs}, shorts)
            seen |= longs
        self.assertEqual(seen, set(self.PAIRS))  # every kind was exercised

    def test_short_plan_is_the_long_plan_upside_down(self):
        history = flat_breakout()
        long_plan = find(history, "channel_breakout").plan
        signal = find(mirrored(history), "channel_breakdown")
        plan, top = signal.plan, 2 * max(history.highs)
        self.assertEqual((signal.direction, plan.direction), ("short", "short"))
        self.assertAlmostEqual(plan.entry, top - long_plan.entry)
        self.assertAlmostEqual(plan.stop, top - long_plan.stop)
        self.assertAlmostEqual(plan.targets[1], top - long_plan.targets[1])
        self.assertGreater(plan.stop, plan.entry)        # stop above
        self.assertLess(plan.targets[0], plan.entry)     # targets below
        self.assertEqual(plan.stop_basis, "2 ATR above entry")
        self.assertEqual(signal.reason, "Price closed below its 55-day low of 106.53.")
        self.assertEqual(plan.exit_text, "Exit when a day closes above the high of the 20 days before it.")

    def test_short_wording(self):
        pop = find(mirrored(dip_setup()), "pop")
        self.assertEqual(pop.title, "Pop in a laggard")
        self.assertRegex(pop.reason, r"2-day RSI has spiked to 9\d \(above 90\)")
        self.assertEqual(pop.plan.exit_text, "Take the profit when a day closes back below the 10-day average.")
        self.assertLess(pop.plan.targets[0], pop.plan.entry)
        gap = find(mirrored(gap_setup()), "gap_down")
        self.assertIn("below yesterday's close", gap.reason)
        self.assertIn("the day's high", gap.plan.stop_basis)

    def test_directions_can_be_switched_off(self):
        history, flipped = flat_breakout(), mirrored(flat_breakout())
        no_shorts = {"signals": {**CONFIG["signals"], "short": False}}
        no_longs = {"signals": {**CONFIG["signals"], "long": False}}
        self.assertEqual({s.direction for s in scan(flipped, no_shorts)}, set())
        self.assertEqual({s.direction for s in scan(history, no_longs)}, set())
        self.assertEqual({s.direction for s in scan(flipped, no_longs)}, {"short"})

    def test_email_marks_the_direction(self):
        signals = [find(mirrored(flat_breakout()), "channel_breakdown")]
        message = emailer.build_message(signals, "me@example.com", "me@example.com")
        self.assertEqual(message["Subject"], "Entry alert: TEST short")
        self.assertIn("TEST (Test Corp) - SHORT", message.get_body(("plain",)).get_content())
        self.assertIn("Short entries had no reliable edge in testing", message.get_body(("plain",)).get_content())
        self.assertIn(">SHORT</span>", message.get_body(("html",)).get_content())


def hourly_history(closes, volumes=None):
    """Candles one hour apart, seven a day, starting on a Monday."""
    history = make_history(closes, volumes=volumes)
    day = date(2025, 1, 6)
    stamps = []
    while len(stamps) < len(closes):
        if day.weekday() < 5:
            stamps += [f"{day.isoformat()} {hour:02d}:30" for hour in range(9, 16)]
        day += timedelta(days=1)
    history.dates = stamps[:len(closes)]
    return history


def hourly_breakout(after=()):
    closes = [100.0] * 259 + [103.0] + list(after)
    return hourly_history(closes, volumes=[1_000_000.0] * 259 + [2_000_000.0] + [1_000_000.0] * len(after))


class HourlyTests(unittest.TestCase):
    def test_hourly_candles_use_hourly_wording(self):
        history = hourly_breakout()
        self.assertTrue(history.hourly)
        self.assertFalse(make_history([100.0] * 60).hourly)
        signals = {s.kind: s for s in scan(history, CONFIG)}
        self.assertEqual(signals["channel_breakout"].title, "55-hour breakout")
        self.assertIn("55-hour high", signals["channel_breakout"].reason)
        self.assertIn("above the previous hour's close", signals["gap_up"].reason)
        self.assertEqual(signals["gap_up"].plan.exit_text,
                         "Exit when a hour closes below the low of the 20 hours before it.")
        self.assertEqual(signals["gap_up"].date, history.dates[-1])
        self.assertIn("200-hour avg", signals["gap_up"].facts)
        message = emailer.build_message(list(signals.values()), "a@b.c", "a@b.c")
        self.assertEqual(message["Subject"], "Entry alert (hourly): TEST long")

    def test_market_filter_uses_the_day_of_the_hour(self):
        history = hourly_breakout()
        days = sorted({stamp[:10] for stamp in history.dates})
        falling = make_history([300 - i * 0.1 for i in range(260)])
        falling.dates = [(date.fromisoformat(days[-1]) - timedelta(days=259 - i)).isoformat() for i in range(260)]
        self.assertEqual(kinds(history, benchmark=falling), {"gap_up"})   # the breakout waits

    def test_change_is_measured_from_the_previous_day(self):
        from trader.signals import day_change
        closes = [100.0] * 63 + [101.0, 102.0, 110.0]      # 9 full days, then 3 hours of the 10th
        history = hourly_history(closes)
        self.assertEqual(history.dates[-1][11:], "11:30")
        self.assertAlmostEqual(day_change(history), 10.0)   # against yesterday's close of 100
        self.assertAlmostEqual(day_change(make_history([100.0, 110.0])), 10.0)

    def test_hourly_alerts_have_their_own_cooldown(self):
        signal = find(hourly_breakout(), "channel_breakout")
        with tempfile.TemporaryDirectory() as tmp:
            state = AlertState(Path(tmp) / "alerts.json")
            signal.date = "2025-03-03 10:30"
            state.record([signal])
            self.assertEqual(list(state.sent), ["TEST:channel_breakout:1h"])
            signal.date = "2025-03-03 15:30"
            self.assertFalse(state.is_new(signal, 5, cooldown_hours=24))
            signal.date = "2025-03-04 10:30"
            self.assertTrue(state.is_new(signal, 5, cooldown_hours=24))
            self.assertFalse(state.is_new(signal, 5, cooldown_hours=48))
            # A daily signal of the same kind is tracked separately.
            signal.date = "2025-03-03"
            self.assertTrue(state.is_new(signal, 5))

    def test_the_forming_hour_is_measured(self):
        at = lambda *parts: datetime(*parts, tzinfo=timezone.utc)
        # 23 November 2026 is winter time: 10:30 New York is 15:30 UTC.
        self.assertAlmostEqual(market.candle_fraction("2026-11-23 10:30", at(2026, 11, 23, 15, 45)), 0.25)
        self.assertEqual(market.candle_fraction("2026-11-23 10:30", at(2026, 11, 23, 16, 30)), 1.0)
        self.assertEqual(market.candle_fraction("2026-11-20 10:30", at(2026, 11, 23, 15, 45)), 1.0)
        # The last candle of the day lasts 30 minutes.
        self.assertAlmostEqual(market.candle_fraction("2026-11-23 15:30", at(2026, 11, 23, 20, 45)), 0.5)
        self.assertAlmostEqual(market.candle_fraction("2026-11-23", at(2026, 11, 23, 16, 7, 30)), 0.25)

    def test_backtest_on_hourly_candles_counts_days_as_seven_hours(self):
        from trader import backtest
        history = hourly_breakout(after=[103.2] * 30)
        def run(hold, unit):
            options = backtest.Options(strategy="channel_breakout", years=0.1, hold=hold, hold_unit=unit)
            return backtest.backtest_stock(history, CONFIG, options)[0]
        self.assertEqual((run(3, "hours").reason, run(3, "hours").held), ("time", 3))
        self.assertEqual((run(2, "days").reason, run(2, "days").held), ("time", 14))
        self.assertEqual(run(3, "hours").entry_date, history.dates[259])


class BacktestTests(unittest.TestCase):
    def after_breakout(self, later, short=False, **options):
        """The 55-day breakout at 103, followed by the given (open, high, low, close) days."""
        from trader import backtest
        history = flat_breakout()
        plan = find(history, "channel_breakout").plan
        for i, (open_, high, low, close) in enumerate(later):
            history.dates.append((date(2025, 1, 1) + timedelta(days=i)).isoformat())
            history.opens.append(open_); history.highs.append(high)
            history.lows.append(low); history.closes.append(close)
            history.volumes.append(1_000_000.0)
        if short:
            history = mirrored(history)
        settings = dict(strategy="channel_breakout", direction="short" if short else "long",
                        years=0.1, exit="tp1", hold=3)
        settings.update(options)
        trades = backtest.backtest_stock(history, CONFIG, backtest.Options(**settings))
        return plan, trades[0]

    def test_target_hit(self):
        plan, trade = self.after_breakout([(103, 104, 102.5, 103.5), (104, 110, 103.5, 109)])
        self.assertEqual((trade.reason, trade.held), ("target", 2))
        self.assertAlmostEqual(trade.exit, plan.targets[0])
        self.assertAlmostEqual(trade.r, 2.0)
        _, second = self.after_breakout([(103, 104, 102.5, 103.5), (104, 111, 103.5, 110)], exit="tp2")
        self.assertAlmostEqual(second.r, 3.0)

    def test_stop_hit(self):
        plan, trade = self.after_breakout([(103, 103.5, 99, 100)])
        self.assertEqual(trade.reason, "stop")
        self.assertAlmostEqual(trade.exit, plan.stop)
        self.assertAlmostEqual(trade.r, -1.0)

    def test_stop_wins_when_both_are_touched_the_same_day(self):
        _, trade = self.after_breakout([(103, 115, 95, 110)])
        self.assertEqual(trade.reason, "stop")
        self.assertAlmostEqual(trade.r, -1.0)

    def test_gaps_exit_at_the_open(self):
        _, stopped = self.after_breakout([(97, 98, 96, 97)])
        self.assertEqual((stopped.reason, stopped.exit), ("stop", 97))
        self.assertLess(stopped.r, -1.0)
        _, paid = self.after_breakout([(112, 113, 111, 112)])
        self.assertEqual((paid.reason, paid.exit), ("target", 112))
        self.assertGreater(paid.r, 2.0)

    def test_time_limit_and_no_time_limit(self):
        quiet = [(103, 104, 102, 103.5)] * 5
        _, trade = self.after_breakout(quiet)
        self.assertEqual((trade.reason, trade.held, trade.exit), ("time", 3, 103.5))
        _, unlimited = self.after_breakout(quiet, hold=0)
        self.assertEqual(unlimited.reason, "open")

    def test_trade_still_open_at_the_end(self):
        from trader import backtest
        _, trade = self.after_breakout([(103, 104, 102, 103.5)])
        self.assertEqual(trade.reason, "open")
        self.assertEqual(backtest.summarize([trade]), {"trades": 0, "open": 1, "profit": 0.0})

    def test_exit_rule_lets_a_winner_run_until_its_trend_breaks(self):
        # Up for 25 days, far past both targets, then a slide below the 20-day low.
        climb = [(103 + i, 104.5 + i, 102.5 + i, 104 + i) for i in range(25)]
        slide = [(128 - 2 * i, 128.5 - 2 * i, 125.5 - 2 * i, 126 - 2 * i) for i in range(1, 12)]
        plan, trade = self.after_breakout(climb + slide, exit="rule", hold=0, years=0.5)
        self.assertEqual(trade.reason, "rule")
        self.assertGreater(trade.r, 3.0)                     # more than TP2 would have paid
        self.assertLess(trade.exit, 128)                     # but below the top: the price of waiting
        _, capped = self.after_breakout(climb + slide, years=0.5, hold=0)   # the same days with a fixed target
        self.assertEqual(capped.reason, "target")
        self.assertAlmostEqual(capped.r, 2.0)
        # The stop still applies while the rule waits.
        _, stopped = self.after_breakout([(103, 103.5, 99, 100)], exit="rule", hold=0)
        self.assertEqual(stopped.reason, "stop")

    def test_dip_is_closed_when_it_recovers_above_its_average(self):
        from trader import backtest
        history = dip_setup()
        last = history.closes[-1]
        for i, close in enumerate([last * 1.004, last * 1.01, last * 1.03, last * 1.05]):
            history.dates.append((date(2025, 6, 1) + timedelta(days=i)).isoformat())
            history.opens.append(close); history.highs.append(close * 1.002)
            history.lows.append(close * 0.998); history.closes.append(close)
            history.volumes.append(1_000_000.0)
        options = backtest.Options(strategy="dip", years=0.1, hold=0)
        trade = backtest.backtest_stock(history, CONFIG, options)[0]
        self.assertEqual((trade.title, trade.reason), ("Dip in a leader", "rule"))
        average = indicators.sma(history.closes, 10)
        day = history.dates.index(trade.exit_date)
        self.assertGreater(history.closes[day], average[day])           # closed back above it...
        self.assertLessEqual(history.closes[day - 1], average[day - 1])  # ...for the first time
        self.assertGreater(trade.r, 0)

    def test_short_trade_is_the_long_trade_upside_down(self):
        days = [(103, 104, 102.5, 103.5), (104, 110, 103.5, 109)]
        _, long_trade = self.after_breakout(days)
        _, short_trade = self.after_breakout(days, short=True)
        self.assertEqual((short_trade.direction, short_trade.reason), ("short", "target"))
        self.assertAlmostEqual(short_trade.r, long_trade.r)
        self.assertLess(short_trade.exit, short_trade.entry)

    def test_only_the_chosen_strategy_and_direction_are_traded(self):
        from trader import backtest
        history = flat_breakout()
        for i in range(5):
            history.dates.append((date(2025, 1, 1) + timedelta(days=i)).isoformat())
            for series, value in ((history.opens, 103.0), (history.highs, 103.5), (history.lows, 102.6),
                                  (history.closes, 103.0), (history.volumes, 1_000_000.0)):
                series.append(value)
        def traded(**options):
            trades = backtest.backtest_stock(history, {}, backtest.Options(years=0.1, **options))
            return {(t.strategy, t.direction) for t in trades}
        self.assertEqual(traded(), {("gap", "long")})                                   # the tested set
        self.assertEqual(traded(strategy="channel_breakout"), {("channel_breakout", "long")})
        self.assertEqual(traded(strategy="gap", direction="short"), set())

    def test_market_filter_applies_to_past_days(self):
        from trader import backtest
        history = flat_breakout()
        options = backtest.Options(strategy="channel_breakout", years=0.1)
        self.assertTrue(backtest.backtest_stock(history, {}, options, benchmark=market_like(history, True)))
        self.assertEqual(backtest.backtest_stock(history, {}, options, benchmark=market_like(history, False)), [])

    def test_past_days_scan_exactly_like_live_days(self):
        import math
        closes = [100 * 1.001 ** i + 12 * math.sin(i / 9) + (i % 7) * 0.4 for i in range(520)]
        volumes = [1_000_000.0 * (1 + (i % 11) / 3) for i in range(520)]
        opens = [c - 1.5 * math.cos(i / 5) for i, c in enumerate(closes)]
        history = make_history(closes, volumes=volumes, opens=opens)
        benchmark = make_history([100 + i * 0.05 + 6 * math.sin(i / 40) for i in range(520)])
        analysis, compared = Analysis(history, benchmark), 0
        describe = lambda s: (s.kind, s.reason, s.plan.stop, s.plan.targets, s.plan.exit_text)
        for end in range(260, 520):
            part = make_history(closes[:end], volumes=volumes[:end], opens=opens[:end])
            cut = make_history(benchmark.closes[:end])
            live = [describe(s) for s in scan(part, CONFIG, cut)]
            past = [describe(s) for s in analysis.scan(CONFIG, end=end)]
            self.assertEqual(live, past)
            compared += len(live)
        self.assertGreater(compared, 5)

    def test_budget_follows_the_trades(self):
        from trader import backtest
        def trade(entry_day, exit_day, r, stop=95.0):
            won = r > 0
            return backtest.Trade("T", "gap", "Gap", "long", f"2025-01-{entry_day:02d}", 100, stop, 110,
                                  f"2025-01-{exit_day:02d}", 100 + (100 - stop) * r, "target" if won else "stop",
                                  r, (100 - stop) * r, exit_day - entry_day)
        # 1% risk of 10,000 = 100 per trade; the SL is 5 away, so 20 shares.
        first, second, third = trade(1, 5, 2), trade(2, 6, -1), trade(7, 9, -1)
        backtest.apply_budget([first, second, third], 10_000, 1)
        self.assertAlmostEqual(first.shares, 20)
        self.assertAlmostEqual(first.pnl, 200)
        # The second trade starts before the first ends, so it is sized on 10,000 too.
        self.assertAlmostEqual(second.pnl, -100)
        # The third starts after both: budget 10,100, so it risks 101.
        self.assertAlmostEqual(third.pnl, -101)
        stats = backtest.summarize([first, second, third], 10_000)
        self.assertAlmostEqual(stats["end_budget"], 9_999)
        self.assertAlmostEqual(stats["return_pct"], -0.01)
        self.assertAlmostEqual(stats["max_drawdown_pct"], 201 / 10_200 * 100)

    def test_position_is_never_larger_than_the_budget(self):
        from trader import backtest
        tight = backtest.Trade("T", "gap", "Gap", "long", "2025-01-01", 100, 99.9, 101,
                               "2025-01-02", 99.9, "stop", -1, -0.1, 1)
        backtest.apply_budget([tight], 10_000, 1)
        # Risking 100 with the SL only 0.10 away would need 1,000 shares = 100,000.
        self.assertAlmostEqual(tight.shares, 100)
        self.assertAlmostEqual(tight.pnl, -10)

    def test_hours_are_followed_on_hourly_candles(self):
        from trader import backtest
        history = flat_breakout()
        history.dates.append("2024-09-17"); history.opens.append(103); history.highs.append(111)
        history.lows.append(102); history.closes.append(110); history.volumes.append(1_000_000.0)
        signal_day = history.dates[-2]
        # Hourly prices are twice the daily ones (as if not adjusted): 206 is the entry of 103.
        stamps = [f"{signal_day} 15:30", "2024-09-17 09:30", "2024-09-17 10:30", "2024-09-17 11:30"]
        hourly = History("TEST", "TEST", stamps, opens=[205, 206, 208, 209], highs=[206, 209, 210, 223],
                         lows=[204, 205, 207, 208], closes=[206, 208, 209, 222], volumes=[1.0] * 4)
        def run(hold):
            options = backtest.Options(strategy="channel_breakout", years=0.1, exit="tp1",
                                       hold=hold, hold_unit="hours")
            return backtest.backtest_stock(history, CONFIG, options, hourly)[0]
        two_hours = run(2)
        self.assertEqual((two_hours.reason, two_hours.held, two_hours.exit_date), ("time", 2, "2024-09-17 10:30"))
        self.assertAlmostEqual(two_hours.exit, 104.5)  # 209 at the hourly scale
        longer = run(20)
        self.assertEqual((longer.reason, longer.held), ("target", 3))
        self.assertAlmostEqual(longer.r, 2.0)
        # Days before the hourly candles begin are not tested.
        late = History("TEST", "TEST", stamps[1:], [206, 208, 209], [209, 210, 223], [205, 207, 208], [208, 209, 222], [1.0] * 3)
        options = backtest.Options(strategy="channel_breakout", years=0.1, hold=2, hold_unit="hours")
        self.assertEqual(backtest.backtest_stock(history, CONFIG, options, late), [])

    def test_summary_numbers(self):
        from trader import backtest
        def trade(r, day):
            return backtest.Trade("T", "gap", "Gap up on heavy volume", "long", "2025-01-01", 100, 95, 110,
                                  f"2025-02-{day:02d}", 100 + 5 * r, "target" if r > 0 else "stop", r, 5 * r, 4)
        stats = backtest.summarize([trade(2, 1), trade(-1, 2), trade(-1, 3), trade(2, 4)])
        self.assertEqual(stats["trades"], 4)
        self.assertAlmostEqual(stats["win_rate"], 50)
        self.assertAlmostEqual(stats["avg_r"], 0.5)
        self.assertAlmostEqual(stats["total_r"], 2)
        self.assertAlmostEqual(stats["profit_factor"], 2)
        self.assertAlmostEqual(stats["max_drawdown_r"], 2)
        report = backtest.report([trade(2, 1), trade(-1, 2)])
        self.assertEqual([p["r"] for p in report["curve"]], [2, 1])
        self.assertEqual(report["rows"][0]["name"], "Gap on heavy volume")


class UniverseTests(unittest.TestCase):
    def test_saved_lists(self):
        from trader import universe
        for name, size in (("nasdaq100", 90), ("sp500", 480), ("dowjones", 30)):
            symbols = universe._saved_index(name)
            self.assertGreaterEqual(len(symbols), size)
            self.assertTrue(all(universe.SYMBOL.match(symbol) for symbol in symbols))
        self.assertIn("BRK-B", universe._saved_index("sp500"))  # Yahoo's spelling, not BRK.B

    def test_download_with_fallback_to_the_saved_list(self):
        import io, urllib.error
        from unittest import mock
        from trader import universe
        listing = "Symbol,Name\n" + "".join(f"T{i},Company {i}\n" for i in range(25)) + "brk.b,Berkshire\nbad symbol!,X\n"
        with mock.patch.object(universe.urllib.request, "urlopen", lambda request, timeout=0: io.BytesIO(listing.encode())):
            symbols = universe.load("dowjones")
        self.assertEqual((len(symbols), symbols[0], symbols[-1]), (26, "T0", "BRK-B"))

        def offline(request, timeout=0):
            raise urllib.error.URLError("no network")
        with mock.patch.object(universe.urllib.request, "urlopen", offline):
            self.assertEqual(universe.load("dowjones"), universe._saved_index("dowjones"))
        with self.assertRaises(DataError):
            universe.load("mars")

    def test_batch_download_reports_each_stock(self):
        try:
            import pandas
        except ImportError:
            self.skipTest("pandas is not installed")
        import sys, types
        from unittest import mock
        from trader import data
        index = pandas.DatetimeIndex(["2024-01-02", "2024-01-03"], tz="America/New_York")
        fields = ["Open", "High", "Low", "Close", "Volume"]
        columns = pandas.MultiIndex.from_product([["AAA", "BBB"], fields])
        table = pandas.DataFrame([[1, 2, 0.5, 1.5, 10] + [None] * 5, [2, 3, 1.5, 2.5, 20] + [None] * 5],
                                 index=index, columns=columns)
        fake = types.SimpleNamespace(download=lambda symbols, **options: table)
        with mock.patch.dict(sys.modules, {"yfinance": fake}):
            found = dict(data.fetch_many(["AAA", "BBB", "CCC"]))
            with self.assertRaises(data.SetupError):
                list(data.fetch_many(["AAA"], source="twelvedata"))
        self.assertEqual(found["AAA"].closes, [1.5, 2.5])
        self.assertIsInstance(found["BBB"], DataError)   # all prices missing
        self.assertIsInstance(found["CCC"], DataError)   # not in the answer at all


class DataTests(unittest.TestCase):
    def payload(self):
        # Shape of a real Twelve Data /time_series response: newest first, numbers as strings.
        return {"meta": {"symbol": "AAPL", "interval": "1day", "exchange": "NASDAQ"},
                "values": [
                    {"datetime": "2024-01-04", "open": "12.0", "high": "13.0", "low": "11.0",
                     "close": "12.5", "volume": "300"},
                    {"datetime": "2024-01-03", "open": None, "high": None, "low": None,
                     "close": None, "volume": None},
                    {"datetime": "2024-01-02", "open": "10.0", "high": "11.0", "low": "9.0",
                     "close": "10.5", "volume": "100"},
                ], "status": "ok"}

    def test_parse_orders_oldest_first_and_skips_incomplete_rows(self):
        history = parse_time_series("AAPL", self.payload())
        self.assertEqual(history.dates, ["2024-01-02", "2024-01-04"])
        self.assertEqual(history.closes, [10.5, 12.5])
        self.assertEqual(history.volumes, [100.0, 300.0])
        self.assertEqual(history.session_fraction, 1.0)

    def test_parse_empty_payload(self):
        with self.assertRaises(DataError):
            parse_time_series("NOPE", {"values": [], "status": "ok"})

    def fetch_with(self, *responses):
        """Run fetch_history against canned API responses."""
        import io, json, os, urllib.error
        from unittest import mock
        from trader import data
        queue = list(responses)
        def fake_urlopen(request, timeout=0):
            self.assertEqual(request.get_header("Authorization"), "apikey test-key")
            self.assertNotIn("test-key", request.full_url)
            body = queue.pop(0)
            status = body.get("code", 200)
            raw = io.BytesIO(json.dumps(body).encode())
            if status != 200:
                raise urllib.error.HTTPError(request.full_url, status, "error", {}, raw)
            return raw
        with mock.patch.dict(os.environ, {"TWELVE_DATA_API_KEY": "test-key"}), \
             mock.patch.object(data.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.object(data.time, "sleep") as sleep:
            return data.fetch_history("AAPL", source="twelvedata"), sleep

    def test_fetch_success(self):
        history, _ = self.fetch_with(self.payload())
        self.assertEqual(history.closes[-1], 12.5)

    def test_fetch_waits_out_the_per_minute_limit(self):
        limit = {"code": 429, "status": "error",
                 "message": "You have run out of API credits for the current minute."}
        history, sleep = self.fetch_with(limit, self.payload())
        self.assertEqual(history.closes[-1], 12.5)
        self.assertEqual(sleep.call_count, 1)

    def test_fetch_rejected_key(self):
        with self.assertRaises(ApiKeyError):
            self.fetch_with({"code": 401, "status": "error", "message": "apikey is incorrect"})

    def test_fetch_unknown_symbol(self):
        with self.assertRaises(DataError) as raised:
            self.fetch_with({"code": 404, "status": "error", "message": "symbol not found"})
        self.assertIn("symbol not found", str(raised.exception))

    def test_yahoo_table_is_converted(self):
        try:
            import pandas
        except ImportError:
            self.skipTest("pandas is not installed")
        from trader import data
        index = pandas.DatetimeIndex(["2024-01-02", "2024-01-03", "2024-01-04"], tz="America/New_York")
        frame = pandas.DataFrame({
            "Open": [10.0, None, 12.0], "High": [11.0, None, 13.0], "Low": [9.0, None, 11.0],
            "Close": [10.5, None, 12.5], "Volume": [100, None, 300], "Dividends": [0, 0, 0],
        }, index=index)
        history = data.history_from_frame("AAPL", frame)
        self.assertEqual(history.dates, ["2024-01-02", "2024-01-04"])
        self.assertEqual(history.closes, [10.5, 12.5])
        self.assertEqual(history.volumes, [100.0, 300.0])
        with self.assertRaises(DataError):
            data.history_from_frame("NOPE", frame.iloc[0:0])

    def test_unknown_source(self):
        from trader import data
        with self.assertRaises(data.SetupError):
            data.fetch_history("AAPL", source="somewhere")

    def test_missing_key(self):
        import os
        from unittest import mock
        from trader import data
        with mock.patch.dict(os.environ, {"TWELVE_DATA_API_KEY": ""}):
            with self.assertRaises(ApiKeyError):
                data.fetch_history("AAPL", source="twelvedata")


class StateAndEmailTests(unittest.TestCase):
    def signal(self, day="2024-06-10"):
        signal = find(flat_breakout(), "channel_breakout")
        signal.date = day
        return signal

    def test_cooldown_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "state" / "alerts.json"
            state = AlertState(path)
            first = self.signal(date.today().isoformat())
            self.assertTrue(state.is_new(first, 5))
            state.record([first])
            state.save()

            reloaded = AlertState(path)
            self.assertFalse(reloaded.is_new(first, 5))
            soon = self.signal((date.today() + timedelta(days=4)).isoformat())
            later = self.signal((date.today() + timedelta(days=5)).isoformat())
            self.assertFalse(reloaded.is_new(soon, 5))
            self.assertTrue(reloaded.is_new(later, 5))

    def test_corrupt_state_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "alerts.json"
            path.write_text("not json")
            self.assertTrue(AlertState(path).is_new(self.signal(), 5))

    def test_email_contents(self):
        message = emailer.build_message([self.signal()], "me@example.com", "me@example.com")
        self.assertEqual(message["Subject"], "Entry alert: TEST long")
        text = message.get_body(("plain",)).get_content()
        html = message.get_body(("html",)).get_content()
        for body in (text, html):
            self.assertIn("TEST", body)
            self.assertIn("55-day breakout", body)
            self.assertIn("103.00", body)
            self.assertIn("financial advice", body)
            for label in ("Entry", "SL", "TP1", "TP2", "107.72", "2 ATR below entry",
                          "Exit when a day closes below the low of the 20 days before it."):
                self.assertIn(label, body)

    def test_email_for_a_dip_names_its_target(self):
        text = emailer.build_message([find(dip_setup(), "dip")], "a@b.c", "a@b.c").get_body(("plain",)).get_content()
        self.assertIn("TP:", text)
        self.assertNotIn("TP1", text)
        self.assertIn("TP is where the 10-day average stands now", text)
        self.assertIn("Take the profit when a day closes back above the 10-day average.", text)
        self.assertRegex(text, r"This plan risks \d+\.\d\d to make about \d+\.\d\d per share")
        self.assertNotIn("Short entries", text)

    def test_watchlist_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "watchlist.txt"
            path.write_text("# my stocks\naapl\n\nMSFT  # big one\nAAPL\n")
            self.assertEqual(load_watchlist(path), ["AAPL", "MSFT"])


class MarketHoursTests(unittest.TestCase):
    def utc(self, *parts):
        return datetime(*parts, tzinfo=timezone.utc)

    def test_new_york_offset_follows_daylight_time(self):
        # 2026: daylight time starts 8 March and ends 1 November.
        self.assertEqual(market.new_york_time(self.utc(2026, 1, 15, 15, 0)).hour, 10)
        self.assertEqual(market.new_york_time(self.utc(2026, 7, 15, 15, 0)).hour, 11)
        self.assertEqual(market.new_york_time(self.utc(2026, 3, 8, 6, 59)).hour, 1)
        self.assertEqual(market.new_york_time(self.utc(2026, 3, 8, 7, 0)).hour, 3)
        self.assertEqual(market.new_york_time(self.utc(2026, 11, 1, 5, 59)).hour, 1)
        self.assertEqual(market.new_york_time(self.utc(2026, 11, 1, 6, 0)).hour, 1)
        self.assertEqual(market.new_york_time(self.utc(2026, 11, 23, 15, 0)).hour, 10)

    def test_scan_window(self):
        # Monday 23 November 2026, winter time: the session is 14:30-21:00 UTC.
        self.assertFalse(market.is_scan_time(15, self.utc(2026, 11, 23, 14, 29)))
        self.assertTrue(market.is_scan_time(15, self.utc(2026, 11, 23, 14, 30)))
        self.assertTrue(market.is_scan_time(15, self.utc(2026, 11, 23, 21, 15)))
        self.assertFalse(market.is_scan_time(15, self.utc(2026, 11, 23, 21, 16)))
        # Saturday
        self.assertFalse(market.is_scan_time(15, self.utc(2026, 11, 21, 16, 0)))
        # Summer time: the session starts at 13:30 UTC.
        self.assertTrue(market.is_scan_time(15, self.utc(2026, 7, 15, 13, 30)))

    def test_session_fraction(self):
        # Monday 23 November 2026: the session is 14:30-21:00 UTC.
        self.assertEqual(market.session_fraction("2026-11-20", self.utc(2026, 11, 23, 16, 0)), 1.0)
        self.assertAlmostEqual(market.session_fraction("2026-11-23", self.utc(2026, 11, 23, 16, 7, 30)), 0.25)
        self.assertEqual(market.session_fraction("2026-11-23", self.utc(2026, 11, 23, 21, 0)), 1.0)
        self.assertEqual(market.session_fraction("2026-11-23", self.utc(2026, 11, 23, 14, 0)), 0.0)

    def test_env_file(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text('# comment\nTRADER_TEST_A = hello\nTRADER_TEST_B="quoted value"\n')
            try:
                load_env_file(path)
                self.assertEqual(os.environ["TRADER_TEST_A"], "hello")
                self.assertEqual(os.environ["TRADER_TEST_B"], "quoted value")
            finally:
                os.environ.pop("TRADER_TEST_A", None)
                os.environ.pop("TRADER_TEST_B", None)


class AppTests(unittest.TestCase):
    """The app's server, with prices replaced by canned histories."""

    def setUp(self):
        import json, os, urllib.request
        from unittest import mock
        from trader import app as app_module, scanner
        self.json, self.urllib = json, urllib.request
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "watchlist.txt").write_text("AAA\nBBB\n")
        (root / "config.toml").write_text(
            "[data]\nsource = \"twelvedata\"\n[schedule]\ninterval_minutes = 15\n[market]\nfilter = false\n")
        self.sent = []

        def fake_fetch(symbol, source="yahoo", notify=print, years=1):
            if symbol == "BBB":
                raise DataError("BBB: symbol not found")
            history = flat_breakout()
            history.symbol = history.name = symbol
            history.dates[-1] = date.today().isoformat()
            return history

        env = {"TWELVE_DATA_API_KEY": "k", "GMAIL_ADDRESS": "me@example.com", "GMAIL_APP_PASSWORD": "p"}
        for patcher in (
            mock.patch.dict(os.environ, env),
            mock.patch.object(scanner, "fetch_history", fake_fetch),
            mock.patch.object(scanner.time, "sleep"),
            mock.patch.object(emailer, "send", lambda message, sender, password: self.sent.append(message)),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.app = app_module.App(root / "watchlist.txt", root / "config.toml", root / ".state")
        self.server = app_module.ThreadingHTTPServer(("127.0.0.1", 0), app_module.make_handler(self.app))
        import threading
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, path, body=None, headers=None):
        import urllib.error
        request = self.urllib.Request(self.base + path)
        if body is not None:
            request.data = self.json.dumps(body).encode()
            request.add_header("X-Trader", "1")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        opener = self.urllib.build_opener(self.urllib.ProxyHandler({}))
        try:
            with opener.open(request, timeout=10) as response:
                return response.status, self.json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, self.json.loads(error.read())

    def check(self, email):
        import time
        self.assertEqual(self.call("/api/check", {"email": email})[1]["error"], "")
        for _ in range(200):
            scan = self.call("/api/state")[1]["scan"]
            if not scan["running"]:
                return scan
            time.sleep(0.02)
        self.fail("check did not finish")

    def test_page_is_served(self):
        opener = self.urllib.build_opener(self.urllib.ProxyHandler({}))
        with opener.open(self.base + "/", timeout=10) as response:
            self.assertIn(b"Check entries", response.read())

    def test_check_shows_plan_and_emails_once(self):
        scan = self.check(email=True)
        rows = {row["symbol"]: row for row in scan["results"]}
        self.assertEqual(rows["BBB"]["error"], "symbol not found")
        entry = rows["AAA"]["entries"][0]
        self.assertEqual(entry["direction"], "long")
        self.assertEqual([s["title"] for s in entry["signals"]], ["Gap up on heavy volume"])
        self.assertIn("Exit when a day closes below the low of the 20 days before it.", entry["plan"]["exit"])
        self.assertTrue(all(s["new"] for s in entry["signals"]))
        self.assertEqual([level["label"] for level in entry["plan"]["levels"]],
                         ["Entry", "SL", "TP1", "TP2"])
        self.assertEqual(len(self.sent), 1)
        self.assertIn("Emailed", scan["email"])

        again = self.check(email=True)
        self.assertEqual(len(self.sent), 1)  # nothing new, so no second email
        self.assertFalse(any(s["new"] for s in again["results"][0]["entries"][0]["signals"]))

    def test_check_without_email(self):
        scan = self.check(email=False)
        self.assertEqual(self.sent, [])
        self.assertEqual(scan["email"], "")
        self.assertTrue(scan["results"][0]["entries"][0]["signals"][0]["new"])

    def test_watchlist_editing(self):
        self.assertEqual(self.call("/api/watchlist/add", {"symbol": " tsla "})[1]["error"], "")
        self.assertIn("already", self.call("/api/watchlist/add", {"symbol": "TSLA"})[1]["error"])
        self.assertIn("ticker", self.call("/api/watchlist/add", {"symbol": "no spaces!"})[1]["error"])
        self.call("/api/watchlist/remove", {"symbol": "AAA"})
        self.assertEqual(self.call("/api/state")[1]["watchlist"], ["BBB", "TSLA"])

    def test_auto_setting_is_remembered(self):
        from trader import app as app_module
        self.assertEqual(self.call("/api/state")[1]["auto"]["interval"], 15)  # from config.toml
        self.call("/api/auto", {"enabled": True, "interval": 2})
        auto = self.call("/api/state")[1]["auto"]
        self.assertEqual((auto["enabled"], auto["interval"]), (True, 2))
        reopened = app_module.App(self.app.watchlist_path, self.app.config_path, self.app.state_path.parent)
        self.assertTrue(reopened.auto)
        self.assertEqual(reopened._interval(), 2)
        # Out-of-range or nonsense values are brought back into 1-15 minutes.
        for sent, kept in ((0, 1), (99, 15), ("abc", 15), (None, 15), (1, 1)):
            self.call("/api/auto", {"enabled": True, "interval": sent})
            self.assertEqual(self.call("/api/state")[1]["auto"]["interval"], kept, sent)

    def test_automatic_check_runs_on_the_chosen_interval(self):
        from unittest import mock
        from trader import app as app_module
        self.app.set_auto(True, 1)
        clock = {"now": 1000.0}
        ticks = {"n": 0}

        import threading
        test_thread = threading.current_thread()

        def fake_sleep(seconds):
            if threading.current_thread() is not test_thread:
                return  # a pause inside the background check itself
            ticks["n"] += 1
            clock["now"] += seconds
            if ticks["n"] > 40:  # 200 simulated seconds
                raise SystemExit
            while self.app.scan["running"]:
                pass  # let the background check finish (time.sleep is faked here)

        started = []
        real_start = self.app.start_check
        self.app.start_check = lambda send_email: started.append(clock["now"]) or real_start(send_email)
        with mock.patch.object(app_module.time, "sleep", fake_sleep), \
             mock.patch.object(app_module.time, "monotonic", lambda: clock["now"]), \
             mock.patch.object(app_module.market, "is_scan_time", lambda grace_minutes=0: True):
            with self.assertRaises(SystemExit):
                self.app.run_auto()
        # One check at once, then one per minute: 4 in 200 seconds.
        self.assertEqual(len(started), 4)
        self.assertEqual([round(b - a) for a, b in zip(started, started[1:])], [60, 60, 60])

    def test_checks_use_the_chosen_strategy(self):
        # Long entries from the tested strategies, until the user chooses otherwise.
        state = self.call("/api/state")[1]
        self.assertEqual(state["selection"], {"strategy": "all", "direction": "long", "timeframe": "1d"})
        self.assertEqual([(s["id"], s["unproven"]) for s in state["strategies"]],
                         [("dip", False), ("gap", False), ("base_breakout", True),
                          ("pullback", True), ("channel_breakout", True)])
        # Picking an unproven strategy by name runs it.
        self.call("/api/selection", {"strategy": "channel_breakout", "direction": "long"})
        scan = self.check(email=False)
        titles = [s["title"] for e in scan["results"][0]["entries"] for s in e["signals"]]
        self.assertEqual(titles, ["55-day breakout"])
        self.assertEqual((scan["strategy"], scan["direction"]), ("channel_breakout", "long"))

        self.call("/api/selection", {"strategy": "channel_breakout", "direction": "short"})
        self.assertEqual(self.check(email=False)["results"][0]["entries"], [])
        # Unknown values are ignored, and the choice survives a restart.
        self.call("/api/selection", {"strategy": "nonsense", "direction": "sideways"})
        self.assertEqual(self.call("/api/state")[1]["selection"],
                         {"strategy": "channel_breakout", "direction": "short", "timeframe": "1d"})
        from trader import app as app_module
        reopened = app_module.App(self.app.watchlist_path, self.app.config_path, self.app.state_path.parent)
        self.assertEqual((reopened.strategy, reopened.direction), ("channel_breakout", "short"))
        # Choices saved by a version before the strategies changed are not carried over.
        self.app.settings_path.write_text('{"strategy": "channel_breakout", "direction": "both", "auto": true}')
        older = app_module.App(self.app.watchlist_path, self.app.config_path, self.app.state_path.parent)
        self.assertEqual((older.strategy, older.direction, older.auto), ("all", "long", True))

    def test_market_filter_is_shown_and_applied(self):
        from unittest import mock
        from trader import scanner
        self.app.config_path.write_text("[data]\nsource = \"twelvedata\"\n")   # filter on by default

        def prices(rising):
            def fetch(symbol, source="yahoo", notify=print, years=1):
                history = flat_breakout()
                if symbol == "SPY":
                    return market_like(history, rising=rising)
                history.symbol = symbol
                return history
            return fetch

        self.call("/api/selection", {"strategy": "channel_breakout", "direction": "long"})
        with mock.patch.object(scanner, "fetch_history", prices(True)):
            scan = self.check(email=False)
        self.assertEqual(scan["market"], {"symbol": "SPY", "up": True})
        self.assertTrue(scan["results"][0]["entries"])
        with mock.patch.object(scanner, "fetch_history", prices(False)):
            scan = self.check(email=False)
        self.assertEqual(scan["market"], {"symbol": "SPY", "up": False})
        self.assertEqual([row["entries"] for row in scan["results"]], [[], []])

    def test_backtest_runs_and_reports(self):
        from unittest import mock
        from trader import app as app_module

        def history_for(symbol, source="yahoo", notify=print, years=1):
            if symbol == "BBB":
                raise DataError("BBB: symbol not found")
            closes = [200 - 0.002 * i * i for i in range(200)] + [120 + i * 1.5 for i in range(1, 80)]
            history = make_history(closes)
            history.symbol = symbol
            return history

        self.assertIsNone(self.call("/api/backtest")[1]["result"])
        with mock.patch.object(app_module, "fetch_history", history_for):
            self.assertIn("ticker", self.call("/api/backtest", {"symbol": ""})[1]["error"])
            body = {"symbol": "aaa", "strategy": "channel_breakout", "years": 1, "exit": "tp2",
                    "hold": 5, "hold_unit": "days", "budget": 2000, "risk_pct": 2}
            self.assertEqual(self.call("/api/backtest", body)[1]["error"], "")
            for _ in range(2000):
                status = self.call("/api/state")[1]["backtest"]
                if not status["running"]:
                    break
        self.assertEqual((status["error"], status["run"]), ("", 1))
        result = self.call("/api/backtest")[1]["result"]
        self.assertEqual(result["options"]["strategy"], "channel_breakout")
        self.assertEqual(result["options"]["exit"], "tp2")
        self.assertEqual((result["symbol"], result["options"]["hold"], result["options"]["budget"]), ("AAA", 5, 2000))
        self.assertTrue(result["trades"])
        self.assertEqual({t["strategy"] for t in result["trades"]}, {"channel_breakout"})
        self.assertEqual(result["total"]["start_budget"], 2000)
        profit = sum(t["pnl"] for t in result["trades"] if t["pnl"] is not None)
        self.assertAlmostEqual(result["total"]["end_budget"], 2000 + profit)

        # A ticker without prices reports the problem instead of a result.
        with mock.patch.object(app_module, "fetch_history", history_for):
            self.call("/api/backtest", {"symbol": "BBB"})
            for _ in range(2000):
                status = self.call("/api/state")[1]["backtest"]
                if not status["running"]:
                    break
        self.assertEqual(status["error"], "BBB: symbol not found")

    def test_hourly_checks(self):
        from unittest import mock
        from trader import scanner
        asked = []

        def hourly_prices(symbol, source="yahoo", notify=print, days=730):
            asked.append((symbol, days))
            return hourly_breakout()

        self.call("/api/selection", {"strategy": "channel_breakout", "direction": "both", "timeframe": "1h"})
        with mock.patch.object(scanner, "fetch_hourly", hourly_prices):
            scan = self.check(email=False)
        self.assertEqual(asked, [("AAA", 60), ("BBB", 60)])
        self.assertEqual(scan["timeframe"], "1h")
        self.assertIn("55-hour high", scan["results"][0]["entries"][0]["signals"][0]["reason"])
        self.assertEqual(self.call("/api/state")[1]["selection"]["timeframe"], "1h")

    def test_market_scan(self):
        from unittest import mock
        from trader import app as app_module

        def many(symbols, source="yahoo", batch=25):
            for symbol in symbols:
                if symbol == "NOPE":
                    yield symbol, DataError("NOPE: no price data")
                elif symbol == "FLAT":
                    yield symbol, make_history([100.0] * 260)
                else:
                    history = flat_breakout()
                    history.symbol = symbol
                    yield symbol, history

        self.assertIn("Choose", self.call("/api/screen", {"universe": "mars"})[1]["error"])
        names = [item["id"] for item in self.call("/api/state")[1]["universes"]]
        self.assertEqual(names[:3], ["nasdaq100", "sp500", "dowjones"])
        with mock.patch.object(app_module, "fetch_many", many), \
             mock.patch.object(app_module.universe, "load", lambda name: ["AAA", "FLAT", "NOPE", "ZZZ"]):
            body = {"universe": "dowjones", "strategy": "channel_breakout", "direction": "long"}
            self.assertEqual(self.call("/api/screen", body)[1]["error"], "")
            for _ in range(2000):
                status = self.call("/api/state")[1]["screen"]
                if not status["running"]:
                    break
        self.assertEqual((status["error"], status["done"], status["total"]), ("", 4, 4))
        result = self.call("/api/screen")[1]["result"]
        self.assertEqual((result["name"], result["scanned"], result["failed"]), ("Dow Jones 30", 3, 1))
        self.assertEqual([hit["symbol"] for hit in result["hits"]], ["AAA", "ZZZ"])
        entry = result["hits"][0]["entries"][0]
        self.assertEqual([s["title"] for s in entry["signals"]], ["55-day breakout"])
        self.assertEqual(entry["plan"]["levels"][1]["label"], "SL")
        self.assertEqual(self.sent, [])  # a scan never emails

    def test_page_is_told_when_the_program_was_updated(self):
        from unittest import mock
        from trader import app as app_module
        self.assertIs(self.call("/api/state")[1]["stale"], False)
        with mock.patch.object(app_module, "code_fingerprint", lambda: "different files on disk"):
            self.assertIs(self.call("/api/state")[1]["stale"], True)

    def test_a_new_copy_can_take_over_the_port(self):
        import socket
        from trader import app as app_module
        port = self.server.server_address[1]
        self.assertTrue(app_module.stop_running_copy(self.base))
        for _ in range(200):
            try:
                socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            except OSError:
                break  # the old copy has stopped listening
        else:
            self.fail("the running copy did not stop")
        # Nothing answering there any more: asking again reports failure.
        self.assertFalse(app_module.stop_running_copy(self.base))

    def test_other_sites_cannot_use_the_app(self):
        request = self.urllib.Request(self.base + "/api/check", data=b"{}")  # no X-Trader header
        opener = self.urllib.build_opener(self.urllib.ProxyHandler({}))
        import urllib.error
        with self.assertRaises(urllib.error.HTTPError) as raised:
            opener.open(request, timeout=10)
        self.assertEqual(raised.exception.code, 403)
        self.assertEqual(self.call("/api/state", headers={"Host": "evil.example"})[0], 403)
        self.assertEqual(self.sent, [])


class UpdateTests(unittest.TestCase):
    def archive(self, files, version="abc123"):
        import io, zipfile
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w") as bundle:
            for name, content in files.items():
                bundle.writestr("Trader-main/" + name, content)
            bundle.comment = version.encode()
        return buffer.getvalue()

    def test_update_replaces_program_files_and_keeps_user_files(self):
        from trader import update
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "trader").mkdir()
            (root / "trader" / "app.py").write_text("old code")
            (root / "config.toml").write_text("old settings")
            (root / "watchlist.txt").write_text("MU\n")
            (root / ".env").write_text("SECRET=1")
            (root / ".state").mkdir()
            (root / ".state" / "alerts.json").write_text("{}")

            first = self.archive({
                "trader/app.py": "new code", "trader/extra.py": "extra", "config.toml": "new settings",
                "watchlist.txt": "AAPL\n", ".env": "SECRET=stolen", ".state/alerts.json": "x",
                "../outside.txt": "escape",
            }, "v1")
            messages = update.apply_update(first, root)
            self.assertIn("3 file(s) changed", messages[0])
            self.assertEqual((root / "trader" / "app.py").read_text(), "new code")
            self.assertEqual((root / "config.toml").read_text(), "new settings")
            self.assertEqual((root / "config.toml.backup").read_text(), "old settings")
            # The user's own files are untouched, whatever the archive contains.
            self.assertEqual((root / "watchlist.txt").read_text(), "MU\n")
            self.assertEqual((root / ".env").read_text(), "SECRET=1")
            self.assertEqual((root / ".state" / "alerts.json").read_text(), "{}")
            self.assertFalse((root.parent / "outside.txt").exists())

            self.assertEqual(update.apply_update(first, root), ["Trader is already up to date."])

            # Next version: one file dropped, one edited by the user in between.
            (root / "trader" / "app.py").write_text("my own tweak")
            second = self.archive({"trader/app.py": "newer code", "config.toml": "new settings"}, "v2")
            messages = update.apply_update(second, root)
            self.assertIn("1 file(s) changed, 1 removed", messages[0])
            self.assertFalse((root / "trader" / "extra.py").exists())
            self.assertEqual((root / "trader" / "app.py.backup").read_text(), "my own tweak")
            self.assertEqual((root / "watchlist.txt").read_text(), "MU\n")

    def test_local_settings_override_defaults(self):
        from trader.scanner import load_config
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "config.toml"
            path.write_text("[plan]\nmin_stop_atr = 1.0\ntargets_r = [2.0, 3.0]\n[alerts]\ncooldown_days = 5\n")
            self.assertEqual(load_config(path)["plan"]["targets_r"], [2.0, 3.0])
            (Path(tmp) / "config.local.toml").write_text("[plan]\ntargets_r = [1.5]\n")
            config = load_config(path)
            self.assertEqual(config["plan"], {"min_stop_atr": 1.0, "targets_r": [1.5]})
            self.assertEqual(config["alerts"]["cooldown_days"], 5)


class HostingTests(unittest.TestCase):
    """Trader on a hosting service: a password page in front of everything."""

    PASSWORD = "correct horse battery"

    def setUp(self):
        import json, threading, urllib.request
        from unittest import mock
        from trader import app as app_module
        self.json, self.urllib, self.app_module = json, urllib.request, app_module
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "config.toml").write_text("[schedule]\ninterval_minutes = 15\n")
        (root / "data").mkdir()
        (root / "data" / "config.local.toml").write_text("[schedule]\ninterval_minutes = 3\n")
        patcher = mock.patch.object(app_module.time, "sleep")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = app_module.App(root / "data" / "watchlist.txt", root / "config.toml", root / "data" / ".state",
                                  local_config=root / "data" / "config.local.toml",
                                  password=self.PASSWORD, hosted=True)
        self.server = app_module.ThreadingHTTPServer(("127.0.0.1", 0), app_module.make_handler(self.app))
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.tmp.cleanup)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def call(self, path, body=None, cookie=None, headers=None):
        import urllib.error
        request = self.urllib.Request(self.base + path, headers={"Host": "trader.example.com", **(headers or {})})
        if body is not None:
            request.data = self.json.dumps(body).encode()
            request.add_header("X-Trader", "1")
        if cookie:
            request.add_header("Cookie", cookie)
        opener = self.urllib.build_opener(self.urllib.ProxyHandler({}))
        try:
            response = opener.open(request, timeout=10)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            raw = response.read()
            payload = self.json.loads(raw) if response.headers.get_content_type() == "application/json" else raw
            return response.status, payload, response.headers.get("Set-Cookie") or ""

    def sign_in(self):
        status, _, cookie = self.call("/api/login", {"password": self.PASSWORD})
        self.assertEqual(status, 200)
        return cookie.split(";")[0]

    def test_nothing_is_shown_before_signing_in(self):
        status, page, _ = self.call("/")
        self.assertEqual(status, 200)
        self.assertIn(b'id="login"', page)
        self.assertNotIn(b"Check entries", page)
        for path in ("/api/state", "/api/backtest", "/api/screen"):
            self.assertEqual(self.call(path)[0], 401)
        for path in ("/api/check", "/api/watchlist/add", "/api/backtest", "/api/quit"):
            self.assertEqual(self.call(path, {"symbol": "AAPL"})[0], 401)
        self.assertEqual(self.call("/healthz")[:2], (200, b"ok"))
        self.assertFalse((Path(self.tmp.name) / "data" / "watchlist.txt").exists())

    def test_signing_in_and_out(self):
        status, payload, cookie = self.call("/api/login", {"password": "wrong"})
        self.assertEqual((status, payload["error"], cookie), (401, "Wrong password.", ""))
        session = self.sign_in()
        _, _, set_cookie = self.call("/api/login", {"password": self.PASSWORD})
        self.assertIn("HttpOnly", set_cookie)
        self.assertIn("SameSite=Strict", set_cookie)
        self.assertNotIn("Secure", set_cookie)
        _, _, https_cookie = self.call("/api/login", {"password": self.PASSWORD}, headers={"X-Forwarded-Proto": "https"})
        self.assertIn("Secure", https_cookie)

        status, state, _ = self.call("/api/state", cookie=session)
        self.assertEqual((status, state["auth"]), (200, True))
        self.assertEqual(state["auto"]["interval"], 3)   # read from the data folder's own settings
        self.assertIn(b"Check entries", self.call("/", cookie=session)[1])
        self.assertEqual(self.call("/api/watchlist/add", {"symbol": "NVDA"}, cookie=session)[1]["error"], "")
        self.assertIn("NVDA", (Path(self.tmp.name) / "data" / "watchlist.txt").read_text())

        self.assertIn("Max-Age=0", self.call("/api/logout", {}, cookie=session)[2])

    def test_passes_cannot_be_forged_or_outlive_their_time(self):
        import time
        session = self.sign_in()
        expires, signature = session.split("=", 1)[1].split(".")
        self.assertEqual(self.call("/api/state", cookie=f"trader_session={int(expires) + 1}.{signature}")[0], 401)
        self.assertEqual(self.call("/api/state", cookie="trader_session=garbage")[0], 401)
        self.assertEqual(self.call("/api/state", cookie="trader_session=.")[0], 401)
        month_later = time.time() + 31 * 86400
        with unittest.mock.patch.object(self.app_module.time, "time", lambda: month_later):
            self.assertEqual(self.call("/api/state", cookie=session)[0], 401)
        # A pass signed for another password is worthless.
        other = self.app_module.App(self.app.watchlist_path, self.app.config_path, self.app.state_path.parent,
                                    password="another password")
        self.assertFalse(self.app.valid_session(other.new_session()))

    def test_guessing_is_locked_out(self):
        for _ in range(10):
            self.assertEqual(self.call("/api/login", {"password": "guess"})[0], 401)
        status, payload, _ = self.call("/api/login", {"password": self.PASSWORD})
        self.assertEqual(status, 429)
        self.assertIn("Too many", payload["error"])

    def test_quit_is_only_for_the_local_app(self):
        session = self.sign_in()
        self.assertEqual(self.call("/api/quit", {}, cookie=session)[0], 404)
        self.assertEqual(self.call("/api/state", cookie=session)[0], 200)   # still running

    def test_will_not_start_reachable_without_a_password(self):
        import contextlib, io, os
        from unittest import mock
        for password in ("", "short"):
            with mock.patch.dict(os.environ, {"PORT": "0", "TRADER_PASSWORD": password}), \
                 mock.patch.object(self.app_module, "load_env_file"), \
                 mock.patch.object(self.app_module, "serve") as serve, \
                 contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(self.app_module.main([]), 1)
            serve.assert_not_called()
            self.assertIn("TRADER_PASSWORD", out.getvalue())


class ResendTests(unittest.TestCase):
    def signal(self):
        return find(flat_breakout(), "channel_breakout")

    def test_alerts_go_through_resend_when_a_key_is_set(self):
        import io, json, os
        from unittest import mock
        sent = []

        def fake_urlopen(request, timeout=0):
            sent.append(request)
            return io.BytesIO(b'{"id": "1"}')

        env = {"RESEND_API_KEY": "re_123", "ALERT_TO": "me@example.com", "GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": ""}
        with mock.patch.dict(os.environ, env), \
             mock.patch.object(emailer.urllib.request, "urlopen", fake_urlopen), \
             mock.patch.object(emailer, "send") as gmail:
            self.assertEqual(emailer.send_alerts([self.signal()]), "me@example.com")
            self.assertEqual(emailer.send_test(), "me@example.com")
        gmail.assert_not_called()
        request = sent[0]
        self.assertEqual(request.full_url, "https://api.resend.com/emails")
        self.assertEqual(request.get_header("Authorization"), "Bearer re_123")
        payload = json.loads(request.data)
        self.assertEqual((payload["from"], payload["to"], payload["subject"]),
                         ("Trader <onboarding@resend.dev>", ["me@example.com"], "Entry alert: TEST long"))
        self.assertIn("SL", payload["text"])
        self.assertIn("<html>", payload["html"])
        self.assertNotIn("html", json.loads(sent[1].data))   # the test email is plain text

    def test_resend_problems_are_reported(self):
        import io, os, urllib.error
        from unittest import mock

        def refuse(request, timeout=0):
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {},
                                         io.BytesIO(b'{"message": "You can only send to your own address"}'))
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "re_123", "ALERT_TO": "x@example.com"}), \
             mock.patch.object(emailer.urllib.request, "urlopen", refuse):
            with self.assertRaises(emailer.EmailSendError) as raised:
                emailer.send_test()
        self.assertIn("You can only send to your own address", str(raised.exception))

        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "re_123", "ALERT_TO": "", "GMAIL_ADDRESS": ""}):
            with self.assertRaises(emailer.EmailConfigError):
                emailer.credentials()
        with mock.patch.dict(os.environ, {"RESEND_API_KEY": "", "ALERT_TO": "", "GMAIL_ADDRESS": "", "GMAIL_APP_PASSWORD": ""}):
            with self.assertRaises(emailer.EmailConfigError):
                emailer.credentials()
        gmail = {"RESEND_API_KEY": "", "GMAIL_ADDRESS": "me@gmail.com", "GMAIL_APP_PASSWORD": "abcd efgh", "ALERT_TO": ""}
        with mock.patch.dict(os.environ, gmail):
            self.assertEqual(emailer.credentials(), ("me@gmail.com", "abcdefgh", "me@gmail.com"))


if __name__ == "__main__":
    unittest.main()
