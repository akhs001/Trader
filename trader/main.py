"""Scan the watchlist and email any new entry signals.

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
import tomllib
from datetime import datetime
from pathlib import Path

from . import emailer, market
from .data import DataError, fetch_history
from .signals import MIN_BARS, Signal, scan
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


def load_watchlist(path: Path) -> list[str]:
    symbols = []
    for line in path.read_text().splitlines():
        symbol = line.split("#", 1)[0].strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def scan_once(args: argparse.Namespace) -> int:
    """Scan every stock once. Returns 0 on success, 1 if no prices could be read."""
    # Read on every scan, so edits take effect without restarting the bot.
    config = tomllib.loads(args.config.read_text())
    symbols = load_watchlist(args.watchlist)
    if not symbols:
        print(f"The watchlist {args.watchlist} is empty; nothing to scan.")
        return 0

    signals: list[Signal] = []
    failed: list[str] = []
    for symbol in symbols:
        try:
            history = fetch_history(symbol)
        except DataError as error:
            print(f"  ! {error}")
            failed.append(symbol)
            continue
        found = scan(history, config)
        signals.extend(found)
        note = ", ".join(s.title for s in found) if found else "no entry"
        if len(history.closes) < MIN_BARS:
            note = f"skipped, only {len(history.closes)} days of history"
        print(f"  {symbol:<6} {history.closes[-1]:>10.2f}  {history.dates[-1]}  {note}")
        time.sleep(0.5)  # stay polite to the data provider

    state = AlertState(args.state)
    cooldown = int(config.get("alerts", {}).get("cooldown_days", 5))
    fresh = [s for s in signals if state.is_new(s, cooldown)]
    print(f"{len(signals)} signal(s) firing, {len(fresh)} not yet emailed.")

    if fresh and args.dry_run:
        print("Dry run, not sending. The email would say:\n")
        print(emailer.build_message(fresh, "you", "you").get_body(("plain",)).get_content())
    elif fresh:
        sender, password, recipient = emailer.credentials()
        emailer.send(emailer.build_message(fresh, sender, recipient), sender, password)
        print(f"Emailed {len(fresh)} alert(s) to {recipient}.")
        # Recorded only after a successful send, so a failed send is retried.
        state.record(fresh)
        state.save()

    if failed:
        print(f"Could not get prices for: {', '.join(failed)}")
        # Fail only if nothing worked, so one bad ticker does not hide the
        # rest of the watchlist behind an error.
        return 1 if len(failed) == len(symbols) else 0
    return 0


def run_loop(args: argparse.Namespace) -> None:
    """Scan every few minutes during US market hours until stopped."""
    emailer.credentials()  # fail now, not at the first signal, if email is not set up
    print("Trader bot is running. Leave this window open; press Ctrl+C to stop.")
    waiting = False
    while True:
        config = tomllib.loads(args.config.read_text())
        interval = max(int(config.get("schedule", {}).get("interval_minutes", 15)), 1)
        if market.is_scan_time(grace_minutes=interval):
            waiting = False
            print(f"\n[{datetime.now():%a %H:%M}] Scanning...")
            try:
                scan_once(args)
            except smtplib.SMTPAuthenticationError:
                print(f"ERROR: {GMAIL_LOGIN_HELP}")
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
    parser.add_argument("--dry-run", action="store_true",
                        help="print signals without emailing or remembering them")
    parser.add_argument("--test-email", action="store_true",
                        help="send a test email and exit")
    args = parser.parse_args(argv)
    load_env_file(ROOT / ".env")

    if args.test_email:
        sender, password, recipient = emailer.credentials()
        emailer.send(emailer.test_message(sender, recipient), sender, password)
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
    except emailer.EmailConfigError as error:
        print(f"ERROR: {error}")
        sys.exit(2)
    except smtplib.SMTPAuthenticationError:
        print(f"ERROR: {GMAIL_LOGIN_HELP}")
        sys.exit(2)
