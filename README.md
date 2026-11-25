# Trader

A watchlist scanner that runs on your own computer and opens in your browser.
Press **Check entries** and it checks every stock on your watchlist for a
possible entry, shows each one with Entry, SL, TP and an exit rule, and can
email them to you. It only alerts; it never places trades.

## Strategies

The entry rules come from what professional and systematic traders publish,
and each one was then tested on nine years of prices for the S&P 500 members
before being made a default. Three principles apply to every strategy:

- **Mind the market.** The page shows whether the S&P 500 (the SPY fund) is
  above its 200-day average, the "200-day rule" used by Paul Tudor Jones and
  in Meb Faber's timing model. Strategies that tested worse against the
  market use it as a filter: they look for longs only above the line and for
  shorts only below it. Dips use the filter; gaps do not.
- **Buy leaders.** Most strategies need the stock to pass Mark Minervini's
  *trend template* first: price above its 50-, 150- and 200-day averages,
  those averages stacked in that order, the 200-day rising, price within 25%
  of its one-year high and at least 30% above its one-year low, and not weaker
  than the market over the last six months.
- **Leave by rule.** Every plan has a stop and an exit rule, so a loss stays
  small and a winner is not cut short by a fixed target.

### Tested strategies (on by default)

| Strategy | Entry | SL | Exit |
| --- | --- | --- | --- |
| **Dip in a leader** (Larry Connors' 2-day RSI, restricted to leaders) | A leader sells off sharply for a day or two: RSI(2) falls below 10. | 2 ATR below the entry. | The first close back above the 10-day average. That average is the TP shown. |
| **Gap on heavy volume** (the drift after news or earnings) | The stock opens at least 1.5 ATR above yesterday's close on at least twice its 50-day average volume, never fills the gap, and closes in the upper half of the day's range. | Just below the gap day's low. | The first close below the low of the previous 20 days. |

### Unproven strategies (off; run when you pick one by name)

| Strategy | Entry | SL | Exit |
| --- | --- | --- | --- |
| **Base breakout** (William O'Neil, Minervini) | A leader closes above the top of a five-week base that has tightened, on volume 40% above average, at most 5% past it. | Below the low of the base's last days, at most 8% away. | The first close below the 20-day average. |
| **Pullback with a turn** | A leader dips at least 2 ATR to its 20- or 50-day average, then closes above the previous day's high. | Below the pullback low. | The first close below the 10-day low. |
| **55-day breakout** (the Turtles' long-term system) | A close above the high of the last 55 days. | 2 ATR below the entry. | The first close below the 20-day low. |

Every strategy also has a short form, the same rule upside down, which Trader
only looks for when you choose "Long and short" or "Short only".

### What the testing showed

All five strategies were replayed on about 500 current S&P 500 members from
2017 to 2026 with the rules above, and compared with entering on a random day
with the same stop and exit.

- **Random entries did about as well as the breakout and pullback rules**
  (around +0.25R per trade either way). In that period most of the profit came
  from holding rising stocks with a stop and a trailing exit, not from the
  entry signal. That is why those three are marked unproven.
- **Dip in a leader** made 7,300 trades with 66% winners, about +0.36% per
  trade in 4 days, against +0.14% in 2 days for a random day in the same
  stocks. It wins often and small: the target is the nearby average (about
  2.5% away on average) while the stop is about 5% away, so it has to keep
  winning about two times in three. A stop of 3 ATR won 70% of the time, and
  one of 1 ATR only 51%.
- **Gap on heavy volume** made 1,400 trades with only 30% winners, but large
  ones: about +3.0% per trade over a month, roughly twice the random-entry
  result per day held.
- **No short form had a reliable edge**, with or without the market filter.
  The short form of the dip (a sharp bounce in a falling stock) about broke
  even over the nine years and lost heavily in the first five.
- **The market filter helps some strategies and not others.** Dips bought
  while the market was below its 200-day average lost money (-0.08% per trade
  against +0.36% above the line). Gaps did as well below the line as above
  it. And simply staying out of the index below the line would have cut its
  return from 14% to 7% a year over this period, because every fall was
  followed by a fast recovery; it did reduce the deepest loss.
- **Nothing tested beat holding the index after allowing for risk.** The S&P
  500 fund itself returned about 14% a year over the period. A monthly
  portfolio of the ten strongest stocks of the past year returned more (about
  20% a year) with deeper losses along the way and a weak first half; the
  twenty weakest returned 3% a year.

Read these numbers with care. The test used today's index members, which
leaves out companies that failed and so flatters long trades. It ignores
commissions and slippage, which matter most for the small, frequent dip
trades. It covers one mostly rising decade of large US stocks. And the
strategies were not tested on hourly candles at all. An edge of this size
means many losing trades and losing months; keep the risk per trade small.

## Trade plan

Every alert comes with a plan:

- **Entry** is the price at the moment of the scan.
- **SL** is the strategy's stop from the tables above, kept between 1 and 3
  ATR from the entry (ATR is the stock's average daily range).
- **TP.** A dip aims at its 10-day average, shown as TP. Strategies that let a
  winner run show TP1 and TP2 at 2 and 3 times the risk as reference points.
- **Exit rule.** The condition that ends the trade, as in the tables. Trader
  does not watch open trades for you: check the rule each day after the close.

When a stock fires several signals in the same direction at once, the plan
follows the first one listed.

## Settings

Thresholds, the trade-plan rules and the check interval live in `config.toml`,
with a comment on every setting. Do not edit that file, because updates
replace it. Create `config.local.toml` next to it instead and put only the
settings you want to change in it:

```toml
[signals.dip]
rsi_level = 5          # only the sharpest dips

[signals.pullback]
enabled = true         # add an unproven strategy to the default set

[market]
filter = false         # look for entries whatever the market is doing
```

After a signal is emailed for a stock, the same signal stays quiet for
`cooldown_days` (default 5).

Prices are daily candles from Yahoo Finance, read through the
[yfinance](https://github.com/ranaroussi/yfinance) package. During market hours
today's unfinished candle is included, so a check uses the current price.

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

- **Strategy** and **Direction** choose what a check looks for. "Tested
  strategies" runs the default set; picking a strategy by name runs that one,
  unproven or not. Direction starts at "Long only": shorts are looked for only
  when you choose them. The choice is remembered and also applies to automatic
  checks and emails.
- **The market line** under the button says whether the S&P 500 is above its
  200-day average. When it is below, no long entries are shown.
- **Candles** chooses the timeframe. *Daily* looks for swing entries on daily
  candles. *Hourly (day trading)* runs the same strategies on hourly candles:
  every "day" in a rule becomes an hour (the 20-hour average, the previous
  hour's high), and SL and TP are sized from the hourly range, so they sit
  much closer. Hourly alerts repeat after `cooldown_hours` (24) at the
  earliest.
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

### Scanner tab

The Scanner looks for entries beyond your watchlist. Pick a list of stocks
(Nasdaq 100, S&P 500, Dow Jones 30, or Yahoo's most active, top gainers or top
losers of the day), a strategy and a direction, and press **Scan market**. It
downloads daily prices in batches and lists every stock with an entry today,
with its Entry, SL and TP. **+ Watch** adds a stock to your watchlist. The
Dow takes seconds, the Nasdaq 100 about a minute, the S&P 500 several minutes.

A scan only shows results; it never emails. Index members are read from a
public list on GitHub when you scan, with a copy saved in the project for
when that list cannot be reached.

### Backtest tab

The Backtest tab shows how the strategies would have done on one stock's past
prices. You choose:

- **Strategy** and **Direction**: the tested set or one strategy, long, short
  or both.
- **Candles**: daily, or hourly to test day trading. On hourly candles the
  signals, the entries and the exits all come from hourly prices, which reach
  back about two years.
- **Ticker**: any ticker, on your watchlist or not.
- **Period**: the last 1, 2 or 5 years.
- **Exit**: the strategy's own exit rule (the default), or a fixed TP1 or TP2.
- **Close after**: an optional time limit on a trade, from 1 hour to 60 days.
  Choices in hours follow each trade on hourly prices, which reach back about
  two years.
- **Start budget** and **Risk per trade**: the money you start with, and the
  share of it a trade loses if its SL is hit.

**Run backtest** replays the period day by day, scanning each past day exactly
as a live check would have, and trades every signal by its own plan.

The page shows the budget at the end, the profit, the largest drawdown, the
number of trades, win rate, average result and profit factor (gains divided
by losses; above 1 is profitable), a chart of the budget over time, a table
per strategy and side, and every trade. Results per trade are in R: 1R is the
amount risked on the trade, the distance from entry to SL. A trade stopped out
loses 1R.

What the backtest assumes:

- A trade enters at the close of the signal day.
- If SL and TP are both touched in the same candle (day, or hour when the
  holding time is in hours), the SL counts.
- A price that opens beyond the SL or TP exits at that opening price.
- One trade at a time per strategy and side.
- The market filter applies on every past day, as it does live.
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

## Running in the cloud

Trader can run on a hosting service instead of your PC, so automatic checks
and emails keep going while your computer is off, and the page works from your
phone. On a host, Trader:

- asks for a password before showing anything (it will not start without one),
- keeps your watchlist, settings and alert history in a folder you name, so
  they survive new versions,
- sends email through [Resend](https://resend.com), because hosts block the
  Gmail connection,
- updates by itself whenever a new version is pushed to GitHub.

Steps for [Railway](https://railway.com) (a paid service, about $5 a month):

1. **New Project → Deploy from GitHub repo** and choose this repository.
2. On the service, open **Variables** and add:

   | Variable | Value |
   | --- | --- |
   | `TRADER_PASSWORD` | The password for the page, at least 8 characters |
   | `TRADER_DATA_DIR` | `/data` |
   | `RESEND_API_KEY` | An API key from your Resend account |
   | `ALERT_TO` | The address that should get the alerts |
   | `ALERT_FROM` | Optional. A sender on a domain you verified at Resend |

   Without `ALERT_FROM` the sender is Resend's shared address, which only
   delivers to the email address of your own Resend account.
3. Add a **Volume** to the service and mount it at `/data`. Without it the
   watchlist is lost at every new version.
4. Under **Settings → Networking**, generate a public domain.
5. Open that address, sign in, and set up your watchlist. Tick **Check
   automatically** to get emails while your PC is off.

The start command and health check come from `railway.toml` in this
repository. Another host works the same way as long as it sets `PORT`, or you
set `TRADER_HOST=0.0.0.0` yourself.

Use a long password and keep it to yourself: anyone who has it can read and
change your watchlist. Ten wrong passwords lock sign-in for five minutes.

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
