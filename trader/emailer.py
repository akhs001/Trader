"""Builds and sends the alert email through Gmail."""

from __future__ import annotations

import os
import smtplib
from email.message import EmailMessage
from html import escape

from .signals import Signal

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

DISCLAIMER = (
    "Automated technical alert based on delayed price data. "
    "It is not financial advice; check the chart before trading."
)


class EmailConfigError(Exception):
    pass


def credentials() -> tuple[str, str, str]:
    """Return (gmail_address, app_password, recipient) from the environment."""
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not address or not password:
        raise EmailConfigError(
            "Email is not set up: put GMAIL_ADDRESS and GMAIL_APP_PASSWORD "
            "in the .env file. See README.md."
        )
    recipient = os.environ.get("ALERT_TO", "").strip() or address
    return address, password, recipient


def build_message(signals: list[Signal], sender: str, recipient: str) -> EmailMessage:
    symbols = list(dict.fromkeys(s.symbol for s in signals))
    shown = ", ".join(symbols[:5]) + (f" +{len(symbols) - 5} more" if len(symbols) > 5 else "")
    message = EmailMessage()
    message["Subject"] = f"Entry alert: {shown}"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(_plain_text(signals))
    message.add_alternative(_html(signals), subtype="html")
    return message


def send(message: EmailMessage, sender: str, password: str) -> None:
    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=30) as smtp:
        smtp.login(sender, password)
        smtp.send_message(message)


def test_message(sender: str, recipient: str) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = "Trader bot: test email"
    message["From"] = sender
    message["To"] = recipient
    message.set_content(
        "Email is set up correctly. You will get a message like this one "
        "whenever a stock on your watchlist shows a possible entry."
    )
    return message


def _by_stock(signals: list[Signal]) -> list[list[Signal]]:
    groups: dict[str, list[Signal]] = {}
    for signal in signals:
        groups.setdefault(signal.symbol, []).append(signal)
    return list(groups.values())


def _plain_text(signals: list[Signal]) -> str:
    blocks = []
    for group in _by_stock(signals):
        s = group[0]
        reasons = "\n".join(f"  * {g.title}: {g.reason}" for g in group)
        facts = "\n".join(f"  {label}: {value}" for label, value in s.facts.items())
        blocks.append(
            f"{s.symbol} ({s.name})\n"
            f"  Price: {s.price:.2f} ({s.change_pct:+.2f}% today, {s.date})\n"
            f"{reasons}\n{facts}\n"
            f"  Chart: {_chart_url(s.symbol)}"
        )
    return "\n\n".join(blocks) + f"\n\n{DISCLAIMER}\n"


def _html(signals: list[Signal]) -> str:
    cards = []
    for group in _by_stock(signals):
        s = group[0]
        color = "#137333" if s.change_pct >= 0 else "#c5221f"
        reasons = "".join(
            f'<div style="margin:12px 0 0">'
            f'<div style="font-size:12px;font-weight:700;letter-spacing:.04em;'
            f'text-transform:uppercase;color:#1a73e8">{escape(g.title)}</div>'
            f'<div style="font-size:14px;line-height:1.5;color:#202124">{escape(g.reason)}</div></div>'
            for g in group
        )
        facts = "".join(
            f'<span style="display:inline-block;margin:10px 16px 0 0;font-size:13px;color:#5f6368">'
            f'{escape(label)}<br><span style="color:#202124;font-weight:600">{escape(value)}</span></span>'
            for label, value in s.facts.items()
        )
        cards.append(f"""
<div style="border:1px solid #dadce0;border-radius:8px;padding:16px;margin:0 0 14px">
  <div style="font-size:20px;font-weight:700;color:#202124">{escape(s.symbol)}
    <span style="font-size:14px;font-weight:400;color:#5f6368">{escape(s.name)}</span></div>
  <div style="font-size:16px;color:#202124;margin:2px 0 0">{s.price:.2f}
    <span style="color:{color}">{s.change_pct:+.2f}%</span>
    <span style="font-size:12px;color:#5f6368">&nbsp;{escape(s.date)}</span></div>
  {reasons}
  <div style="margin:4px 0 0">{facts}</div>
  <p style="margin:14px 0 0"><a href="{_chart_url(s.symbol)}" style="font-size:13px;color:#1a73e8">Open chart</a></p>
</div>""")
    return f"""<html><body style="margin:0;padding:16px;background:#ffffff;font-family:Arial,Helvetica,sans-serif">
<div style="max-width:560px;margin:0 auto">
{''.join(cards)}
<p style="font-size:12px;line-height:1.5;color:#80868b">{escape(DISCLAIMER)}</p>
</div></body></html>"""


def _chart_url(symbol: str) -> str:
    return f"https://finance.yahoo.com/chart/{symbol}"
