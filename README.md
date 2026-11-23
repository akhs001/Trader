# Trader

A watchlist scanner that runs on your own computer. While the US market is
open it checks the stocks in `watchlist.txt` every 15 minutes and emails you
when one shows a possible entry. It only alerts; it never places trades.

## Signals

All signals use daily candles from Yahoo Finance. During market hours today's
unfinished candle is included, so the bot reacts to the current price.

| Signal | Fires when |
| --- | --- |
| Trend pullback | The stock is in an uptrend (above its 200-day average), dipped to its 20- or 50-day average or to RSI below 40 in the last 5 days, and now trades above yesterday's high. |
| Breakout | Price moves above its 20-day high on at least 1.5x average volume. |
| Oversold bounce | RSI(14) was below 30 yesterday and is back above it. |
| Moving-average cross | The 20-day average crosses above the 50-day average. |
| MACD cross | MACD (12, 26, 9) crosses above its signal line. |

Thresholds and the check interval live in `config.toml`, where each signal can
also be switched off. After a signal is emailed for a stock, the same signal
stays quiet for `cooldown_days` (default 5).

## Setup

1. **Install Python 3.11 or newer** from <https://www.python.org/downloads/>.
   On Windows, tick "Add python.exe to PATH" in the installer. Nothing else
   needs installing.
2. **Download this project** to your computer: on GitHub, *Code → Download
   ZIP*, then unzip it.
3. **Create a Gmail app password.** Turn on 2-Step Verification for your
   Google account, then create a password at
   <https://myaccount.google.com/apppasswords>. It is 16 letters long.
4. **Add your email details.** Copy `.env.example` to a new file named `.env`
   in the same folder and fill in your Gmail address and the app password.
5. **Edit `watchlist.txt`** with your own tickers, one per line.

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

Yahoo Finance blocks requests from cloud servers (GitHub Actions and similar),
which is why the bot runs from a home connection.

## Tests

```sh
python3 -m unittest discover -s tests
```

## Disclaimer

The alerts are mechanical readings of price and volume, not financial advice.
