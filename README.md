# Trader

A watchlist scanner that runs on your own computer and opens in your browser.
Press **Check entries** and it checks every stock on your watchlist for a
possible entry, shows each one with Entry, SL and TP, and can email them to
you. It only alerts; it never places trades.

## Signals

All signals use daily candles from Yahoo Finance, read through the
[yfinance](https://github.com/ranaroussi/yfinance) package. During market hours
today's unfinished candle is included, so a check uses the current price.

Trader looks for long (buy) and short (sell short) entries. Every short signal
is its long counterpart upside down.

| Long signal | Fires when | Short signal | Fires when |
| --- | --- | --- | --- |
| Uptrend continuation | A massive run up (at least 10 ATR within 60 days, with the 20-day average above the 50-day above the 200-day) gave back 30% to 65% of the run in the last few days, and the price now trades above yesterday's high for the first time. | Downtrend continuation | A massive drop of the same size, with the averages stacked the other way, bounced 30% to 65% of the drop, and the price now trades below yesterday's low for the first time. |
| Trend pullback | Uptrend (above the 200-day average) dipped at least 2 ATR to its 20- or 50-day average, or to RSI below 40, and now trades above yesterday's high for the first time. | Downtrend rally | Downtrend (below the 200-day average) rallied at least 2 ATR to its 20- or 50-day average, or to RSI above 60, and now trades below yesterday's low for the first time. |
| Breakout | Price moves above its 20-day high on at least 1.5x average volume. | Breakdown | Price moves below its 20-day low on at least 1.5x average volume. |
| Oversold bounce | RSI(14) was below 30 yesterday and is back above it. | Overbought reversal | RSI(14) was above 70 yesterday and is back below it. |
| Moving-average cross | The 20-day average crosses above the 50-day average. | Moving-average cross down | The 20-day average crosses below the 50-day average. |
| MACD cross | MACD (12, 26, 9) crosses above its signal line. | MACD cross down | MACD crosses below its signal line. |

To get only one direction, put this in `config.local.toml`:

```toml
[signals]
short = false
```

## Trade plan

Every alert comes with Entry, SL, TP1 and TP2:

- **Entry** is the price at the moment of the scan.
- **SL** sits just beyond the price that would prove the setup wrong: for a
  long, below the pullback low, the breakout level, or the recent low; for a
  short, above the rally high, the breakdown level, or the recent high. It is kept between 1
  and 3 ATR from the entry (ATR is the stock's average daily range), so it is
  neither inside everyday noise nor unreasonably far. Average and MACD crosses
  have no such price, so their SL is 2 ATR from the entry.
- **TP1 and TP2** are 2 and 3 times the risk (the distance from entry to SL)
  on the profit side: above the entry for a long, below it for a short.

When a stock fires several signals in the same direction at once, the plan
follows the first one listed. The levels come from fixed rules, not from a forecast, and have not
been backtested.

## Settings

Thresholds, the trade-plan rules and the check interval live in `config.toml`,
where each signal can also be switched off. Do not edit that file, because
updates replace it. Create `config.local.toml` next to it instead and put only
the settings you want to change in it:

```toml
[plan]
targets_r = [1.5, 3.0]

[signals.ma_cross]
macd = false
```
 After a signal is emailed for a stock, the same signal
stays quiet for `cooldown_days` (default 5).

## Setup

1. **Install Python 3.11 or newer** from <https://www.python.org/downloads/>.
   On Windows, tick "Add python.exe to PATH" in the installer. `start.bat`
   installs the one package Trader needs (yfinance) the first time it runs.
2. **Download this project** to your computer: on GitHub, *Code → Download
   ZIP*, then unzip it.
3. **Create a Gmail app password.** Turn on 2-Step Verification for your
   Google account, then create a password at
   <https://myaccount.google.com/apppasswords>. It is 16 letters long.
4. **Add your email details.** Copy `.env.example` to a new file named `.env`
   in the same folder and fill in your Gmail address and the app password.
5. **Set your watchlist** in the app, or edit `watchlist.txt` (one ticker per
   line).

## Running

On Windows, double-click `start.bat`. On Mac or Linux, run
`python3 -m pip install -r requirements.txt` once, then `python3 -m trader.app`
in the project folder. Trader opens in your browser at
<http://127.0.0.1:8765>. Keep the small console window open while you use it;
closing it stops Trader.

- **Strategy** and **Direction** choose what a check looks for: all strategies
  or one of them (trend continuation, trend pullback, breakout, RSI reversal,
  moving-average cross, MACD cross), and long, short or both. The choice is remembered and
  also applies to automatic checks and emails.
- **Check entries** checks the whole watchlist now. Stocks with a signal
  appear as cards with Entry, SL, TP1 and TP2; the table lists every stock.
- **Email me new entries** also emails the entries that have not been emailed
  in the last `cooldown_days`. Without Gmail details in `.env`, entries are
  only shown on the page.
- **Check automatically every N min** repeats the check on weekdays from 9:30
  to 16:00 New York time and emails new entries. Pick any interval from 1 to
  15 minutes; your choice is remembered. It is off until you tick it, and your
  computer has to be on and awake.
- Add and remove tickers under **Watchlist**, or edit `watchlist.txt`.

The page only answers on your own computer; it is not reachable from the
network.

### Backtest tab

The Backtest tab shows how the strategies would have done on one stock's past
prices. You choose:

- **Strategy** and **Direction**: all strategies or one, long, short or both.
- **Ticker**: any ticker, on your watchlist or not.
- **Period**: the last 1, 2 or 5 years.
- **Take profit at**: whether TP1 or TP2 ends a winning trade.
- **Close after**: how long a trade may stay open if neither SL nor TP is
  reached, from 1 hour to 60 days. Choices in hours follow each trade on
  hourly prices, which reach back about two years.
- **Start budget** and **Risk per trade**: the money you start with, and the
  share of it a trade loses if its SL is hit.

**Run backtest** replays the period day by day, scanning each past day exactly
as a live check would have, and trades every signal by its own plan.

The page shows the budget at the end, the profit, the largest drawdown, the
number of trades, win rate, average result and profit factor (gains divided
by losses; above 1 is profitable), a chart of the budget over time, a table
per strategy and side, and every trade. Results per trade are in R: 1R is the
amount risked on the trade, the distance from entry to SL. A trade stopped out
loses 1R; one that reaches TP1 gains 2R.

What the backtest assumes:

- A trade enters at the close of the signal day.
- If SL and TP are both touched in the same candle (day, or hour when the
  holding time is in hours), the SL counts.
- A price that opens beyond the SL or TP exits at that opening price.
- One trade at a time per strategy and side.
- Each trade risks the chosen percentage of the budget as it stands at entry.
  A position is never larger than the budget, and fractions of a share are
  allowed.
- No commissions, slippage, or borrowing costs for shorts.

It tells you how the rules behaved in the past on your stocks, which is not a
promise about the future. One stock over a couple of years is also a small
sample.

### Price source

Yahoo Finance needs no key and has no fixed per-minute limit, but yfinance is
an unofficial library and Yahoo can temporarily refuse a computer that asks
too much. A watchlist of a few dozen stocks every minute has been fine; if
checks start failing, choose a longer interval. If prices stop loading
altogether, update the library with `py -m pip install -U yfinance`. As a fallback, Trader can read prices from
[Twelve Data](https://twelvedata.com) instead: put `TWELVE_DATA_API_KEY` in
`.env` and this in `config.local.toml`:

```toml
[data]
source = "twelvedata"
```

Its free plan allows 8 requests a minute, so a longer watchlist pauses during
a check.

## Updating

Double-click `update.bat` (or run `python3 -m trader.update`). It downloads the
newest version from GitHub and replaces the program files. Your own files are
never touched: `.env`, `watchlist.txt`, `config.local.toml` and the alert
history. Close Trader and start it again afterwards.

### Without the browser

```sh
python3 -m trader.main --dry-run     # check now, print the result, send nothing
python3 -m trader.main --test-email  # send yourself a test email
python3 -m trader.main --loop        # check every interval_minutes, in the console
```

On Windows use `py` in place of `python3`.

## Tests

```sh
python3 -m unittest discover -s tests
```

## Disclaimer

The alerts are mechanical readings of price and volume, not financial advice.
