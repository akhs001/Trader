"""Builds the alert email and sends it, through Gmail or through Resend.

Gmail (SMTP) is used on your own computer. Hosting services such as Railway
block SMTP, so there the email goes through Resend's web API instead: set
RESEND_API_KEY and ALERT_TO.
"""

from __future__ import annotations

import json
import os
import smtplib
import urllib.error
import urllib.request
from email.message import EmailMessage
from html import escape

from .signals import Signal

SMTP_HOST = "smtp.gmail.com"
SMTP_PORT = 465

DISCLAIMER = (
    "Automated technical alert. Entry, SL and TP are calculated by fixed rules "
    "from the price at scan time; they are not predictions and not financial "
    "advice. Check the chart before trading."
)


RESEND_URL = "https://api.resend.com/emails"
# Resend's shared sender: it delivers only to the address of the Resend account.
RESEND_SENDER = "Trader <onboarding@resend.dev>"


class EmailConfigError(Exception):
    pass


class EmailSendError(OSError):
    """The email service refused the message."""


def uses_resend() -> bool:
    return bool(os.environ.get("RESEND_API_KEY", "").strip())


def credentials() -> tuple[str, str, str]:
    """Return (sender, secret, recipient) from the environment.

    The secret is the Resend API key when one is set, else the Gmail app password.
    """
    address = os.environ.get("GMAIL_ADDRESS", "").strip()
    recipient = os.environ.get("ALERT_TO", "").strip() or address
    if uses_resend():
        if not recipient:
            raise EmailConfigError(
                "Email is not set up: set ALERT_TO to the address that should get the alerts."
            )
        sender = (os.environ.get("ALERT_FROM", "").strip()
                  or os.environ.get("ALERT_FROM_EMAIL", "").strip() or RESEND_SENDER)
        return sender, os.environ["RESEND_API_KEY"].strip(), recipient
    password = os.environ.get("GMAIL_APP_PASSWORD", "").replace(" ", "")
    if not address or not password:
        raise EmailConfigError(
            "Email is not set up. On your own computer, put GMAIL_ADDRESS and "
            "GMAIL_APP_PASSWORD in the .env file. On a hosting service, set "
            "RESEND_API_KEY and ALERT_TO. See README.md."
        )
    return address, password, recipient


def send_alerts(signals: list[Signal]) -> str:
    """Email the signals. Returns the address they went to."""
    sender, secret, recipient = credentials()
    deliver(build_message(signals, sender, recipient), sender, secret)
    return recipient


def send_test() -> str:
    """Send a test email. Returns the address it went to."""
    sender, secret, recipient = credentials()
    deliver(test_message(sender, recipient), sender, secret)
    return recipient


def deliver(message: EmailMessage, sender: str, secret: str) -> None:
    if uses_resend():
        send_resend(message, secret)
    else:
        send(message, sender, secret)


def send_resend(message: EmailMessage, api_key: str) -> None:
    """Send through Resend's web API (works where SMTP is blocked)."""
    payload = {"from": message["From"], "to": [message["To"]], "subject": message["Subject"]}
    text, html = message.get_body(("plain",)), message.get_body(("html",))
    if text is not None:
        payload["text"] = text.get_content()
    if html is not None:
        payload["html"] = html.get_content()
    request = urllib.request.Request(
        RESEND_URL, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json",
                 "User-Agent": "trader/1.0"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30):
            pass
    except urllib.error.HTTPError as error:
        try:
            reason = json.load(error).get("message", "")
        except (ValueError, AttributeError):
            reason = ""
        raise EmailSendError(f"Resend refused the email ({error.code}): {reason or error.reason}") from error


def build_message(signals: list[Signal], sender: str, recipient: str) -> EmailMessage:
    entries = [f"{group[0].symbol} {group[0].direction}" for group in by_entry(signals)]
    shown = ", ".join(entries[:5]) + (f" +{len(entries) - 5} more" if len(entries) > 5 else "")
    message = EmailMessage()
    hourly = " (hourly)" if signals and len(signals[0].date) > 10 else ""
    message["Subject"] = f"Entry alert{hourly}: {shown}"
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


def by_entry(signals: list[Signal]) -> list[list[Signal]]:
    """Group signals into entries: one per stock and direction (long or short)."""
    groups: dict[tuple[str, str], list[Signal]] = {}
    for signal in signals:
        groups.setdefault((signal.symbol, signal.direction), []).append(signal)
    return list(groups.values())


