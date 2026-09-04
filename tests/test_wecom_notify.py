from datetime import UTC, datetime

from stock_daily_report.notify.base import NotificationSummary
from stock_daily_report.notify.wecom import WeComNotifier


def _summary(*, focus_items=("600519 贵州茅台：偏强",)) -> NotificationSummary:
    return NotificationSummary(
        report_date="2026-09-04",
        generated_at=datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
        report_url="https://reports.example/2026-09-04/",
        stock_count=2,
        decision_counts={"偏强": 1, "观察": 1},
        focus_items=focus_items,
        high_risks=("000001 平安银行：风险升高",),
    )


def test_wecom_summary_contains_report_link_and_no_full_report():
    payload = WeComNotifier("https://example.invalid").build_payload(_summary())

    content = payload["markdown"]["content"]
    assert _summary().report_url in content
    assert "600519 贵州茅台：偏强" in content
    assert len(content) < 4096
    assert "report.json" not in content


def test_wecom_summary_limits_focus_items():
    summary = _summary(focus_items=tuple(f"item-{index}" for index in range(8)))

    content = WeComNotifier("https://example.invalid").build_payload(summary)[
        "markdown"
    ]["content"]

    assert "item-0" in content
    assert "item-4" in content
    assert "item-5" not in content


def test_wecom_truncates_oversized_summary_without_dropping_report_link():
    summary = _summary(
        focus_items=("x" * 10_000,),
    )

    content = WeComNotifier("https://example.invalid").build_payload(summary)[
        "markdown"
    ]["content"]

    assert len(content) <= 4096
    assert summary.report_url in content
