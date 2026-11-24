"""Lists of stocks to scan beyond the watchlist: index members and Yahoo's daily lists."""

from __future__ import annotations

import csv
import io
import re
import urllib.error
import urllib.request
from pathlib import Path

from .data import DataError, _yfinance

LISTS_URL = "https://raw.githubusercontent.com/yfiua/index-constituents/main/docs/constituents-{name}.csv"
SAVED = Path(__file__).with_name("universes")
SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9\-]{0,9}$")

# id -> (name shown on the page, kind). "index" lists come from a public file
# of index members; "yahoo" lists are Yahoo Finance's own lists for today.
UNIVERSES = {
    "nasdaq100": ("Nasdaq 100", "index"),
    "sp500": ("S&P 500", "index"),
    "dowjones": ("Dow Jones 30", "index"),
    "most_actives": ("Most active today", "yahoo"),
    "day_gainers": ("Top gainers today", "yahoo"),
    "day_losers": ("Top losers today", "yahoo"),
}


def load(universe: str) -> list[str]:
    """The tickers of a universe, in Yahoo's spelling (BRK-B, not BRK.B)."""
    if universe not in UNIVERSES:
        raise DataError(f"Unknown list of stocks: {universe}")
    if UNIVERSES[universe][1] == "yahoo":
        return _yahoo_list(universe)
    try:
        return _download_index(universe)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
        return _saved_index(universe)  # offline, or the file moved: use the saved copy


def _download_index(name: str) -> list[str]:
    request = urllib.request.Request(LISTS_URL.format(name=name),
                                     headers={"User-Agent": "trader"})
    with urllib.request.urlopen(request, timeout=15) as response:
        text = response.read().decode("utf-8")
    symbols = _clean(row["Symbol"] for row in csv.DictReader(io.StringIO(text)))
    if len(symbols) < 20:
        raise ValueError("the downloaded list is too short to be right")
    return symbols


def _saved_index(name: str) -> list[str]:
    lines = (SAVED / f"{name}.txt").read_text().splitlines()
    return _clean(line for line in lines if not line.startswith("#"))


def _yahoo_list(name: str) -> list[str]:
    try:
        found = _yfinance().screen(name, count=100)
        return _clean(quote.get("symbol", "") for quote in found.get("quotes", []))
    except Exception as error:  # yfinance raises many different error types
        raise DataError(f"Yahoo's list could not be loaded ({error})") from error


def _clean(symbols) -> list[str]:
    tidy = (str(symbol).strip().upper().replace(".", "-") for symbol in symbols)
    return [symbol for symbol in dict.fromkeys(tidy) if SYMBOL.match(symbol)]
