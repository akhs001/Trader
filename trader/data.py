"""Daily price history from the Twelve Data API (https://twelvedata.com)."""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime

from . import market

API_URL = "https://api.twelvedata.com/time_series"
BARS = 400  # enough history for the 200-day average to settle


class DataError(Exception):
    """Prices for one stock could not be read."""


class ApiKeyError(Exception):
    """The Twelve Data API key is missing or rejected; no stock can be read."""


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


def api_key() -> str:
    key = os.environ.get("TWELVE_DATA_API_KEY", "").strip()
    if not key:
        raise ApiKeyError(
            "Price data is not set up: put TWELVE_DATA_API_KEY in the .env file. "
            "See README.md."
        )
    return key


def fetch_history(symbol: str, retries: int = 3) -> History:
    query = urllib.parse.urlencode(
        {"symbol": symbol, "interval": "1day", "outputsize": BARS, "order": "asc"}
    )
    request = urllib.request.Request(
        f"{API_URL}?{query}", headers={"Authorization": f"apikey {api_key()}"}
    )
    last_error: object = None
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as error:
            # Twelve Data describes the problem in a JSON body.
            try:
                payload = json.load(error)
            except (json.JSONDecodeError, UnicodeDecodeError):
                payload = {"status": "error", "code": error.code, "message": str(error)}
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            time.sleep(2 * (attempt + 1))
            continue

        if payload.get("status") != "error":
            return parse_time_series(symbol, payload)
        code, message = payload.get("code"), str(payload.get("message", "unknown error"))
        if code == 401:
            raise ApiKeyError(f"Twelve Data rejected the API key: {message}")
        if code == 429 and "minute" in message and attempt < retries - 1:
            # The free plan allows a few requests per minute; wait for the next one.
            wait = 61 - datetime.now().second
            print(f"  (Twelve Data's per-minute limit reached, waiting {wait}s)")
            time.sleep(wait)
            continue
        raise DataError(f"{symbol}: {message}")
    raise DataError(f"{symbol}: could not download prices ({last_error})")


def parse_time_series(symbol: str, payload: dict) -> History:
    values = payload.get("values") or []
    history = History(
        symbol=symbol, name=symbol,
        dates=[], opens=[], highs=[], lows=[], closes=[], volumes=[],
    )
    for row in sorted(values, key=lambda r: r.get("datetime", "")):
        try:
            candle = [float(row[field]) for field in ("open", "high", "low", "close")]
        except (KeyError, TypeError, ValueError):
            continue  # skip an incomplete candle
        history.dates.append(str(row["datetime"])[:10])
        history.opens.append(candle[0])
        history.highs.append(candle[1])
        history.lows.append(candle[2])
        history.closes.append(candle[3])
        history.volumes.append(float(row.get("volume") or 0))
    if not history.dates:
        raise DataError(f"{symbol}: no price data returned")

    history.session_fraction = market.session_fraction(history.dates[-1])
    return history
