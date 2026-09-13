"""Deterministic Markdown and HTML renderers for validated reports."""

from __future__ import annotations

import html
from collections.abc import Sequence

from stock_daily_report.report.models import (
    MarketConsensusRanking,
    MarketRanking,
    MarketRankingComponents,
    MarketRankings,
    ReportDocument,
    StockReport,
)

_EM_DASH = "—"
_COMPONENT_FIELDS = ("trend", "momentum", "volume", "structure", "risk")
_PRIMARY_METRIC_FIELDS = ("close", "ma20", "ma60")
_ALL_METRIC_FIELDS = (
    "close",
    "ma20",
    "ma60",
    "return20",
    "return60",
    "realized_volatility20",
    "drawdown60",
    "volume_ratio20",
    "recent_high20",
    "recent_low20",
)


def render_markdown(report: ReportDocument) -> str:
    """Render a report with Markdown escaping for untrusted text."""

    metadata = report.metadata
    lines = [
        f"# A-share daily report — {_md(metadata.report_date.isoformat())}",
        "",
        f"- Generated: {_md(metadata.generated_at.isoformat())}",
        f"- Latest source timestamp: {_md(metadata.latest_source_timestamp.isoformat())}",
        f"- Quality: {_md(metadata.quality_status)}",
        f"- Stock count: {metadata.stock_count}",
        f"- Providers: {_md(', '.join(metadata.provider_names))}",
        f"- Snapshot: `{_md(metadata.snapshot_path)}`",
        f"- Snapshot hash: `{metadata.snapshot_hash}`",
        f"- Config hash: `{metadata.config_hash}`",
        f"- Analyzer: {_md(metadata.analyzer_versions.structural)}",
        "",
        "## Market summary",
        "",
        _md(report.market_summary.text),
        "",
    ]
    lines.extend(_render_market_rankings_markdown(report.market_rankings))
    lines.extend(
        [
        "## Watchlist",
        "",
        ]
    )
    for stock in report.stocks:
        lines.extend(_render_stock_markdown(stock))
    return "\n".join(lines) + "\n"


def render_html(report: ReportDocument) -> str:
    """Render a self-contained report page with escaped dynamic values."""

    metadata = report.metadata
    title = _html(f"A-share daily report — {metadata.report_date.isoformat()}")
    header = _render_report_header(report, report.market_rankings)
    market_summary = _render_market_summary_html(report)
    market_rankings = _render_market_rankings_html(report.market_rankings)
    stock_sections = "\n".join(_render_watchlist_card(stock) for stock in report.stocks)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{title}</title>
  <link rel="stylesheet" href="../../site/styles.css">
</head>
<body>
  <main class="report-page">
    <h1>{title}</h1>
    {header}
    {market_summary}
    {market_rankings}
    <section>
      <h2>Watchlist</h2>
      {stock_sections}
    </section>
  </main>
