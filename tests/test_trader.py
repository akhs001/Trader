import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from datetime import datetime, timezone

from trader import emailer, indicators, market
from trader.data import ApiKeyError, DataError, History, parse_time_series
from trader.main import load_env_file, load_watchlist
from trader.signals import scan
from trader.state import AlertState

CONFIG = {
    "signals": {
        "trend_pullback": {"enabled": True, "lookback_days": 5, "touch_pct": 1.0, "rsi_below": 40},
        "breakout": {"enabled": True, "lookback_days": 20, "volume_ratio": 1.5},
        "oversold_bounce": {"enabled": True, "rsi_level": 30},
        "ma_cross": {"enabled": True, "fast": 20, "slow": 50, "macd": True},
    }
}


def make_history(closes, volumes=None, highs=None, lows=None, session_fraction=1.0):
    n = len(closes)
    start = date(2024, 1, 1)
    return History(
        symbol="TEST",
        name="Test Corp",
        dates=[(start + timedelta(days=i)).isoformat() for i in range(n)],
        opens=list(closes),
        highs=highs or [c * 1.005 for c in closes],
        lows=lows or [c * 0.995 for c in closes],
        closes=list(closes),
        volumes=volumes or [1_000_000.0] * n,
        session_fraction=session_fraction,
    )


def kinds(history, config=CONFIG):
    return {s.kind for s in scan(history, config)}


class IndicatorTests(unittest.TestCase):
    def test_sma(self):
        self.assertEqual(indicators.sma([1, 2, 3, 4, 5], 3), [None, None, 2, 3, 4])

    def test_ema_seeds_with_sma(self):
        out = indicators.ema([1, 2, 3, 4, 5], 3)
        self.assertEqual(out[:3], [None, None, 2])
        self.assertAlmostEqual(out[3], 3)
        self.assertAlmostEqual(out[4], 4)

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

    def test_macd_lengths_and_sign(self):
        closes = [100 + i for i in range(60)]
        line, signal = indicators.macd(closes)
        self.assertEqual(len(line), 60)
        self.assertEqual(len(signal), 60)
        self.assertGreater(line[-1], 0)
        self.assertIsNone(signal[32])
        self.assertIsNotNone(signal[33])

    def test_atr_constant_range(self):
        n = 30
        out = indicators.atr([11.0] * n, [9.0] * n, [10.0] * n)
        self.assertAlmostEqual(out[-1], 2.0)


