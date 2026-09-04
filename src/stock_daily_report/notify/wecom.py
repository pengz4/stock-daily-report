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
        return self.build_payloads(summary)[0]

    def build_payloads(
        self, summary: NotificationSummary
    ) -> tuple[dict[str, object], ...]:
        lines = [
            f"### A股日报 {summary.report_date}",
            f"> 数据时间：{summary.data_timestamp.isoformat()}",
            f"> 自选股数量：{summary.stock_count}",
            "> 决策分布："
            + "、".join(
                f"{_shorten(label, 40)}: {count}"
                for label, count in sorted(summary.decision_counts.items())
            ),
            "",
            "**重点观察**",
        ]
        lines.extend(f"- {_shorten(item, 120)}" for item in summary.focus_items[:5])
        lines.append("**高风险**")
        risk_lines = [f"- {_shorten(item, 120)}" for item in summary.high_risks]
        suffix = f"[查看完整日报]({summary.report_url})"
        chunks: list[dict[str, object]] = []
        current_risks: list[str] = []
        for risk_line in risk_lines or ["- 无"]:
            candidate = "\n".join([*lines, *current_risks, risk_line, suffix])
            if (
                current_risks
                and len(candidate.encode("utf-8")) > _WECOM_MARKDOWN_LIMIT
            ):
                chunks.append(_payload("\n".join([*lines, *current_risks, suffix])))
                current_risks = []
            current_risks.append(risk_line)
        content = "\n".join([*lines, *current_risks, suffix])
        if len(content.encode("utf-8")) > _WECOM_MARKDOWN_LIMIT:
            raise ValueError("notification summary exceeds WeCom message limit")
        chunks.append(_payload(content))
        return tuple(chunks)


def _payload(content: str) -> dict[str, object]:
    return {"msgtype": "markdown", "markdown": {"content": content}}


__all__ = ["WeComNotifier"]