</body>
</html>
"""


def _render_market_rankings_markdown(rankings: MarketRankings) -> list[str]:
    lines = ["## Full-market rankings", ""]
    if rankings.status == "unavailable":
        lines.extend(
            [
                "Full-market rankings unavailable.",
                "",
                f"- Reason: `{rankings.unavailable_reason}`",
            ]
        )
        if _has_market_ranking_metadata(rankings):
            lines.extend(_market_ranking_metadata_markdown(rankings))
        lines.append("")
        return lines

    lines.extend(_market_ranking_metadata_markdown(rankings))
    lines.extend(
        [
            "",
            "### 多策略共识 / Consensus",
            "",
            (
                "Consensus is the exact intersection of rankings built from shared "
                "technical inputs; it is not independently validated predictive "
                "evidence."
            ),
            "",
            "| Code | Name | Trend | Balanced | Components | Evidence | Risks | Date | Provider |",
            "| --- | --- | ---: | ---: | --- | --- | --- | --- | --- |",
        ]
    )
    lines.extend(_render_consensus_markdown(row) for row in rankings.consensus)
    if not rankings.consensus:
        lines.append("| — | No exact intersection | — | — | — | — | — | — | — |")
    lines.extend(["", "### Trend Top 30", ""])
    lines.extend(_ranking_markdown_table(rankings.trend, rankings.consensus))
    lines.extend(["", "### Balanced Top 30", ""])
    lines.extend(_ranking_markdown_table(rankings.balanced, rankings.consensus))
    lines.append("")
    return lines


def _market_ranking_metadata_markdown(rankings: MarketRankings) -> list[str]:
    generated_at = (
        rankings.generated_at.isoformat()
        if rankings.generated_at is not None
        else "—"
    )
    coverage = f"{rankings.coverage:.2%}" if rankings.coverage is not None else "—"
    return [
        f"- Scan date: {_md(_optional_market_metadata(rankings.scan_date))}",
        f"- Generated: {_md(generated_at)}",
        f"- Rule: {_md(_optional_market_metadata(rankings.rule_version))}",
        (
            "- Providers: "
            f"{_md(', '.join(rankings.provider_names) or '—')}"
        ),
        (
            f"- Coverage: {coverage} "
            f"({_optional_market_metadata(rankings.valid_count)}/"
            f"{_optional_market_metadata(rankings.eligible_count)} valid; "
            f"{_optional_market_metadata(rankings.universe_count)} universe)"
        ),
        f"- Exclusions: {_md(_format_reason_counts(rankings.exclusion_counts))}",
        f"- Failures: {_md(_format_reason_counts(rankings.failure_counts))}",
    ]


def _ranking_markdown_table(
    records: Sequence[MarketRanking],
    consensus: Sequence[MarketConsensusRanking],
) -> list[str]:
    consensus_by_code = {record.code: record for record in consensus}
    lines = [
        "| Rank | Code | Name | Score | Consensus | Components | Evidence | Risks | Date | Provider |",
        "| ---: | --- | --- | ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        consensus_record = consensus_by_code.get(record.code)
        consensus_text = (
            "—"
            if consensus_record is None
            else (
                "多策略共识 / consensus — "
                f"Trend rank {consensus_record.trend_rank} / "
                f"{consensus_record.trend_score:.2f}; "
                f"Balanced rank {consensus_record.balanced_rank} / "
                f"{consensus_record.balanced_score:.2f}"
            )
        )
        lines.append(
            "| "
            + " | ".join(
                (
                    str(record.rank),
                    _md(record.code),
                    _md(record.name),
                    f"{record.score:.2f}",
                    _md(consensus_text),
                    _md(_format_components(record.components)),
                    _md(_format_codes(record.evidence_codes)),
                    _md(_format_codes(record.risk_codes)),
                    _md(record.latest_trade_date.isoformat()),
                    _md(record.provider_name),
                )
            )
            + " |"
        )
    return lines


def _render_consensus_markdown(record: MarketConsensusRanking) -> str:
    component_text = (
        f"trend profile: {_format_components(record.trend_components)}; "
        f"balanced profile: {_format_components(record.balanced_components)}"
    )
    return (
        "| "
        + " | ".join(
            (
                _md(record.code),
                _md(record.name),
                f"Trend rank {record.trend_rank} / {record.trend_score:.2f}",
                f"Balanced rank {record.balanced_rank} / {record.balanced_score:.2f}",
                _md(component_text),
                _md(_format_codes(record.evidence_codes)),
                _md(_format_codes(record.risk_codes)),
                _md(record.latest_trade_date.isoformat()),
                _md(record.provider_name),
            )
        )
        + " |"
    )


def _render_market_rankings_html(rankings: MarketRankings) -> str:
    warning = _render_ranking_warning(rankings)
    if rankings.status == "unavailable":
        warning_html = f"\n      {warning}" if warning else ""
        return f"""<section class="market-rankings unavailable">
      <h2>Full-market rankings</h2>{warning_html}
      <p>Full-market rankings unavailable.</p>
      <p><strong>Reason:</strong> <code>{_format_html_optional(rankings.unavailable_reason)}</code></p>
    </section>"""

    consensus_by_code = {record.code: record for record in rankings.consensus}
    warning_html = f"\n      {warning}" if warning else ""
    return f"""<section class="market-rankings">
      <h2>Full-market rankings</h2>{warning_html}
      {_render_consensus_cards(rankings.consensus)}
      {_render_ranking_profile("trend", rankings.trend, consensus_by_code)}
      {_render_ranking_profile("balanced", rankings.balanced, consensus_by_code)}
    </section>"""


def _render_report_header(report: ReportDocument, rankings: MarketRankings) -> str:
    metadata = report.metadata
    return f"""<header class="report-header">
      <p><a href="../../site/index.html">All dated reports</a></p>
      <p><strong>Report date:</strong> {_html(metadata.report_date.strftime("%Y/%m/%d"))}</p>
      <p><strong>Latest trading date:</strong> {_render_latest_trading_text(report, rankings)}</p>
      <p class="quality-badge">Quality: {_html(metadata.quality_status)}</p>
      {_render_runtime_metadata(report, rankings)}
    </header>"""


def _render_market_summary_html(report: ReportDocument) -> str:
    summary_metrics = _render_summary_metrics(report.market_rankings)
    summary_metrics_html = f"\n      {summary_metrics}" if summary_metrics else ""
    return f"""<section class="market-summary">
      <h2>Market summary</h2>
      <p>{_html(report.market_summary.text)}</p>{summary_metrics_html}
    </section>"""


def _render_runtime_metadata(report: ReportDocument, rankings: MarketRankings) -> str:
    metadata = report.metadata
    has_ranking_metadata = _has_market_ranking_metadata(rankings)
    report_fields = "".join(
        (
            _render_html_field("Report generated", metadata.generated_at.isoformat()),
            _render_html_field(
                "Report latest source timestamp",
                metadata.latest_source_timestamp.isoformat(),
            ),
            _render_html_field(
                "Report providers",
                _sequence_text(metadata.provider_names),
            ),
            _render_html_field("Stock count", metadata.stock_count),
            _render_html_field("Snapshot path", metadata.snapshot_path),
            _render_html_field("Snapshot hash", metadata.snapshot_hash),
            _render_html_field("Report config hash", metadata.config_hash),
            _render_html_field(
                "Analyzer",
                metadata.analyzer_versions.structural,
            ),
            (
                ""
                if has_ranking_metadata
                else _render_html_field("Ranking status", rankings.status)
            ),
            (
                ""
                if has_ranking_metadata
                else _render_html_field("Ranking reason", rankings.unavailable_reason)
            ),
        )
    )
    ranking_metadata_section = ""
    if has_ranking_metadata:
        ranking_fields = "".join(
            (
                _render_html_field("Ranking status", rankings.status),
                _render_html_field("Ranking reason", rankings.unavailable_reason),
                _render_html_field(
                    "Ranking scan date",
                    rankings.scan_date.isoformat()
                    if rankings.scan_date is not None
                    else None,
                ),
                _render_html_field(
                    "Ranking generated",
                    rankings.generated_at.isoformat()
                    if rankings.generated_at is not None
                    else None,
                ),
                _render_html_field("Ranking rule version", rankings.rule_version),
                _render_html_field(
                    "Ranking providers",
                    _sequence_text(rankings.provider_names),
                ),
                _render_html_field("Ranking config hash", rankings.config_hash),
                _render_html_field("Ranking input hash", rankings.input_hash),
                _render_html_field("Universe count", rankings.universe_count),
                _render_html_field("Eligible count", rankings.eligible_count),
                _render_html_field("Valid count", rankings.valid_count),
                _render_html_field(
                    "Coverage",
                    f"{rankings.coverage:.2%}"
                    if rankings.coverage is not None
                    else None,
                ),
                _render_html_field(
                    "Exclusions",
                    _format_reason_counts(rankings.exclusion_counts),
                ),
                _render_html_field(
                    "Failures",
                    _format_reason_counts(rankings.failure_counts),
                ),
            )
        )
        ranking_metadata_section = f"""
      <div class="runtime-metadata__section">
        <h3>Ranking metadata</h3>
        {ranking_fields}
      </div>"""
    body = f"""<div class="runtime-metadata__section">
        <h3>Report metadata</h3>
        {report_fields}
      </div>{ranking_metadata_section}"""
    return _render_html_details("Runtime metadata", body, class_name="runtime-metadata")


def _render_summary_metrics(rankings: MarketRankings) -> str:
    cards: list[str] = []
    if rankings.coverage is not None:
        cards.append(
            _render_metric_card(
                "coverage",
                "Coverage",
                f"{rankings.coverage:.2%}",
            )
        )
    if rankings.valid_count is not None and rankings.eligible_count is not None:
        cards.append(
            _render_metric_card(
                "valid",
                "Valid / eligible",
                f"{rankings.valid_count} / {rankings.eligible_count}",
            )
        )
    if rankings.universe_count is not None:
        cards.append(
            _render_metric_card(
                "universe",
                "Universe",
                str(rankings.universe_count),
            )
        )
    if rankings.status == "available":
        cards.append(
            _render_metric_card(
                "consensus",
                "Consensus count",
                str(len(rankings.consensus)),
            )
        )
    if not cards:
        return ""
    return f"""<div class="summary-metrics">
        {"".join(cards)}
      </div>"""


def _render_metric_card(data_metric: str, label: str, value: str) -> str:
    return f"""<article class="metric-card" data-metric="{_html(data_metric)}">
          <h3>{_html(label)}</h3>
          <p>{_html(value)}</p>
        </article>"""


def _render_latest_trading_text(
    report: ReportDocument,
    rankings: MarketRankings,
) -> str:
    watchlist_latest = _watchlist_latest_trade_date(report.stocks)
    if rankings.status == "available" and rankings.scan_date is not None:
        scan_date = rankings.scan_date.isoformat()
        if watchlist_latest is not None and watchlist_latest != rankings.scan_date:
            watchlist_text = (
                watchlist_latest.isoformat() if watchlist_latest is not None else None
            )
            return (
                f"scan date {_html(scan_date)} · "
                f"watchlist latest {_format_html_optional(watchlist_text)}"
            )
        return f"scan date {_html(scan_date)}"
    if watchlist_latest is None:
        return _EM_DASH
    return f"watchlist latest {_html(watchlist_latest.isoformat())}"


def _render_ranking_warning(rankings: MarketRankings) -> str:
    if rankings.status == "unavailable":
        return (
            '<p class="ranking-warning">Full-market rankings unavailable: '
            f"{_format_html_optional(rankings.unavailable_reason)}</p>"
        )
    if rankings.failure_counts:
        return (
            '<p class="ranking-warning">Ranking audit warning: failures reported — '
            f"{_html(_format_reason_counts(rankings.failure_counts))}</p>"
        )
    return ""


def _render_consensus_cards(
    consensus: Sequence[MarketConsensusRanking],
) -> str:
    note = (
        '<p class="consensus-note">Consensus is the exact intersection of '
        "rankings built from shared technical inputs; it is not independently "
        "validated predictive evidence.</p>"
    )
    if not consensus:
        return f"""<section class="consensus-section">
      <h3>多策略共识 / Consensus</h3>
      {note}
      <p class="empty-state">No exact intersection for this scan.</p>
    </section>"""

    cards = "\n".join(
        _render_consensus_card(record)
        for record in sorted(
            consensus,
            key=lambda ranking: (
                ranking.trend_rank,
                ranking.balanced_rank,
                ranking.code,
            ),
        )[:5]
    )
    return f"""<section class="consensus-section">
      <h3>多策略共识 / Consensus</h3>
      {note}
      <div class="consensus-grid">
        {cards}
      </div>
    </section>"""


def _render_consensus_card(record: MarketConsensusRanking) -> str:
    detail_body = "".join(
        (
            _render_html_field("Code", record.code),
            _render_html_field("Name", record.name),
            _render_html_field("Trend rank", record.trend_rank),
            _render_html_field("Trend score", f"{record.trend_score:.2f}"),
            _render_html_field("Balanced rank", record.balanced_rank),
            _render_html_field("Balanced score", f"{record.balanced_score:.2f}"),
            _render_component_group("Trend components", record.trend_components),
            _render_component_group(
                "Balanced components",
                record.balanced_components,
            ),
            _render_html_tag_field(
                "Evidence",
                record.evidence_codes,
                kind="evidence",
            ),
            _render_html_tag_field("Risks", record.risk_codes, kind="risk"),
            _render_html_field(
                "Latest trade date",
                record.latest_trade_date.isoformat(),
            ),
            _render_html_field("Provider", record.provider_name),
        )
    )
    return f"""<article class="consensus-card">
      <div class="consensus-row">
        <h4>{_html(record.code)} — {_html(record.name)}</h4>
        <p class="consensus-trend">Trend rank {record.trend_rank} / {_format_html_float(record.trend_score)}</p>
        <p class="consensus-balanced">Balanced rank {record.balanced_rank} / {_format_html_float(record.balanced_score)}</p>
        <p>Trend components: {_html(_format_components(record.trend_components))}</p>
        <p>Balanced components: {_html(_format_components(record.balanced_components))}</p>
        <p>Evidence: {_render_html_tags(record.evidence_codes, kind="evidence")}</p>
        <p>Risks: {_render_html_tags(record.risk_codes, kind="risk")}</p>
        <p>Date: {_html(record.latest_trade_date.isoformat())}</p>
        <p>Provider: {_html(record.provider_name)}</p>
      </div>
      {_render_html_details("Audit details", detail_body, class_name="consensus-detail")}
    </article>"""


def _render_ranking_profile(
    profile: str,
    records: Sequence[MarketRanking],
    consensus_by_code: dict[str, MarketConsensusRanking],
) -> str:
    profile_title = "Trend Top 10" if profile == "trend" else "Balanced Top 10"
    profile_scope = "Trend Top 30" if profile == "trend" else "Balanced Top 30"
    if not records:
        content = '<p class="empty-state">No ranking records available.</p>'
    else:
        visible_rows = "\n".join(
            _render_ranking_row(record, consensus_by_code.get(record.code))
            for record in records
            if record.rank <= 10
        )
        full_rows = "\n".join(
            _render_ranking_row(record, consensus_by_code.get(record.code))
            for record in records
            if 10 < record.rank <= 30
        )
        full_ranking = (
            _render_html_details(
                "View full ranking (11–30)",
                full_rows,
                class_name="full-ranking",
            )
            if full_rows
            else ""
        )
        content = f"""<div class="ranking-visible">
          {visible_rows}
        </div>
        {full_ranking}"""
    return f"""<section class="ranking-profile" data-profile="{_html(profile)}" data-scope="{_html(profile_scope)}">
      <h3>{profile_title}</h3>
      {content}
    </section>"""


def _render_ranking_row(
    record: MarketRanking,
    consensus: MarketConsensusRanking | None,
) -> str:
    consensus_marker = (
        "Consensus"
        if consensus is not None
        else "No consensus"
    )
    short_risk = record.risk_codes[0] if record.risk_codes else "None"
    return f"""<div class="ranking-row">
      <div class="ranking-row__summary">
        <span class="ranking-rank">#{record.rank}</span>
        <span class="ranking-code">{_html(record.code)}</span>
        <span class="ranking-name">{_html(record.name)}</span>
        <span class="ranking-score">{_format_html_float(record.score)}</span>
        <span class="ranking-consensus">{_html(consensus_marker)}</span>
        <span class="ranking-risk">{_html(short_risk)}</span>
      </div>
      {_render_ranking_detail(record, consensus)}
    </div>"""


def _render_ranking_detail(
    record: MarketRanking,
    consensus: MarketConsensusRanking | None,
) -> str:
    fields = [
        _render_html_field("Rank", record.rank),
        _render_html_field("Code", record.code),
        _render_html_field("Name", record.name),
        _render_html_field("Score", f"{record.score:.2f}"),
    ]
    if consensus is not None:
        fields.extend(
            (
                _render_html_field(
                    f"Trend rank {consensus.trend_rank}",
                    f"{consensus.trend_score:.2f}",
                ),
                _render_html_field(
                    f"Balanced rank {consensus.balanced_rank}",
                    f"{consensus.balanced_score:.2f}",
                ),
            )
        )
    fields.extend(
        _render_html_field(
            component_name,
            f"{getattr(record.components, component_name):.2f}",
        )
        for component_name in _COMPONENT_FIELDS
    )
    fields.extend(
        (
            _render_html_tag_field("Evidence", record.evidence_codes, kind="evidence"),
            _render_html_tag_field("Risks", record.risk_codes, kind="risk"),
            _render_html_field(
                "Latest trade date",
                record.latest_trade_date.isoformat(),
            ),
            _render_html_field("Provider", record.provider_name),
        )
    )
    return _render_html_details(
        "Details",
        "".join(fields),
        class_name="ranking-detail",
    )


def _render_component_group(
    title: str,
    components: MarketRankingComponents,
) -> str:
    fields = "".join(
        _render_html_field(
            component_name,
            f"{getattr(components, component_name):.2f}",
        )
        for component_name in _COMPONENT_FIELDS
    )
    return f"""<section class="component-group">
      <h5>{_html(title)}</h5>
      {fields}
    </section>"""


def _format_components(components: MarketRankingComponents) -> str:
    return (
        f"trend={components.trend:.2f}, momentum={components.momentum:.2f}, "
        f"volume={components.volume:.2f}, structure={components.structure:.2f}, "
        f"risk={components.risk:.2f}"
    )


def _format_codes(codes: Sequence[str]) -> str:
    return ", ".join(codes) if codes else "None"


def _format_reason_counts(counts: Sequence[object]) -> str:
    if not counts:
        return "None"
    return ", ".join(f"{item.code}={item.count}" for item in counts)


def _has_market_ranking_metadata(rankings: MarketRankings) -> bool:
    return any(
        value is not None
        for value in (
            rankings.scan_date,
            rankings.generated_at,
            rankings.rule_version,
            rankings.config_hash,
            rankings.input_hash,
            rankings.universe_count,
            rankings.eligible_count,
            rankings.valid_count,
            rankings.coverage,
        )
    ) or bool(
        rankings.provider_names
        or rankings.exclusion_counts
        or rankings.failure_counts
    )


def _optional_market_metadata(value: object | None) -> object:
    return _EM_DASH if value is None else value


def _format_html_optional(value: object | None) -> str:
    return _EM_DASH if value is None else _html(value)


def _format_html_float(value: float | None, digits: int = 2) -> str:
    if value is None:
        return _EM_DASH
    return _html(f"{value:.{digits}f}")


def _render_html_tags(values: Sequence[str], *, kind: str) -> str:
    tag_class = "tag"
    if kind == "evidence":
        tag_class = "tag tag--evidence"
    elif kind == "risk":
        tag_class = "tag tag--risk"
    if not values:
        return f'<span class="{tag_class}">None</span>'
    return " ".join(
        f'<span class="{tag_class}">{_html(value)}</span>'
        for value in values
    )


def _render_html_field(
    label: str,
    value: object | None,
    *,
    class_name: str = "detail-field",
) -> str:
    return (
        f'<div class="{_html(class_name)}">'
        f'<span class="{_html(class_name)}__label">{_html(label)}</span>: '
        f'<span class="{_html(class_name)}__value">{_format_html_optional(value)}</span>'
        "</div>\n"
    )


def _render_html_details(summary: str, body: str, *, class_name: str) -> str:
    return (
        f'<details class="{_html(class_name)}">'
        f"<summary>{_html(summary)}</summary>"
        f"{body}"
        "</details>"
    )


def _render_html_tag_field(label: str, values: Sequence[str], *, kind: str) -> str:
    return (
        '<div class="detail-field">'
        f'<span class="detail-field__label">{_html(label)}</span>: '
        f'<span class="detail-field__value">{_render_html_tags(values, kind=kind)}</span>'
        "</div>\n"
    )


def _render_html_list(values: Sequence[object]) -> str:
    items = values or ("None",)
    return "<ul>" + "".join(f"<li>{_html(value)}</li>" for value in items) + "</ul>"


def _render_html_list_field(label: str, values: Sequence[object]) -> str:
    return (
        '<div class="detail-field">'
        f'<span class="detail-field__label">{_html(label)}</span>: '
        f"{_render_html_list(values)}"
        "</div>\n"
    )


def _sequence_text(values: Sequence[object]) -> str:
    return ", ".join(str(value) for value in values) if values else "None"


def _watchlist_latest_trade_date(stocks: Sequence[StockReport]) -> object | None:
    if not stocks:
        return None
    return max(stock.latest_trade_date for stock in stocks)


def render_site_index(report_dates: Sequence[str]) -> str:
    """Render a safe static index linking to every dated report page."""

    links = "\n".join(
        f'      <li><a href="../reports/{_html(report_date)}/index.html">'
        f"{_html(report_date)}</a></li>"
        for report_date in sorted(set(report_dates), reverse=True)
    )
    if not links:
        links = "      <li>No reports published yet.</li>"
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>A-share daily reports</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <main>
    <h1>A-share daily reports</h1>
    <p>Static, validated daily report artifacts.</p>
    <ul>
{links}
    </ul>
  </main>
</body>
</html>
"""


