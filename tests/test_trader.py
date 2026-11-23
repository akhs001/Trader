import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path

from datetime import datetime, timezone

from trader import emailer, indicators, market
from trader.data import DataError, History, parse_chart
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


class DataTests(unittest.TestCase):
    def payload(self, **meta):
        return {"chart": {"error": None, "result": [{
            "meta": {"shortName": "Apple Inc.", "gmtoffset": -18000, **meta},
            # 2024-01-02, -03, -04 at 14:30 UTC (09:30 New York)
            "timestamp": [1704205800, 1704292200, 1704378600],
            "indicators": {"quote": [{
                "open": [10.0, None, 12.0], "high": [11.0, None, 13.0],
                "low": [9.0, None, 11.0], "close": [10.5, None, 12.5],
                "volume": [100, None, 300],
            }]},
        }]}}

    def test_parse_skips_null_rows(self):
        history = parse_chart("AAPL", self.payload())
        self.assertEqual(history.dates, ["2024-01-02", "2024-01-04"])
        self.assertEqual(history.closes, [10.5, 12.5])
        self.assertEqual(history.name, "Apple Inc.")
        self.assertEqual(history.session_fraction, 1.0)

    def test_parse_error_payload(self):
        with self.assertRaises(DataError):
            parse_chart("NOPE", {"chart": {"result": None, "error": {"description": "No data found"}}})

    def test_session_fraction_mid_session(self):
        import time
        now = int(time.time())
        start, end = now - 3600, now + 3 * 3600
        payload = self.payload(
            regularMarketTime=now,
            currentTradingPeriod={"regular": {"start": start, "end": end}},
        )
        payload["chart"]["result"][0]["timestamp"][-1] = start
        self.assertAlmostEqual(parse_chart("AAPL", payload).session_fraction, 0.25)

    def test_session_fraction_after_close(self):
        payload = self.payload(
            regularMarketTime=1704402000,
            currentTradingPeriod={"regular": {"start": 1704378600, "end": 1704402000}},
        )
        self.assertEqual(parse_chart("AAPL", payload).session_fraction, 1.0)


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
        self.assertEqual(message["Subject"], "Entry alert: TEST")
        text = message.get_body(("plain",)).get_content()
        html = message.get_body(("html",)).get_content()
        for body in (text, html):
            self.assertIn("TEST", body)
            self.assertIn("Breakout", body)
            self.assertIn("103.00", body)
            self.assertIn("not financial advice", body)

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


if __name__ == "__main__":
    unittest.main()
