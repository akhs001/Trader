"""Remembers which alerts were already emailed, so each is sent only once."""

from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path

from .signals import Signal


class AlertState:
    def __init__(self, path: Path):
        self.path = path
        self.sent: dict[str, str] = {}  # "SYMBOL:kind" -> date of the last alert
        if path.exists():
            try:
                self.sent = dict(json.loads(path.read_text()).get("sent", {}))
            except (json.JSONDecodeError, AttributeError):
                self.sent = {}  # a corrupt file only costs a repeated alert

    def is_new(self, signal: Signal, cooldown_days: int, cooldown_hours: int = 24) -> bool:
        """Whether the signal may be emailed: not sent within its cooldown.

        Daily signals wait `cooldown_days`; hourly ones wait `cooldown_hours`.
        """
        last = self.sent.get(_key(signal))
        if last is None:
            return True
        gap = datetime.fromisoformat(signal.date) - datetime.fromisoformat(last)
        if len(signal.date) > 10:
            return gap >= timedelta(hours=max(cooldown_hours, 1))
        return gap >= timedelta(days=max(cooldown_days, 1))

    def record(self, signals: list[Signal]) -> None:
        for signal in signals:
            self.sent[_key(signal)] = signal.date

    def save(self) -> None:
        cutoff = (date.today() - timedelta(days=90)).isoformat()
        self.sent = {k: v for k, v in self.sent.items() if v >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"sent": self.sent}, indent=2, sort_keys=True))


def _key(signal: Signal) -> str:
    # Hourly signals are remembered separately from daily ones.
    hourly = ":1h" if len(signal.date) > 10 else ""
    return f"{signal.symbol}:{signal.kind}{hourly}"
