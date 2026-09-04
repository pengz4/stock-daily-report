from datetime import UTC, datetime

import pytest

from stock_daily_report.models import NotificationSettings
from stock_daily_report.notify.base import (
    NotificationDeliveryError,
    NotificationSummary,
    WebhookError,
)
from stock_daily_report.notify.service import NotificationService
from stock_daily_report.notify.wecom import WeComNotifier


def _summary() -> NotificationSummary:
    return NotificationSummary(
        report_date="2026-09-04",
        generated_at=datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
        report_url="https://reports.example/2026-09-04/",
        stock_count=1,
        decision_counts={"观察": 1},
        focus_items=(),
        high_risks=(),
    )


class RecordingNotifier:
    def __init__(self, *, error: Exception | None = None):
        self.error = error
        self.calls = []

    def send(self, summary):
        self.calls.append(summary)
        if self.error:
            raise self.error


def test_service_delivers_enabled_channels_independently(monkeypatch):
    wecom = RecordingNotifier()
    feishu = RecordingNotifier()
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://wecom.example/hook")
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://feishu.example/hook")
    service = NotificationService(
        NotificationSettings(enabled_channels={"wecom", "feishu"}),
        notifiers={"wecom": wecom, "feishu": feishu},
    )

    outcomes = service.send(_summary())

    assert [outcome.channel for outcome in outcomes] == ["feishu", "wecom"]
    assert len(wecom.calls) == len(feishu.calls) == 1


def test_service_records_channel_failure_and_raises_after_attempting_all(monkeypatch):
    wecom = RecordingNotifier(error=RuntimeError("temporary failure"))
    feishu = RecordingNotifier()
    monkeypatch.setenv("WECOM_WEBHOOK_URL", "https://wecom.example/hook")
    monkeypatch.setenv("FEISHU_WEBHOOK_URL", "https://feishu.example/hook")
    service = NotificationService(
        NotificationSettings(enabled_channels={"wecom", "feishu"}),
        notifiers={"wecom": wecom, "feishu": feishu},
    )

    with pytest.raises(NotificationDeliveryError) as raised:
        service.send(_summary())

    assert "wecom" in str(raised.value)
    assert len(feishu.calls) == 1
    assert next(
        outcome for outcome in raised.value.outcomes if outcome.channel == "wecom"
    ).status == "failed"


def test_service_requires_webhook_environment_for_enabled_channel(monkeypatch):
    monkeypatch.delenv("WECOM_WEBHOOK_URL", raising=False)
    service = NotificationService(NotificationSettings(enabled_channels={"wecom"}))

    with pytest.raises(NotificationDeliveryError, match="WECOM_WEBHOOK_URL"):
        service.send(_summary())


def test_webhook_retries_transient_http_failure_with_bounded_backoff():
    responses = [(503, b""), (200, b'{"errcode": 0}')]
    sleeps = []

    def transport(url, body, timeout):
        return responses.pop(0)

    WeComNotifier(
        "https://wecom.example/hook",
        transport=transport,
        sleep=sleeps.append,
    ).send(_summary())

    assert sleeps == [0.5]


def test_webhook_does_not_retry_non_transient_api_failure():
    attempts = []

    def transport(url, body, timeout):
        attempts.append(1)
        return 200, b'{"errcode": 40058}'

    with pytest.raises(WebhookError, match="errcode=40058"):
        WeComNotifier(
            "https://wecom.example/hook",
            transport=transport,
            sleep=lambda _: pytest.fail("non-transient failure was retried"),
        ).send(_summary())

    assert len(attempts) == 1


def test_service_redacts_webhook_url_from_recorded_failure(monkeypatch):
    secret_url = "https://wecom.example/secret-token"
    monkeypatch.setenv("WECOM_WEBHOOK_URL", secret_url)
    service = NotificationService(
        NotificationSettings(enabled_channels={"wecom"}),
        notifiers={"wecom": RecordingNotifier(error=RuntimeError(secret_url))},
    )

    with pytest.raises(NotificationDeliveryError) as raised:
        service.send(_summary())

    assert secret_url not in str(raised.value)
    assert "<redacted>" in str(raised.value)
