"""Daily price history from Yahoo Finance's public chart endpoint."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HOSTS = ("query1.finance.yahoo.com", "query2.finance.yahoo.com")
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


class DataError(Exception):
    pass


@dataclass
class History:
    symbol: str
    name: str
    dates: list[str]  # ISO dates in the exchange's timezone, oldest first
    opens: list[float]
    highs: list[float]
    lows: list[float]
    closes: list[float]
    volumes: list[float]
    # How much of today's regular session has elapsed for the last candle:
    # 1.0 when the candle is complete, less while the market is still open.
    session_fraction: float = 1.0


def fetch_history(symbol: str, range_: str = "2y", retries: int = 3) -> History:
    path = f"/v8/finance/chart/{urllib.parse.quote(symbol)}?range={range_}&interval=1d"
    last_error: Exception | None = None
    for attempt in range(retries):
        host = HOSTS[attempt % len(HOSTS)]
        request = urllib.request.Request(
            f"https://{host}{path}", headers={"User-Agent": USER_AGENT}
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                return parse_chart(symbol, json.load(response))
        except urllib.error.HTTPError as error:
            if error.code == 404:
                raise DataError(f"{symbol}: unknown ticker") from error
            last_error = error
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(2 * (attempt + 1))
    raise DataError(f"{symbol}: could not download prices ({last_error})")


def parse_chart(symbol: str, payload: dict) -> History:
    chart = payload.get("chart") or {}
    if chart.get("error"):
        raise DataError(f"{symbol}: {chart['error'].get('description', 'data error')}")
    results = chart.get("result") or []
    if not results or not results[0].get("timestamp"):
        raise DataError(f"{symbol}: no price data returned")
    result = results[0]
    meta = result.get("meta", {})
    quote = result["indicators"]["quote"][0]
    tz = ZoneInfo(meta.get("exchangeTimezoneName") or "America/New_York")

    history = History(
        symbol=symbol,
        name=meta.get("shortName") or meta.get("longName") or symbol,
        dates=[], opens=[], highs=[], lows=[], closes=[], volumes=[],
    )
    rows = zip(
        result["timestamp"], quote["open"], quote["high"], quote["low"],
        quote["close"], quote["volume"],
    )
    for ts, o, h, l, c, v in rows:
        if None in (o, h, l, c):
            continue  # Yahoo leaves gaps as nulls (e.g. a candle before the open)
        history.dates.append(datetime.fromtimestamp(ts, tz).date().isoformat())
        history.opens.append(float(o))
        history.highs.append(float(h))
        history.lows.append(float(l))
        history.closes.append(float(c))
        history.volumes.append(float(v or 0))
    if not history.dates:
        raise DataError(f"{symbol}: no usable price data")

    history.session_fraction = _session_fraction(meta, history.dates[-1], tz)
    return history


def _session_fraction(meta: dict, last_date: str, tz: ZoneInfo) -> float:
    regular = (meta.get("currentTradingPeriod") or {}).get("regular") or {}
    start, end = regular.get("start"), regular.get("end")
    now = meta.get("regularMarketTime")
    if not (start and end and now) or end <= start:
        return 1.0
    session_date = datetime.fromtimestamp(start, tz).date().isoformat()
    if last_date != session_date or now >= end:
        return 1.0
    # Guard against a clock that says "still open" long after the last trade.
    if datetime.now(timezone.utc).timestamp() >= end:
        return 1.0
    return min(max((now - start) / (end - start), 0.0), 1.0)
