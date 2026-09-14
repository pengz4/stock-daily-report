import base64
import hashlib
import hmac
import json
from datetime import UTC, datetime

from stock_daily_report.notify.base import NotificationSummary
from stock_daily_report.notify.feishu import FeishuNotifier


def _summary() -> NotificationSummary:
    return NotificationSummary(
        report_date="2026-09-04",
        generated_at=datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
        data_timestamp=datetime(2026, 9, 4, 8, tzinfo=UTC),
        report_url="https://reports.example/2026-09-04/",
        stock_count=1,
        decision_counts={"观察": 1},
        focus_items=(),
        high_risks=(),
    )


def test_feishu_signature_verification_stamps_timestamp_and_valid_sign(monkeypatch):
    secret = "example-secret"
    monkeypatch.setattr("stock_daily_report.notify.feishu.time.time", lambda: 1_700_000_000)

    payload = FeishuNotifier("https://example.invalid", secret=secret).build_payload(
        _summary()
    )

    assert payload["timestamp"] == "1700000000"
    expected = base64.b64encode(
        hmac.new(
            f"{payload['timestamp']}\n{secret}".encode(),
            digestmod=hashlib.sha256,
        ).digest()
    ).decode("utf-8")
    assert payload["sign"] == expected


def test_feishu_without_secret_omits_signature_fields():
    payload = FeishuNotifier("https://example.invalid").build_payload(_summary())

    assert "timestamp" not in payload
    assert "sign" not in payload


def test_feishu_summary_contains_link_counts_and_focus_without_full_report():
    summary = NotificationSummary(
        report_date="2026-09-04",
        generated_at=datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
        data_timestamp=datetime(2026, 9, 4, 8, tzinfo=UTC),
        report_url="https://reports.example/2026-09-04/",
        stock_count=2,
        decision_counts={"偏强": 1, "观察": 1},
        focus_items=("600519 贵州茅台：偏强",),
        high_risks=("000001 平安银行：风险升高",),
    )

    payload = FeishuNotifier("https://example.invalid").build_payload(summary)
    post = payload["content"]["post"]["zh_cn"]
    blocks = post["content"]

    assert payload["msg_type"] == "post"
    assert post["title"] == "A股日报 2026-09-04"
    assert any(block[0]["text"].find("数据时间：2026-09-04T08:00:00+00:00") >= 0 for block in blocks)
    assert any(block[0]["text"].find("偏强: 1") >= 0 for block in blocks)
    assert any(block[0]["text"].find("风险升高") >= 0 for block in blocks)
    assert any(
        block[0]["tag"] == "a" and block[0]["href"] == summary.report_url
        for block in blocks
    )
    assert "report.json" not in str(payload)


def test_feishu_splits_large_risk_summary_into_valid_payloads():
    summary = NotificationSummary(
        report_date="2026-09-04",
        generated_at=datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
        data_timestamp=datetime(2026, 9, 4, 8, tzinfo=UTC),
        report_url="https://reports.example/2026-09-04/",
        stock_count=100,
        decision_counts={"风险升高": 100},
        focus_items=(),
        high_risks=tuple(f"risk-{index}-" + "风险" * 500 for index in range(100)),
    )

    payloads = FeishuNotifier("https://example.invalid").build_payloads(summary)

    assert len(payloads) > 1
    assert all(
        len(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
        )
        <= 20_000
        for payload in payloads
    )
    rendered = str(payloads)
    assert all(f"risk-{index}-" in rendered for index in range(100))
