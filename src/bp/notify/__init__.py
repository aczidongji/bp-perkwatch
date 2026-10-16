"""Notification channels: SMTP now, Telegram kept as an interface only."""

from .email import SendResult, build_message, send_report

__all__ = ["SendResult", "build_message", "send_report"]