def _render_stock_markdown(stock: StockReport) -> list[str]:
    lines = [
        f"### {_md(stock.code)} — {_md(stock.name)}",
        "",
        f"- Group: {_md(stock.group)}",
        f"- Provider: {_md(stock.provider_name)}",
        f"- Latest trade date: {_md(stock.latest_trade_date.isoformat())}",
        f"- Source timestamp: {_md(stock.latest_source_timestamp.isoformat())}",
        f"- Bars: {stock.bar_count}",
        f"- Quality: {_md(stock.quality_status)}",
        f"- Decision: **{_md(stock.decision_label)}**",
        f"- Structure: {_md(stock.structure.state_label)} ({_md(stock.structure.status)})",
        "",
        "Evidence:",
    ]
    lines.extend(f"- {_md(item)}" for item in stock.evidence or ("None",))
    lines.append("Risks:")
    lines.extend(f"- {_md(item)}" for item in stock.risks or ("None",))
    lines.append("Key levels:")
    lines.extend(f"- {_md(item)}" for item in stock.key_levels or ("None",))
    lines.append("Next conditions:")
    lines.extend(f"- {_md(item)}" for item in stock.next_conditions or ("None",))
    lines.append("")
    return lines


def _render_stock_html(stock: StockReport) -> str:
    return _render_watchlist_card(stock)


