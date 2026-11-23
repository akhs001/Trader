"""Scan the watchlist and email any new entry signals.

Usage:
    python -m trader.main               scan and email new signals
    python -m trader.main --dry-run     scan and print, send nothing
    python -m trader.main --test-email  send a test email and exit
"""

from __future__ import annotations

import argparse
import smtplib
import sys
import time
import tomllib
from pathlib import Path

from . import emailer
from .data import DataError, fetch_history
from .signals import MIN_BARS, Signal, scan
from .state import AlertState

ROOT = Path(__file__).resolve().parent.parent


def load_watchlist(path: Path) -> list[str]:
    symbols = []
    for line in path.read_text().splitlines():
        symbol = line.split("#", 1)[0].strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)
    return symbols


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--watchlist", type=Path, default=ROOT / "watchlist.txt")
    parser.add_argument("--config", type=Path, default=ROOT / "config.toml")
    parser.add_argument("--state", type=Path, default=ROOT / ".state" / "alerts.json")
    parser.add_argument("--dry-run", action="store_true",
                        help="print signals without emailing or remembering them")
    parser.add_argument("--test-email", action="store_true",
                        help="send a test email and exit")
    args = parser.parse_args(argv)

    if args.test_email:
        sender, password, recipient = emailer.credentials()
        emailer.send(emailer.test_message(sender, recipient), sender, password)
        print("Test email sent.")
        return 0

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
        time.sleep(0.3)  # stay polite to the data provider

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
        print(f"Emailed {len(fresh)} alert(s).")
        # Recorded only after a successful send, so a failed send is retried.
        state.record(fresh)
        state.save()

    if failed:
        print(f"Could not get prices for: {', '.join(failed)}")
        # Fail the run only if nothing worked, so one bad ticker does not
        # hide the rest of the watchlist behind an error.
        return 1 if len(failed) == len(symbols) else 0
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except emailer.EmailConfigError as error:
        print(f"ERROR: {error}")
        sys.exit(2)
    except smtplib.SMTPAuthenticationError:
        print(
            "ERROR: Gmail rejected the login. GMAIL_APP_PASSWORD must be a 16-character "
            "app password (not your normal Gmail password). See README.md."
        )
        sys.exit(2)
