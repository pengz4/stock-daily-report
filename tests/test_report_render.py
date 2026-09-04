from datetime import UTC, date, datetime

from stock_daily_report.report.models import (
    AnalyzerMetadata,
    MarketSummary,
    ReportDocument,
    ReportMetadata,
    ReportMetrics,
    StockReport,
    StructureSummary,
)
from stock_daily_report.report.render import render_html, render_markdown


def _document(name: str = "<script>alert(1)</script>") -> ReportDocument:
    return ReportDocument(
        metadata=ReportMetadata(
            report_date=date(2026, 9, 4),
            generated_at=datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
            latest_source_timestamp=datetime(2026, 9, 4, 8, tzinfo=UTC),
            snapshot_path="snapshots/2026-09-04/input.json",
            snapshot_hash="a" * 64,
            provider_names=("fixture",),
            config_hash="b" * 64,
            analyzer_versions=AnalyzerMetadata(structural="simplified-v1"),
            quality_status="passed",
            stock_count=1,
        ),
        market_summary=MarketSummary(
            status="unavailable",
            text="Broad market data unavailable; watchlist-only summary.",
        ),
        stocks=(
            StockReport(
                code="600519",
                name=name,
                group="consumer",
                provider_name="fixture",
                latest_trade_date=date(2026, 9, 4),
                latest_source_timestamp=datetime(2026, 9, 4, 8, tzinfo=UTC),
                bar_count=80,
                quality_status="passed",
                quality_issues=(),
                metrics=ReportMetrics(close=100.0),
                structure=StructureSummary(
                    state_label="neutral_consolidation",
                    status="confirmed",
                    rule_version="simplified-v1",
                    levels=(),
                    observations=(),
                ),
                decision_label="观察",
                evidence=("safe [evidence]",),
                risks=("risk",),
                key_levels=("support 100",),
                next_conditions=("next * condition",),
            ),
        ),
    )


def test_renderers_escape_untrusted_markdown_and_html_text():
    document = _document()

    markdown = render_markdown(document)
    html = render_html(document)

    assert r"\<script\>alert\(1\)\</script\>" in markdown
    assert r"safe \[evidence\]" in markdown
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_renderers_do_not_include_notification_secrets():
    document = _document(name="visible")
    markdown = render_markdown(document)
    html = render_html(document)

    assert "https://secret.example/webhook" not in markdown
    assert "https://secret.example/webhook" not in html
