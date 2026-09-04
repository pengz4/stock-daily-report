"""Webhook notification adapters for published daily reports."""

from stock_daily_report.notify.base import (
    NotificationDeliveryError,
    NotificationOutcome,
    NotificationSummary,
)
from stock_daily_report.notify.service import NotificationService

__all__ = [
    "NotificationDeliveryError",
    "NotificationOutcome",
    "NotificationService",
    "NotificationSummary",
]