class SignalTests(unittest.TestCase):
    def test_quiet_uptrend_has_no_signals(self):
        # Steady compounding growth, with highs far enough above the closes
        # that no day closes above the previous day's high.
        closes = [100 * 1.002 ** i for i in range(260)]
        highs = [c * 1.01 for c in closes]
        self.assertEqual(kinds(make_history(closes, highs=highs)), set())

    def test_too_little_history(self):
        self.assertEqual(scan(make_history([100.0] * 30), CONFIG), [])

    def test_breakout_needs_volume(self):
        closes = [100.0] * 259 + [103.0]
        quiet = make_history(closes)
        self.assertNotIn("breakout", kinds(quiet))
        loud = make_history(closes, volumes=[1_000_000.0] * 259 + [2_000_000.0])
        self.assertIn("breakout", kinds(loud))

    def test_breakout_fires_only_on_first_day(self):
        closes = [100.0] * 258 + [103.0, 106.0]
        volumes = [1_000_000.0] * 258 + [2_000_000.0, 3_000_000.0]
        self.assertNotIn("breakout", kinds(make_history(closes, volumes=volumes)))

    def test_breakout_projects_partial_day_volume(self):
        closes = [100.0] * 259 + [103.0]
        volumes = [1_000_000.0] * 259 + [500_000.0]
        # Half the average volume by a quarter of the session = 2x pace.
        self.assertIn("breakout", kinds(make_history(closes, volumes=volumes, session_fraction=0.25)))
        self.assertNotIn("breakout", kinds(make_history(closes, volumes=volumes, session_fraction=1.0)))
        # Too early in the session to judge volume at all.
        self.assertNotIn("breakout", kinds(make_history(closes, volumes=volumes, session_fraction=0.05)))

    def test_oversold_bounce(self):
        closes = [100.0] * 200 + [100 - i * 1.5 for i in range(1, 21)]
        self.assertNotIn("oversold_bounce", kinds(make_history(closes)))
        closes.append(closes[-1] + 12)
        history = make_history(closes)
        self.assertIn("oversold_bounce", kinds(history))

    def test_trend_pullback(self):
        # Steady climb, a four-day dip to the 20-day average, then a turn up.
        closes = [100 + i * 0.5 for i in range(250)]
        peak = closes[-1]
        closes += [peak - 3, peak - 6, peak - 8, peak - 9]
        highs = [c + 0.5 for c in closes]
        lows = [c - 0.5 for c in closes]
        dipping = make_history(closes, highs=highs, lows=lows)
        self.assertNotIn("trend_pullback", kinds(dipping))

        closes.append(closes[-1] + 3)
        highs.append(closes[-1] + 0.5)
        lows.append(closes[-1] - 0.5)
        turned = make_history(closes, highs=highs, lows=lows)
        self.assertIn("trend_pullback", kinds(turned))

        # A dip that is too shallow does not count...
        shallow = {"signals": {**CONFIG["signals"], "trend_pullback": {"min_depth_atr": 50}}}
        self.assertNotIn("trend_pullback", kinds(turned, shallow))
        # ...and neither does the second up day in a row.
        closes.append(closes[-1] + 3)
        highs.append(closes[-1] + 0.5)
        lows.append(closes[-1] - 0.5)
        self.assertNotIn("trend_pullback", kinds(make_history(closes, highs=highs, lows=lows)))

    def test_no_pullback_signal_in_downtrend(self):
        closes = [300 - i * 0.5 for i in range(250)]
        closes.append(closes[-1] + 5)
        self.assertNotIn("trend_pullback", kinds(make_history(closes)))

    def test_ma_cross(self):
        # Long decline, then a rally strong enough to pull the 20-day over the 50-day.
        closes = [200 - i * 0.5 for i in range(200)]
        crossed_at = None
        for step in range(1, 80):
            closes.append(closes[-1] + 1.5)
            if "ma_cross" in kinds(make_history(closes)):
                crossed_at = step
                break
        self.assertIsNotNone(crossed_at)
        fast, slow = indicators.sma(closes, 20), indicators.sma(closes, 50)
        self.assertGreater(fast[-1], slow[-1])
        self.assertLessEqual(fast[-2], slow[-2])
        # The day after, the cross is no longer "new".
        closes.append(closes[-1] + 1.5)
        self.assertNotIn("ma_cross", kinds(make_history(closes)))

    def test_macd_cross(self):
        # An accelerating decline keeps MACD below its signal line until the rally.
        closes = [200 - 0.002 * i * i for i in range(200)]
        self.assertNotIn("macd_cross", kinds(make_history(closes)))
        fired = False
        for _ in range(10):
            closes.append(closes[-1] + 1.5)
            fired = fired or "macd_cross" in kinds(make_history(closes))
        self.assertTrue(fired)

    def test_disabled_signal_is_skipped(self):
        closes = [100.0] * 259 + [103.0]
        history = make_history(closes, volumes=[1_000_000.0] * 259 + [2_000_000.0])
        config = {"signals": {**CONFIG["signals"], "breakout": {"enabled": False}}}
        self.assertNotIn("breakout", kinds(history, config))

    def test_signal_fields(self):
        closes = [100.0] * 259 + [103.0]
        history = make_history(closes, volumes=[1_000_000.0] * 259 + [2_000_000.0])
        signal = next(s for s in scan(history, CONFIG) if s.kind == "breakout")
        self.assertEqual(signal.symbol, "TEST")
        self.assertEqual(signal.price, 103.0)
        self.assertAlmostEqual(signal.change_pct, 3.0)
        self.assertEqual(signal.date, history.dates[-1])
        self.assertIn("2.0x", signal.reason)
        self.assertIn("RSI(14)", signal.facts)


