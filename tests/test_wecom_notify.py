from datetime import UTC, datetime

import pytest

from stock_daily_report.notify.base import NotificationSummary
from stock_daily_report.notify.wecom import WeComNotifier


def _summary(*, focus_items=("600519 贵州茅台：偏强",)) -> NotificationSummary:
    return NotificationSummary(
        report_date="2026-09-04",
        generated_at=datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
        data_timestamp=datetime(2026, 9, 4, 8, tzinfo=UTC),
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
    assert "数据时间：2026-09-04T08:00:00+00:00" in content
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


def test_wecom_uses_utf8_byte_limit_and_splits_all_high_risks():
    summary = _summary(
        focus_items=(),
    )
    summary = NotificationSummary(
        **{
            **summary.__dict__,
            "high_risks": tuple(f"风险-{index}-" + "风险" * 220 for index in range(15)),
        }
    )
    notifier = WeComNotifier("https://example.invalid")

    payloads = notifier.build_payloads(summary)

    assert len(payloads) > 1
    contents = [
        payload["markdown"]["content"]
        for payload in payloads
    ]
    assert all(len(content.encode("utf-8")) <= 4096 for content in contents)
    assert all(f"风险-{index}-" in "\n".join(contents) for index in range(15))


def test_wecom_rejects_a_fixed_section_that_cannot_fit():
    summary = _summary()
    summary = NotificationSummary(
        **{
            **summary.__dict__,
            "report_url": "https://e/" + "a" * 3545,
            "high_risks": ("风险" * 1000, "x"),
        }
    )

    with pytest.raises(ValueError, match="exceeds"):
        WeComNotifier("https://example.invalid").build_payloads(summary)
