from __future__ import annotations

import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formatdate, make_msgid

from ..config import get_settings
from ..report import Report

# ASCII attachment names avoid RFC 2231 filename-encoding quirks in mail clients.
ATTACHMENT_STEM = "perk-report"


def attachment_name(report: Report) -> str:
    return f"{ATTACHMENT_STEM}-{report.day}.md"


@dataclass
class SendResult:
    ok: bool
    recipients: list[str] = field(default_factory=list)
    attachment: str | None = None
    error: str | None = None
    dry_run: bool = False
    bytes: int = 0


def build_message(report: Report, *, to: list[str], sender: str,
                  subject: str | None = None) -> EmailMessage:
    """One MIME message carrying: plain Markdown, an HTML rendition for reading
    inline in Gmail, and the .md file as a downloadable attachment.

    ``set_content`` + ``add_alternative`` produce multipart/alternative; adding the
    attachment at the top level promotes the whole message to multipart/mixed, which
    is what mail clients expect for "readable body + file".
    """
    message = EmailMessage()
    message["Subject"] = subject or f"AI 福利日报 {report.day}"
    message["From"] = sender
    message["To"] = ", ".join(to)
    message["Date"] = formatdate(localtime=True)
    message["Message-ID"] = make_msgid()

    message.set_content(report.markdown, subtype="plain", charset="utf-8")
    message.add_alternative(report.html, subtype="html", charset="utf-8")
    message.add_attachment(
        report.markdown.encode("utf-8"),
        maintype="text",
        subtype="markdown",
        filename=attachment_name(report),
    )
    return message


def send_report(report: Report, *, dry_run: bool = False,
                recipients: list[str] | None = None) -> SendResult:
    settings = get_settings()
    to = recipients or settings.recipients()
    if not to:
        return SendResult(ok=False, error="未配置收件人 (REPORT_TO)")

    sender = settings.smtp_from or settings.smtp_username
    message = build_message(report, to=to, sender=sender or "undisclosed@localhost")
    size = len(message.as_bytes())
    name = attachment_name(report)

    if dry_run:
        return SendResult(ok=True, recipients=to, attachment=name, dry_run=True, bytes=size)

    if not (settings.smtp_username and settings.smtp_password):
        return SendResult(ok=False, recipients=to, attachment=name, bytes=size,
                          error="SMTP 凭据缺失 (SMTP_USERNAME / SMTP_PASSWORD)")

    try:
        context = ssl.create_default_context()
        if settings.smtp_use_ssl:
            with smtplib.SMTP_SSL(settings.smtp_host, settings.smtp_port,
                                  context=context, timeout=45) as server:
                server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(message)
        else:
            with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=45) as server:
                server.ehlo()
                server.starttls(context=context)
                server.ehlo()
                server.login(settings.smtp_username, settings.smtp_password)
                server.send_message(message)
    except Exception as exc:  # noqa: BLE001 - surfaced to the caller and recorded
        return SendResult(ok=False, recipients=to, attachment=name, bytes=size,
                          error=f"{type(exc).__name__}: {exc}"[:400])

    return SendResult(ok=True, recipients=to, attachment=name, bytes=size)
