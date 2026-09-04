"""Enterprise WeChat robot notification adapter."""

from __future__ import annotations

from stock_daily_report.notify.base import (
    NotificationSummary,
    WebhookNotifier,
    _shorten,
)

_WECOM_MARKDOWN_LIMIT = 4096


class WeComNotifier(WebhookNotifier):
    """Send a compact Markdown summary through a WeCom robot webhook."""

    channel = "wecom"

    def build_payload(self, summary: NotificationSummary) -> dict[str, object]:
        lines = [
            f"### A股日报 {summary.report_date}",
            f"> 生成时间：{summary.generated_at.isoformat()}",
            f"> 自选股数量：{summary.stock_count}",
            "> 决策分布："
            + "、".join(
                f"{_shorten(label, 40)}: {count}"
                for label, count in sorted(summary.decision_counts.items())
            ),
            "",
            "**重点观察**",
        ]
        lines.extend(f"- {_shorten(item)}" for item in summary.focus_items[:5])
        lines.append("**高风险**")
        lines.extend(f"- {_shorten(item)}" for item in summary.high_risks)
        lines.append(f"[查看完整日报]({summary.report_url})")
        content = "\n".join(lines)
        if len(content) > _WECOM_MARKDOWN_LIMIT:
            content = _truncate_preserving_link(content, summary.report_url)
        return {"msgtype": "markdown", "markdown": {"content": content}}


def _truncate_preserving_link(content: str, report_url: str) -> str:
    suffix = f"\n[查看完整日报]({report_url})"
    available = _WECOM_MARKDOWN_LIMIT - len(suffix) - 1
    if available <= 0:
        raise ValueError("notification report URL exceeds WeCom message limit")
    return content[:available].rstrip() + "…" + suffix


__all__ = ["WeComNotifier"]
