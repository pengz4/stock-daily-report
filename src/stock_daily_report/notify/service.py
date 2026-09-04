"""Environment-backed notification orchestration."""

from __future__ import annotations

import os
from collections.abc import Mapping

from stock_daily_report.models import NotificationSettings
from stock_daily_report.notify.base import (
    NotificationDeliveryError,
    NotificationOutcome,
    NotificationSummary,
    WebhookNotifier,
)
from stock_daily_report.notify.feishu import FeishuNotifier
from stock_daily_report.notify.wecom import WeComNotifier
from stock_daily_report.report.models import ReportDocument

_ENVIRONMENT_URLS = {"wecom": "WECOM_WEBHOOK_URL", "feishu": "FEISHU_WEBHOOK_URL"}


class NotificationService:
    """Attempt every enabled channel and report all failures together."""

    def __init__(
        self,
        settings: NotificationSettings,
        *,
        notifiers: Mapping[str, WebhookNotifier] | None = None,
    ) -> None:
        self.settings = settings
        self.notifiers = dict(notifiers or {})

    def send(self, summary: NotificationSummary) -> tuple[NotificationOutcome, ...]:
        outcomes: list[NotificationOutcome] = []
        for channel in sorted(self.settings.enabled_channels):
            try:
                notifier = self.notifiers.get(channel) or self._create_notifier(channel)
                notifier.send(summary)
            except (OSError, RuntimeError, TypeError, ValueError) as error:
                outcomes.append(
                    NotificationOutcome(
                        channel=channel,
                        status="failed",
                        error=_safe_error_message(error),
                    )
                )
            else:
                outcomes.append(NotificationOutcome(channel=channel, status="delivered"))
        result = tuple(outcomes)
        if any(outcome.status == "failed" for outcome in result):
            raise NotificationDeliveryError(result)
        return result

    def send_report(
        self, report: ReportDocument, *, report_url: str
    ) -> tuple[NotificationOutcome, ...]:
        """Build and deliver a summary only after report publication."""

        return self.send(
            NotificationSummary(
                report_date=report.metadata.report_date.isoformat(),
                generated_at=report.metadata.generated_at,
                report_url=report_url,
                stock_count=report.metadata.stock_count,
                decision_counts=_decision_counts(report),
                focus_items=tuple(
                    f"{stock.code} {stock.name}: {stock.decision_label}"
                    for stock in report.stocks
                    if stock.decision_label in {"偏强", "风险升高"}
                ),
                high_risks=tuple(
                    f"{stock.code} {stock.name}: {risk}"
                    for stock in report.stocks
                    for risk in stock.risks
                ),
            )
        )

    def _create_notifier(self, channel: str) -> WebhookNotifier:
        environment_name = _ENVIRONMENT_URLS[channel]
        webhook_url = os.environ.get(environment_name, "").strip()
        if not webhook_url:
            raise ValueError(f"{environment_name} is required for {channel}")
        notifier_type = WeComNotifier if channel == "wecom" else FeishuNotifier
        return notifier_type(
            webhook_url,
            timeout_seconds=self.settings.timeout_seconds,
            max_attempts=self.settings.max_attempts,
        )


def _decision_counts(report: ReportDocument) -> dict[str, int]:
    counts: dict[str, int] = {}
    for stock in report.stocks:
        counts[stock.decision_label] = counts.get(stock.decision_label, 0) + 1
    return counts


def _safe_error_message(error: Exception) -> str:
    message = str(error)
    for environment_name in _ENVIRONMENT_URLS.values():
        secret = os.environ.get(environment_name, "")
        if secret:
            message = message.replace(secret, "<redacted>")
    return message[:300]


__all__ = ["NotificationService"]