class PlanTests(unittest.TestCase):
    def breakout(self, plan_settings=None, last_close=103.0):
        closes = [100.0] * 259 + [last_close]
        history = make_history(closes, volumes=[1_000_000.0] * 259 + [2_000_000.0])
        config = {**CONFIG, "plan": plan_settings or {}}
        signal = next(s for s in scan(history, config) if s.kind == "breakout")
        atr = indicators.atr(history.highs, history.lows, history.closes)[-1]
        return signal.plan, atr

    def test_stop_sits_below_the_setup_level(self):
        plan, atr = self.breakout()
        self.assertEqual(plan.entry, 103.0)
        # The breakout level is the prior high of 100.50; the stop is a quarter ATR below it.
        self.assertAlmostEqual(plan.stop, 100.5 - 0.25 * atr)
        self.assertIn("breakout level (100.50)", plan.stop_basis)
        self.assertAlmostEqual(plan.targets[0], plan.entry + 2 * plan.risk)
        self.assertAlmostEqual(plan.targets[1], plan.entry + 3 * plan.risk)
        self.assertLess(plan.stop, plan.entry)

    def test_stop_is_not_tighter_than_the_minimum(self):
        plan, atr = self.breakout(last_close=100.6)
        self.assertAlmostEqual(plan.risk, 1.0 * atr)
        self.assertIn("too close", plan.stop_basis)

    def test_stop_is_not_wider_than_the_maximum(self):
        plan, atr = self.breakout({"max_stop_atr": 1.5})
        self.assertAlmostEqual(plan.risk, 1.5 * atr)
        self.assertIn("too far", plan.stop_basis)

    def test_custom_targets(self):
        plan, _ = self.breakout({"targets_r": [1.5]})
        self.assertEqual(len(plan.targets), 1)
        self.assertAlmostEqual(plan.targets[0], plan.entry + 1.5 * plan.risk)

    def test_cross_signals_use_a_volatility_stop(self):
        closes = [200 - i * 0.5 for i in range(200)]
        signal = None
        while signal is None:
            closes.append(closes[-1] + 1.5)
            history = make_history(closes)
            signal = next((s for s in scan(history, CONFIG) if s.kind == "ma_cross"), None)
        atr = indicators.atr(history.highs, history.lows, history.closes)[-1]
        self.assertAlmostEqual(signal.plan.risk, 2.0 * atr)
        self.assertEqual(signal.plan.stop_basis, "2 ATR below entry")

    def test_pullback_and_bounce_stops_use_recent_lows(self):
        closes = [100 + i * 0.5 for i in range(250)]
        peak = closes[-1]
        closes += [peak - 3, peak - 6, peak - 8, peak - 9, peak - 6]
        history = make_history(closes, highs=[c + 0.5 for c in closes], lows=[c - 0.5 for c in closes])
        signal = next(s for s in scan(history, CONFIG) if s.kind == "trend_pullback")
        self.assertIn(f"pullback low ({peak - 9.5:.2f})", signal.plan.stop_basis)
        self.assertLess(signal.plan.stop, peak - 9.5)


def mirrored(history):
    """The same chart upside down: every rise becomes an equal fall."""
    top = 2 * max(history.highs)
    return History(
        symbol=history.symbol, name=history.name, dates=history.dates,
        opens=[top - v for v in history.opens], highs=[top - v for v in history.lows],
        lows=[top - v for v in history.highs], closes=[top - v for v in history.closes],
        volumes=history.volumes, session_fraction=history.session_fraction,
    )