def plan_rows(signal: Signal) -> list[tuple[str, str, str]]:
    """(label, price, distance from entry) for each level of the trade plan."""
    plan = signal.plan
    if plan is None:
        return []
    def pct(level: float) -> str:
        return f"{(level / plan.entry - 1) * 100:+.1f}%"
    rows = [("Entry", f"{plan.entry:.2f}", ""), ("SL", f"{plan.stop:.2f}", pct(plan.stop))]
    for i, target in enumerate(plan.targets, start=1):
        label = "TP" if len(plan.targets) == 1 else f"TP{i}"
        rows.append((label, f"{target:.2f}", pct(target)))
    return rows


def plan_note(signal: Signal, several_signals: bool) -> str:
    plan = signal.plan
    ratios = " and ".join(f"{abs(t - plan.entry) / plan.risk:g}x" for t in plan.targets)
    note = f"SL is {plan.stop_basis}. TP is {ratios} the risk of {plan.risk:.2f} per share."
    if several_signals:
        note += f" Plan based on the {signal.title.lower()} signal."
    return note


def _plain_text(signals: list[Signal]) -> str:
    blocks = []
    for group in by_entry(signals):
        s = group[0]
        reasons = "\n".join(f"  * {g.title}: {g.reason}" for g in group)
        facts = "\n".join(f"  {label}: {value}" for label, value in s.facts.items())
        plan = ""
        if s.plan:
            levels = "   ".join(
                f"{label}: {price}" + (f" ({pct})" if pct else "")
                for label, price, pct in plan_rows(s)
            )
            plan = f"  {levels}\n  {plan_note(s, len(group) > 1)}\n"
        blocks.append(
            f"{_heading(s)} - {s.direction.upper()}\n"
            f"  Price: {s.price:.2f} ({s.change_pct:+.2f}% today, {s.date})\n"
            f"{plan}{reasons}\n{facts}\n"
            f"  Chart: {_chart_url(s.symbol)}"
        )
    return "\n\n".join(blocks) + f"\n\n{DISCLAIMER}\n"


def _html(signals: list[Signal]) -> str:
    cards = []
    for group in by_entry(signals):
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
        plan = ""
        if s.plan:
            colors = {"Entry": "#202124", "SL": "#c5221f"}
            cells = "".join(
                f'<td style="padding:8px 6px;text-align:center;border-right:1px solid #ffffff">'
                f'<div style="font-size:11px;font-weight:700;letter-spacing:.04em;color:#5f6368">{label}</div>'
                f'<div style="font-size:16px;font-weight:700;color:{colors.get(label, "#137333")}">{price}</div>'
                f'<div style="font-size:11px;color:#5f6368">{pct or "&nbsp;"}</div></td>'
                for label, price, pct in plan_rows(s)
            )
            plan = (
                f'<table cellpadding="0" cellspacing="0" width="100%" style="border-collapse:collapse;'
                f'background:#f1f3f4;border-radius:6px;margin:12px 0 0"><tr>{cells}</tr></table>'
                f'<div style="font-size:12px;line-height:1.5;color:#5f6368;margin:6px 0 0">'
                f'{escape(plan_note(s, len(group) > 1))}</div>'
            )
        cards.append(f"""
<div style="border:1px solid #dadce0;border-radius:8px;padding:16px;margin:0 0 14px">
  <div style="font-size:20px;font-weight:700;color:#202124">{escape(s.symbol)}
    <span style="font-size:12px;font-weight:700;letter-spacing:.05em;color:#ffffff;padding:2px 8px;border-radius:10px;vertical-align:middle;background:{"#c5221f" if s.direction == "short" else "#137333"}">{s.direction.upper()}</span>
    <span style="font-size:14px;font-weight:400;color:#5f6368">{escape(s.name if s.name != s.symbol else "")}</span></div>
  <div style="font-size:16px;color:#202124;margin:2px 0 0">{s.price:.2f}
    <span style="color:{color}">{s.change_pct:+.2f}%</span>
    <span style="font-size:12px;color:#5f6368">&nbsp;{escape(s.date)}</span></div>
  {plan}
  {reasons}
  <div style="margin:4px 0 0">{facts}</div>
  <p style="margin:14px 0 0"><a href="{_chart_url(s.symbol)}" style="font-size:13px;color:#1a73e8">Open chart</a></p>
</div>""")
    return f"""<html><body style="margin:0;padding:16px;background:#ffffff;font-family:Arial,Helvetica,sans-serif">
<div style="max-width:560px;margin:0 auto">
{''.join(cards)}
<p style="font-size:12px;line-height:1.5;color:#80868b">{escape(DISCLAIMER)}</p>
</div></body></html>"""


def _heading(signal: Signal) -> str:
    if signal.name and signal.name != signal.symbol:
        return f"{signal.symbol} ({signal.name})"
    return signal.symbol


def _chart_url(symbol: str) -> str:
    return f"https://finance.yahoo.com/chart/{symbol}"
