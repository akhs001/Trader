"""Scan the watchlist and email any new entry signals, from the console.

The app with buttons is `python -m trader.app`.

Usage:
    python -m trader.main --loop        keep running; scan during market hours
    python -m trader.main               scan once and email new signals
    python -m trader.main --dry-run     scan once and print, send nothing
    python -m trader.main --test-email  send a test email and exit
"""

from __future__ import annotations

import argparse
import os
import smtplib
import sys
import time
from datetime import datetime
from pathlib import Path

from . import emailer, market
from .data import SetupError, check_setup, source_of
from .scanner import (StockResult, check_watchlist, email_signals, fetch_benchmark, load_config,
                      load_watchlist, new_signals)
from .signals import market_trend
from .state import AlertState

ROOT = Path(__file__).resolve().parent.parent

GMAIL_LOGIN_HELP = (
    "Gmail rejected the login. GMAIL_APP_PASSWORD must be a 16-character "
    "app password (not your normal Gmail password). See README.md."
)


def load_env_file(path: Path) -> None:
    """Read KEY=VALUE lines from a .env file into the environment."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


def scan_once(args: argparse.Namespace) -> int:
    """Scan every stock once. Returns 0 on success, 1 if no prices could be read."""
    # Read on every scan, so edits take effect without restarting the bot.
    config = load_config(args.config)
    symbols = load_watchlist(args.watchlist)
    if not symbols:
        print(f"The watchlist {args.watchlist} is empty; nothing to scan.")
        return 0

    def show(result: StockResult) -> None:
        if result.error:
            print(f"  ! {result.symbol}: {result.error}")
            return
        note = ", ".join(s.title for s in result.signals) or "no entry"
        if result.note:
            note = f"skipped, {result.note}"
        print(f"  {result.symbol:<6} {result.price:>10.2f}  {result.date}  {note}")

    notify = lambda text: print(f"  ({text})")
    benchmark = fetch_benchmark(config, notify)
    up = market_trend(benchmark)
    if up:
        state_now = "above" if list(up.values())[-1] else "below"
        print(f"  The market is {state_now} its 200-day average: "
              f"{'long' if state_now == 'above' else 'short'} entries only.")
    # Shorts lost money in testing, so the console looks for longs unless asked.
    direction = "both" if args.shorts else "long"
    results = check_watchlist(symbols, config, on_result=show, notify=notify,
                              direction=direction, benchmark=benchmark)
    state = AlertState(args.state)
    fresh = new_signals(results, state, config)
    firing = sum(len(r.signals) for r in results)
    print(f"{firing} signal(s) firing, {len(fresh)} not yet emailed.")

    if fresh and args.dry_run:
        print("Dry run, not sending. The email would say:\n")
        print(emailer.build_message(fresh, "you", "you").get_body(("plain",)).get_content())
    elif fresh:
        recipient = email_signals(fresh, state)
        print(f"Emailed {len(fresh)} alert(s) to {recipient}.")

    failed = [r.symbol for r in results if r.error]
    if failed:
        print(f"Could not get prices for: {', '.join(failed)}")
        # Fail only if nothing worked, so one bad ticker does not hide the
        # rest of the watchlist behind an error.
        return 1 if len(failed) == len(symbols) else 0
    return 0


def run_loop(args: argparse.Namespace) -> None:
    """Scan every few minutes during US market hours until stopped."""
    # Fail now, not at the first scan or signal, if the setup is incomplete.
    check_setup(source_of(load_config(args.config)))
    emailer.credentials()
    print("Trader bot is running. Leave this window open; press Ctrl+C to stop.")
    waiting = False
    while True:
        config = load_config(args.config)
        interval = max(int(config.get("schedule", {}).get("interval_minutes", 15)), 1)
        if market.is_scan_time(grace_minutes=interval):
            waiting = False
            print(f"\n[{datetime.now():%a %H:%M}] Scanning...")
            try:
                scan_once(args)
            except smtplib.SMTPAuthenticationError:
                print(f"ERROR: {GMAIL_LOGIN_HELP}")
            except SetupError as error:
                print(f"ERROR: {error}")
            except Exception as error:  # keep running through e.g. a network outage
                print(f"Scan failed, will retry: {error}")
            print(f"Next scan in {interval} minutes.")
            time.sleep(interval * 60)
        else:
            if not waiting:
                print(f"\n[{datetime.now():%a %H:%M}] US market is closed. "
                      "Waiting for the next session (weekdays 9:30-16:00 New York time).")
                waiting = True
            time.sleep(60)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--watchlist", type=Path, default=ROOT / "watchlist.txt")
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--state", type=Path, default=ROOT / ".state" / "alerts.json")
    parser.add_argument("--loop", action="store_true",
                        help="keep running and scan during US market hours")
    parser.add_argument("--shorts", action="store_true",
                        help="also look for short entries")
    parser.add_argument("--dry-run", action="store_true",
                        help="print signals without emailing or remembering them")
    parser.add_argument("--test-email", action="store_true",
                        help="send a test email and exit")
    args = parser.parse_args(argv)
    load_env_file(ROOT / ".env")

    if args.test_email:
        recipient = emailer.send_test()
        print(f"Test email sent to {recipient}.")
        return 0
    if args.loop:
        run_loop(args)
        return 0
    return scan_once(args)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nStopped.")
    except (emailer.EmailConfigError, SetupError) as error:
        print(f"ERROR: {error}")
        sys.exit(2)
    except smtplib.SMTPAuthenticationError:
        print(f"ERROR: {GMAIL_LOGIN_HELP}")
        sys.exit(2)