class ShortTests(unittest.TestCase):
    """Short signals are the long ones upside down."""

    PAIRS = {"trend_pullback": "trend_rally", "breakout": "breakdown",
             "oversold_bounce": "overbought_drop", "ma_cross": "ma_cross_down",
             "macd_cross": "macd_cross_down"}

    def long_setups(self):
        yield make_history([100.0] * 259 + [103.0], volumes=[1_000_000.0] * 259 + [2_000_000.0])
        closes = [100 + i * 0.5 for i in range(250)]
        peak = closes[-1]
        closes += [peak - 3, peak - 6, peak - 8, peak - 9, peak - 6]
        yield make_history(closes, highs=[c + 0.5 for c in closes], lows=[c - 0.5 for c in closes])
        closes = [100.0] * 200 + [100 - i * 1.5 for i in range(1, 21)]
        yield make_history(closes + [closes[-1] + 12])
        closes = [200 - i * 0.5 for i in range(200)]
        for _ in range(60):
            closes.append(closes[-1] + 1.5)
            yield make_history(closes)

    def test_every_long_signal_has_a_mirrored_short(self):
        seen = set()
        for history in self.long_setups():
            longs = {s.kind for s in scan(history, CONFIG) if s.direction == "long"}
            shorts = {s.kind for s in scan(mirrored(history), CONFIG) if s.direction == "short"}
            self.assertEqual({self.PAIRS[kind] for kind in longs}, shorts)
            seen |= longs
        self.assertEqual(seen, set(self.PAIRS))  # every kind was exercised

    def test_short_plan_is_the_long_plan_upside_down(self):
        history = make_history([100.0] * 259 + [103.0], volumes=[1_000_000.0] * 259 + [2_000_000.0])
        long_plan = next(s for s in scan(history, CONFIG) if s.kind == "breakout").plan
        flipped = mirrored(history)
        signal = next(s for s in scan(flipped, CONFIG) if s.kind == "breakdown")
        plan, top = signal.plan, 2 * max(history.highs)
        self.assertEqual((signal.direction, plan.direction), ("short", "short"))
        self.assertAlmostEqual(plan.entry, top - long_plan.entry)
        self.assertAlmostEqual(plan.stop, top - long_plan.stop)
        self.assertAlmostEqual(plan.targets[1], top - long_plan.targets[1])
        self.assertGreater(plan.stop, plan.entry)        # stop above
        self.assertLess(plan.targets[0], plan.entry)     # targets below
        self.assertAlmostEqual(plan.risk, long_plan.risk)
        self.assertIn("just above the breakdown level", plan.stop_basis)
        self.assertIn("moved below its 20-day low", signal.reason)

    def test_short_wording(self):
        closes = [100.0] * 200 + [100 + i * 1.5 for i in range(1, 21)]
        history = make_history(closes + [closes[-1] - 12])
        signal = next(s for s in scan(history, CONFIG) if s.kind == "overbought_drop")
        self.assertRegex(signal.reason, r"crossed back below 70 \(yesterday (100|[789]\d), now [3-6]\d\)")
        self.assertIn("the recent high", signal.plan.stop_basis)

    def test_directions_can_be_switched_off(self):
        history = make_history([100.0] * 259 + [103.0], volumes=[1_000_000.0] * 259 + [2_000_000.0])
        flipped = mirrored(history)
        no_shorts = {"signals": {**CONFIG["signals"], "short": False}}
        no_longs = {"signals": {**CONFIG["signals"], "long": False}}
        self.assertEqual({s.direction for s in scan(flipped, no_shorts)}, set())
        self.assertEqual({s.direction for s in scan(history, no_longs)}, set())
        self.assertEqual({s.direction for s in scan(flipped, no_longs)}, {"short"})

    def test_email_marks_the_direction(self):
        history = mirrored(make_history([100.0] * 259 + [103.0], volumes=[1_000_000.0] * 259 + [2_000_000.0]))
        signals = [s for s in scan(history, CONFIG) if s.kind == "breakdown"]
        message = emailer.build_message(signals, "me@example.com", "me@example.com")
        self.assertEqual(message["Subject"], "Entry alert: TEST short")
        self.assertIn("TEST (Test Corp) - SHORT", message.get_body(("plain",)).get_content())
        self.assertIn(">SHORT</span>", message.get_body(("html",)).get_content())