def _render_watchlist_card(stock: StockReport) -> str:
    primary_metrics = "".join(
        _render_html_field(
            field_name,
            f"{value:.2f}" if value is not None else None,
        )
        for field_name in _PRIMARY_METRIC_FIELDS
        for value in (getattr(stock.metrics, field_name),)
    )
    quality_body = "".join(
        (
            _render_html_field("bar_count", stock.bar_count),
            _render_html_field("Quality status", stock.quality_status),
            _render_html_list_field("Quality issues", stock.quality_issues),
        )
    )
    metrics_body = "".join(
        _render_html_field(
            field_name,
            f"{value:.2f}" if value is not None else None,
        )
        for field_name in _ALL_METRIC_FIELDS
        for value in (getattr(stock.metrics, field_name),)
    )
    structure_levels = [
        f"{level.kind} {level.price:.2f} ({level.source})"
        for level in stock.structure.levels
    ]
    structure_body = "".join(
        (
            _render_html_field("rule_version", stock.structure.rule_version),
            _render_html_list_field("Structure Levels", structure_levels),
            _render_html_list_field(
                "Structure Observations",
                stock.structure.observations,
            ),
        )
    )
    signals_body = "".join(
        (
            _render_html_list_field("Evidence", stock.evidence),
            _render_html_list_field("Risks", stock.risks),
            _render_html_list_field("Key levels", stock.key_levels),
            _render_html_list_field("Next conditions", stock.next_conditions),
        )
    )
    return f"""<article class="stock-card">
      <div class="stock-card__identity">
        <h3>{_html(stock.code)} — {_html(stock.name)}</h3>
        <p><strong>Group:</strong> {_html(stock.group)}</p>
        <p><strong>Decision:</strong> {_html(stock.decision_label)}</p>
        <p><strong>Structure:</strong> {_html(stock.structure.state_label)} ({_html(stock.structure.status)})</p>
        <p><strong>Latest trade date:</strong> {_html(stock.latest_trade_date.isoformat())}</p>
        <p><strong>Latest source timestamp:</strong> {_html(stock.latest_source_timestamp.isoformat())}</p>
        <p><strong>Provider:</strong> {_html(stock.provider_name)}</p>
      </div>
      <div class="stock-card__metrics">
        {primary_metrics}
      </div>
      {_render_html_details("Quality and audit", quality_body, class_name="stock-detail")}
      {_render_html_details("All metrics", metrics_body, class_name="stock-detail")}
      {_render_html_details("Structure details", structure_body, class_name="stock-detail")}
      {_render_html_details("Signals and levels", signals_body, class_name="stock-detail")}
    </article>"""


def _md(value: object) -> str:
    text = str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    for character in r"\`*_{}[]()#+-.!|<>":
        text = text.replace(character, f"\\{character}")
    return text


def _html(value: object) -> str:
    return html.escape(str(value), quote=True)


__all__ = ["render_html", "render_markdown", "render_site_index"]
