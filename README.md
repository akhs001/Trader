# Trader

A watchlist scanner. Every hour during US market hours it checks the stocks in
`watchlist.txt` and emails you when one shows a possible entry. It only
alerts; it never places trades.

## Signals

All signals use daily candles. During market hours today's unfinished candle
is included, with prices about 15 minutes delayed.

| Signal | Fires when |
| --- | --- |
| Trend pullback | The stock is in an uptrend (above its 200-day average), dipped to its 20- or 50-day average or to RSI below 40 in the last 5 days, and now trades above yesterday's high. |
| Breakout | Price moves above its 20-day high on at least 1.5x average volume. |
| Oversold bounce | RSI(14) was below 30 yesterday and is back above it. |
| Moving-average cross | The 20-day average crosses above the 50-day average. |
| MACD cross | MACD (12, 26, 9) crosses above its signal line. |

Thresholds live in `config.toml`, where each signal can also be switched off.
After a signal is emailed for a stock, the same signal stays quiet for
`cooldown_days` (default 5).

## Setup

1. **Create a Gmail app password.** Turn on 2-Step Verification for your
   Google account, then create a password at
   <https://myaccount.google.com/apppasswords>. It is 16 characters long.
2. **Add repository secrets** under *Settings → Secrets and variables →
   Actions → New repository secret*:

   | Secret | Value |
   | --- | --- |
   | `GMAIL_ADDRESS` | The Gmail address that sends the alerts |
   | `GMAIL_APP_PASSWORD` | The app password from step 1 |
   | `ALERT_TO` | Optional. Where to send alerts, if not `GMAIL_ADDRESS` |

3. **Send a test email.** Open *Actions → Watchlist scan → Run workflow*,
   choose `test-email`, and run it.
4. **Edit `watchlist.txt`** with your own tickers, one per line.

The schedule in `.github/workflows/scan.yml` only runs from the repository's
default branch. GitHub pauses schedules in a repository with no activity for
60 days and emails you before it does.

## Running it yourself

Needs Python 3.11 or newer and nothing else.

```sh
python3 -m trader.main --dry-run     # print signals, send nothing
python3 -m trader.main --test-email  # needs GMAIL_ADDRESS and GMAIL_APP_PASSWORD
python3 -m trader.main               # scan and email new signals
python3 -m unittest discover -s tests
```

## Disclaimer

The alerts are mechanical readings of price and volume, not financial advice.
