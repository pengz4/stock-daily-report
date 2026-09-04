"""Feishu custom bot notification adapter."""

from __future__ import annotations

from stock_daily_report.notify.base import (
    NotificationSummary,
    WebhookNotifier,
    _shorten,
)


class FeishuNotifier(WebhookNotifier):
    """Send a compact rich-text summary through a Feishu bot webhook."""

    channel = "feishu"

    def build_payload(self, summary: NotificationSummary) -> dict[str, object]:
        counts = "、".join(
            f"{_shorten(label, 40)}: {count}"
            for label, count in sorted(summary.decision_counts.items())
        )
        lines = [
            [{"tag": "text", "text": f"生成时间：{summary.generated_at.isoformat()}"}],
            [{"tag": "text", "text": f"自选股数量：{summary.stock_count}"}],
            [{"tag": "text", "text": f"决策分布：{counts}"}],
            [{"tag": "text", "text": "重点观察"}],
        ]
        lines.extend(
            [{"tag": "text", "text": f"• {_shorten(item)}"}]
            for item in summary.focus_items[:5]
        )
        lines.append([{"tag": "text", "text": "高风险"}])
        lines.extend(
            [{"tag": "text", "text": f"• {_shorten(item)}"}]
            for item in summary.high_risks
        )
        lines.append(
            [
                {
                    "tag": "a",
                    "text": "查看完整日报",
                    "href": summary.report_url,
                }
            ]
        )
        return {
            "msg_type": "post",
            "content": {
                "post": {
                    "zh_cn": {
                        "title": f"A股日报 {summary.report_date}",
                        "content": lines,
                    }
                }
            },
        }


__all__ = ["FeishuNotifier"]
