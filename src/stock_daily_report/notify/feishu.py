"""Feishu custom bot notification adapter."""

from __future__ import annotations

import json

from stock_daily_report.notify.base import (
    NotificationSummary,
    WebhookNotifier,
    _shorten,
)


class FeishuNotifier(WebhookNotifier):
    """Send a compact rich-text summary through a Feishu bot webhook."""

    channel = "feishu"

    def build_payload(self, summary: NotificationSummary) -> dict[str, object]:
        return self.build_payloads(summary)[0]

    def build_payloads(
        self, summary: NotificationSummary
    ) -> tuple[dict[str, object], ...]:
        counts = "、".join(
            f"{_shorten(label, 40)}: {count}"
            for label, count in sorted(summary.decision_counts.items())
        )
        lines = [
            [{"tag": "text", "text": f"数据时间：{summary.data_timestamp.isoformat()}"}],
            [{"tag": "text", "text": f"自选股数量：{summary.stock_count}"}],
            [{"tag": "text", "text": f"决策分布：{counts}"}],
            [{"tag": "text", "text": "重点观察"}],
        ]
        lines.extend(
            [{"tag": "text", "text": f"• {_shorten(item, 120)}"}]
            for item in summary.focus_items[:5]
        )
        lines.append([{"tag": "text", "text": "高风险"}])
        risk_lines = [
            [{"tag": "text", "text": f"• {_shorten(item, 120)}"}]
            for item in summary.high_risks
        ] or [[{"tag": "text", "text": "• 无"}]]
        link = [
            [{"tag": "a", "text": "查看完整日报", "href": summary.report_url}]
        ]
        chunks: list[dict[str, object]] = []
        current_risks: list[list[dict[str, str]]] = []
        for risk_line in risk_lines:
            candidate = _payload(
                summary.report_date, [*lines, *current_risks, risk_line, *link]
            )
            if current_risks and _payload_size(candidate) > _FEISHU_LIMIT:
                chunks.append(
                    _payload(summary.report_date, [*lines, *current_risks, *link])
                )
                current_risks = []
            current_risks.append(risk_line)
        chunks.append(
            _payload(summary.report_date, [*lines, *current_risks, *link])
        )
        if any(_payload_size(chunk) > _FEISHU_LIMIT for chunk in chunks):
            raise ValueError("notification summary exceeds Feishu message limit")
        return tuple(chunks)


_FEISHU_LIMIT = 20_000


def _payload(
    report_date: str, content: list[list[dict[str, str]]]
) -> dict[str, object]:
    return {
        "msg_type": "post",
        "content": {
            "post": {
                "zh_cn": {
                    "title": f"A股日报 {report_date}",
                    "content": content,
                }
            },
        },
    }


def _payload_size(payload: dict[str, object]) -> int:
    return len(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    )


__all__ = ["FeishuNotifier"]
