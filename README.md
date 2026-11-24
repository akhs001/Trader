# Trader

A watchlist scanner that runs on your own computer. While the US market is
open it checks the stocks in `watchlist.txt` every 15 minutes and emails you
when one shows a possible entry. It only alerts; it never places trades.

## Signals

All signals use daily candles from [Twelve Data](https://twelvedata.com).

| Signal | Fires when |
| --- | --- |
| Trend pullback | The stock is in an uptrend (above its 200-day average), dipped at least 2 ATR to its 20- or 50-day average or to RSI below 40 in the last 5 days, and now trades above yesterday's high for the first time. |
| Breakout | Price moves above its 20-day high on at least 1.5x average volume. |
| Oversold bounce | RSI(14) was below 30 yesterday and is back above it. |
| Moving-average cross | The 20-day average crosses above the 50-day average. |
| MACD cross | MACD (12, 26, 9) crosses above its signal line. |

## Trade plan

Every alert comes with Entry, SL, TP1 and TP2:

- **Entry** is the price at the moment of the scan.
- **SL** sits just below the price that would prove the setup wrong: the
  pullback low, the breakout level, or the recent low. It is kept between 1
  and 3 ATR from the entry (ATR is the stock's average daily range), so it is
  neither inside everyday noise nor unreasonably far. Average and MACD crosses
  have no such price, so their SL is 2 ATR below the entry.
- **TP1 and TP2** are 2 and 3 times the risk (entry minus SL) above the entry.

When a stock fires several signals at once, the plan follows the first one
listed. The levels come from fixed rules, not from a forecast, and have not
been backtested.

## Settings

Thresholds, the trade-plan rules and the check interval live in `config.toml`,
where each signal can also be switched off. After a signal is emailed for a stock, the same signal
stays quiet for `cooldown_days` (default 5).

## Setup

1. **Install Python 3.11 or newer** from <https://www.python.org/downloads/>.
   On Windows, tick "Add python.exe to PATH" in the installer. Nothing else
   needs installing.
2. **Download this project** to your computer: on GitHub, *Code → Download
   ZIP*, then unzip it.
3. **Get a free Twelve Data API key.** Sign up at <https://twelvedata.com>
   and copy the API key from your dashboard.
4. **Create a Gmail app password.** Turn on 2-Step Verification for your
   Google account, then create a password at
   <https://myaccount.google.com/apppasswords>. It is 16 letters long.
5. **Add your details.** Copy `.env.example` to a new file named `.env` in the
   same folder and fill in the API key, your Gmail address and the app password.
6. **Edit `watchlist.txt`** with your own tickers, one per line.

## Running

Open a terminal in the project folder (on Windows: type `cmd` in the folder's
address bar). On Windows use `py` in place of `python3`.

```sh
python3 -m trader.main --dry-run     # check the stocks now, print the result, send nothing
python3 -m trader.main --test-email  # send yourself a test email
python3 -m trader.main --loop        # start the bot
```

On Windows you can also start the bot by double-clicking `start.bat`.

Leave the window open. The bot scans on weekdays from 9:30 to 16:00 New York
time and waits quietly outside those hours. Your computer has to be on and
awake for it to work. Changes to `watchlist.txt` and `config.toml` are picked
up at the next scan without restarting.

The bot uses one Twelve Data request per stock per scan. The free plan limits
requests per minute and per day: with a long watchlist the bot pauses when it
reaches the per-minute limit, and if you run out for the day, shorten the
watchlist or raise `interval_minutes` in `config.toml`.

## Tests

```sh
python3 -m unittest discover -s tests
```

## Disclaimer

The alerts are mechanical readings of price and volume, not financial advice.