class BacktestTests(unittest.TestCase):
    def setup_then(self, later, short=False):
        """A breakout on a flat stock, followed by the given (open, high, low, close) days."""
        from trader import backtest
        closes = [100.0] * 259 + [103.0]
        history = make_history(closes, volumes=[1_000_000.0] * 259 + [2_000_000.0])
        plan = next(s for s in scan(history, CONFIG) if s.kind == "breakout").plan
        for i, (open_, high, low, close) in enumerate(later):
            history.dates.append((date(2025, 1, 1) + timedelta(days=i)).isoformat())
            history.opens.append(open_); history.highs.append(high)
            history.lows.append(low); history.closes.append(close)
            history.volumes.append(1_000_000.0)
        if short:
            history, top = mirrored(history), 2 * max(history.highs)
        options = backtest.Options(strategy="breakout", direction="short" if short else "long",
                                   years=0.1, target=1, max_days=3)
        trades = backtest.backtest_stock(history, CONFIG, options)
        return plan, trades[0]

    def test_target_hit(self):
        plan, trade = self.setup_then([(103, 104, 102.5, 103.5), (104, 110, 103.5, 109)])
        self.assertEqual((trade.reason, trade.days), ("target", 2))
        self.assertAlmostEqual(trade.exit, plan.targets[0])
        self.assertAlmostEqual(trade.r, 2.0)

    def test_stop_hit(self):
        plan, trade = self.setup_then([(103, 103.5, 99, 100)])
        self.assertEqual(trade.reason, "stop")
        self.assertAlmostEqual(trade.exit, plan.stop)
        self.assertAlmostEqual(trade.r, -1.0)

    def test_stop_wins_when_both_are_touched_the_same_day(self):
        _, trade = self.setup_then([(103, 115, 95, 110)])
        self.assertEqual(trade.reason, "stop")
        self.assertAlmostEqual(trade.r, -1.0)

    def test_gap_through_the_stop_exits_at_the_open(self):
        _, trade = self.setup_then([(97, 98, 96, 97)])
        self.assertEqual((trade.reason, trade.exit), ("stop", 97))
        self.assertLess(trade.r, -1.0)

    def test_gap_through_the_target_exits_at_the_open(self):
        _, trade = self.setup_then([(112, 113, 111, 112)])
        self.assertEqual((trade.reason, trade.exit), ("target", 112))
        self.assertGreater(trade.r, 2.0)

    def test_time_limit(self):
        _, trade = self.setup_then([(103, 104, 102, 103.5)] * 5)
        self.assertEqual((trade.reason, trade.days, trade.exit), ("time", 3, 103.5))

    def test_trade_still_open_at_the_end(self):
        from trader import backtest
        _, trade = self.setup_then([(103, 104, 102, 103.5)])
        self.assertEqual(trade.reason, "open")
        self.assertEqual(backtest.summarize([trade]), {"trades": 0, "open": 1})

    def test_short_trade_is_the_long_trade_upside_down(self):
        _, long_trade = self.setup_then([(103, 104, 102.5, 103.5), (104, 110, 103.5, 109)])
        _, short_trade = self.setup_then([(103, 104, 102.5, 103.5), (104, 110, 103.5, 109)], short=True)
        self.assertEqual((short_trade.direction, short_trade.reason), ("short", "target"))
        self.assertAlmostEqual(short_trade.r, long_trade.r)
        self.assertLess(short_trade.exit, short_trade.entry)

    def test_only_the_chosen_strategy_and_direction_are_traded(self):
        from trader import backtest
        closes = [200 - 0.002 * i * i for i in range(200)] + [120 + i * 1.5 for i in range(1, 80)]
        history = make_history(closes)
        everything = backtest.backtest_stock(history, CONFIG, backtest.Options(years=1))
        self.assertGreater(len({t.strategy for t in everything}), 1)
        only = backtest.backtest_stock(history, CONFIG, backtest.Options(strategy="macd_cross", direction="long", years=1))
        self.assertTrue(only)
        self.assertEqual({(t.strategy, t.direction) for t in only}, {("macd_cross", "long")})

    def test_past_days_scan_exactly_like_live_days(self):
        from trader.signals import Analysis
        import math
        closes = [100 + 20 * math.sin(i / 9) + i * 0.05 + (i % 7) * 0.4 for i in range(420)]
        volumes = [1_000_000.0 * (1 + (i % 11) / 5) for i in range(420)]
        history = make_history(closes, volumes=volumes)
        analysis, compared = Analysis(history), 0
        for end in range(205, 420):
            part = make_history(closes[:end], volumes=volumes[:end])
            live = [(s.kind, s.reason, s.plan.stop, s.plan.targets) for s in scan(part, CONFIG)]
            past = [(s.kind, s.reason, s.plan.stop, s.plan.targets) for s in analysis.scan(CONFIG, end=end)]
            self.assertEqual(live, past)
            compared += len(live)
        self.assertGreater(compared, 20)

    def test_summary_numbers(self):
        from trader import backtest
        def trade(r, day):
            return backtest.Trade("T", "breakout", "Breakout", "long", "2025-01-01", 100, 95, 110,
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
        self.assertEqual(report["rows"][0]["name"], "Breakout")


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
        closes = [100.0] * 259 + [103.0]
        history = make_history(closes, volumes=[1_000_000.0] * 259 + [2_000_000.0])
        signal = next(s for s in scan(history, CONFIG) if s.kind == "breakout")
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
            self.assertIn("Breakout", body)
            self.assertIn("103.00", body)
            self.assertIn("not financial advice", body)
            for label in ("Entry", "SL", "TP1", "TP2", "108.59", "breakout level"):
                self.assertIn(label, body)

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
        (root / "config.toml").write_text("[data]\nsource = \"twelvedata\"\n[schedule]\ninterval_minutes = 15\n")
        self.sent = []

        def fake_fetch(symbol, source="yahoo", notify=print, years=1):
            if symbol == "BBB":
                raise DataError("BBB: symbol not found")
            history = make_history([100.0] * 259 + [103.0], volumes=[1_000_000.0] * 259 + [2_000_000.0])
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
        self.assertIn("Breakout", [s["title"] for s in entry["signals"]])
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
        self.assertEqual(self.call("/api/state")[1]["selection"], {"strategy": "all", "direction": "both"})
        self.call("/api/selection", {"strategy": "breakout", "direction": "long"})
        scan = self.check(email=False)
        titles = [s["title"] for e in scan["results"][0]["entries"] for s in e["signals"]]
        self.assertEqual(titles, ["Breakout"])
        self.assertEqual((scan["strategy"], scan["direction"]), ("breakout", "long"))

        self.call("/api/selection", {"strategy": "breakout", "direction": "short"})
        self.assertEqual(self.check(email=False)["results"][0]["entries"], [])
        # Unknown values are ignored, and the choice survives a restart.
        self.call("/api/selection", {"strategy": "nonsense", "direction": "sideways"})
        self.assertEqual(self.call("/api/state")[1]["selection"], {"strategy": "breakout", "direction": "short"})
        from trader import app as app_module
        reopened = app_module.App(self.app.watchlist_path, self.app.config_path, self.app.state_path.parent)
        self.assertEqual((reopened.strategy, reopened.direction), ("breakout", "short"))

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
            self.assertEqual(self.call("/api/backtest", {"strategy": "macd_cross", "years": 1, "target": 2})[1]["error"], "")
            for _ in range(2000):
                status = self.call("/api/state")[1]["backtest"]
                if not status["running"]:
                    break
        self.assertEqual((status["error"], status["run"]), ("", 1))
        result = self.call("/api/backtest")[1]["result"]
        self.assertEqual(result["options"]["strategy"], "macd_cross")
        self.assertEqual(result["options"]["target"], 2)
        self.assertEqual(result["skipped"], ["BBB: symbol not found"])
        self.assertTrue(result["trades"])
        self.assertEqual({t["strategy"] for t in result["trades"]}, {"macd_cross"})
        self.assertEqual({t["symbol"] for t in result["trades"]}, {"AAA"})

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


if __name__ == "__main__":
    unittest.main()
