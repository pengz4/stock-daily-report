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
    stock_sections = "\n".join(_render_stock_html(stock) for stock in report.stocks)
    market_rankings = _render_market_rankings_html(report.market_rankings)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{_html(f"A-share daily report — {metadata.report_date.isoformat()}")}</title>
  <link rel="stylesheet" href="../../site/styles.css">
</head>
<body>
  <main>
    <p><a href="../../site/index.html">All dated reports</a></p>
    <h1>{_html(f"A-share daily report — {metadata.report_date.isoformat()}")}</h1>
    <dl class="metadata">
      <dt>Generated</dt><dd>{_html(metadata.generated_at.isoformat())}</dd>
      <dt>Latest source timestamp</dt><dd>{_html(metadata.latest_source_timestamp.isoformat())}</dd>
      <dt>Quality</dt><dd>{_html(metadata.quality_status)}</dd>
      <dt>Stock count</dt><dd>{metadata.stock_count}</dd>
      <dt>Providers</dt><dd>{_html(", ".join(metadata.provider_names))}</dd>
      <dt>Snapshot</dt><dd><code>{_html(metadata.snapshot_path)}</code></dd>
      <dt>Snapshot hash</dt><dd><code>{metadata.snapshot_hash}</code></dd>
      <dt>Config hash</dt><dd><code>{metadata.config_hash}</code></dd>
      <dt>Analyzer</dt><dd>{_html(metadata.analyzer_versions.structural)}</dd>
    </dl>
    <section>
      <h2>Market summary</h2>
      <p>{_html(report.market_summary.text)}</p>
    </section>
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
        if rankings.scan_date is not None:
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
    return [
        f"- Scan date: {_md(rankings.scan_date)}",
        f"- Generated: {_md(rankings.generated_at.isoformat())}",
        f"- Rule: {_md(rankings.rule_version)}",
        f"- Providers: {_md(', '.join(rankings.provider_names))}",
        (
            f"- Coverage: {rankings.coverage:.2%} "
            f"({rankings.valid_count}/{rankings.eligible_count} valid; "
            f"{rankings.universe_count} universe)"
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
    if rankings.status == "unavailable":
        metadata = (
            _market_ranking_metadata_html(rankings)
            if rankings.scan_date is not None
            else ""
        )
        return f"""<section class="market-rankings unavailable">
      <h2>Full-market rankings</h2>
      <p>Full-market rankings unavailable.</p>
      <p><strong>Reason:</strong> <code>{_html(rankings.unavailable_reason)}</code></p>
      {metadata}
    </section>"""

    consensus_by_code = {record.code: record for record in rankings.consensus}
    consensus_rows = "\n".join(
        _render_consensus_html(record) for record in rankings.consensus
    )
    if not consensus_rows:
        consensus_rows = (
            '<tr><td colspan="9">No exact intersection for this scan.</td></tr>'
        )
    trend_rows = "\n".join(
        _render_ranking_html(record, consensus_by_code.get(record.code))
        for record in rankings.trend
    )
    balanced_rows = "\n".join(
        _render_ranking_html(record, consensus_by_code.get(record.code))
        for record in rankings.balanced
    )
    return f"""<section class="market-rankings">
      <h2>Full-market rankings</h2>
      {_market_ranking_metadata_html(rankings)}
      <h3>多策略共识 / Consensus</h3>
      <p class="consensus-note">Consensus is the exact intersection of rankings built
      from shared technical inputs; it is not independently validated predictive evidence.</p>
      <div class="ranking-table-wrap">
        <table class="ranking-table">
          <thead><tr><th>Code</th><th>Name</th><th>Trend</th>
          <th>Balanced</th><th>Components</th><th>Evidence</th>
          <th>Risks</th><th>Date</th><th>Provider</th></tr></thead>
          <tbody>{consensus_rows}</tbody>
        </table>
      </div>
      <h3>Trend Top 30</h3>
      {_ranking_html_table(trend_rows)}
      <h3>Balanced Top 30</h3>
      {_ranking_html_table(balanced_rows)}
    </section>"""


def _market_ranking_metadata_html(rankings: MarketRankings) -> str:
    return f"""<dl class="metadata">
        <dt>Scan date</dt><dd>{_html(rankings.scan_date)}</dd>
        <dt>Generated</dt><dd>{_html(rankings.generated_at.isoformat())}</dd>
        <dt>Rule</dt><dd>{_html(rankings.rule_version)}</dd>
        <dt>Providers</dt><dd>{_html(", ".join(rankings.provider_names))}</dd>
        <dt>Coverage</dt><dd>{rankings.coverage:.2%}
        ({rankings.valid_count}/{rankings.eligible_count} valid;
        {rankings.universe_count} universe)</dd>
        <dt>Exclusions</dt><dd>{_html(_format_reason_counts(rankings.exclusion_counts))}</dd>
        <dt>Failures</dt><dd>{_html(_format_reason_counts(rankings.failure_counts))}</dd>
      </dl>"""


def _ranking_html_table(rows: str) -> str:
    return f"""<div class="ranking-table-wrap">
        <table class="ranking-table">
          <thead><tr><th>Rank</th><th>Code</th><th>Name</th><th>Score</th>
          <th>Consensus</th><th>Components</th><th>Evidence</th>
          <th>Risks</th><th>Date</th><th>Provider</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
      </div>"""


def _render_ranking_html(
    record: MarketRanking,
    consensus: MarketConsensusRanking | None,
) -> str:
    row_class = ' class="consensus-row"' if consensus is not None else ""
    consensus_text = (
        "—"
        if consensus is None
        else (
            "多策略共识 / consensus — "
            f"Trend rank {consensus.trend_rank} / {consensus.trend_score:.2f}; "
            f"Balanced rank {consensus.balanced_rank} / "
            f"{consensus.balanced_score:.2f}"
        )
    )
    return f"""<tr{row_class}><td>{record.rank}</td><td>{_html(record.code)}</td>
      <td>{_html(record.name)}</td><td>{record.score:.2f}</td>
      <td>{_html(consensus_text)}</td>
      <td>{_html(_format_components(record.components))}</td>
      <td>{_html(_format_codes(record.evidence_codes))}</td>
      <td>{_html(_format_codes(record.risk_codes))}</td>
      <td>{_html(record.latest_trade_date.isoformat())}</td>
      <td>{_html(record.provider_name)}</td></tr>"""


def _render_consensus_html(record: MarketConsensusRanking) -> str:
    components = (
        f"trend profile: {_format_components(record.trend_components)}; "
        f"balanced profile: {_format_components(record.balanced_components)}"
    )
    return f"""<tr class="consensus-row"><td>{_html(record.code)}</td>
      <td>{_html(record.name)}</td>
      <td>Trend rank {record.trend_rank} / {record.trend_score:.2f}</td>
      <td>Balanced rank {record.balanced_rank} / {record.balanced_score:.2f}</td>
      <td>{_html(components)}</td>
      <td>{_html(_format_codes(record.evidence_codes))}</td>
      <td>{_html(_format_codes(record.risk_codes))}</td>
      <td>{_html(record.latest_trade_date.isoformat())}</td>
      <td>{_html(record.provider_name)}</td></tr>"""


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
    def bullets(values: Sequence[str]) -> str:
        items = values or ("None",)
        return "".join(f"<li>{_html(value)}</li>" for value in items)

    return f"""<article class="stock">
      <h3>{_html(stock.code)} — {_html(stock.name)}</h3>
      <p><strong>Decision:</strong> {_html(stock.decision_label)}</p>
      <p><strong>Structure:</strong> {_html(stock.structure.state_label)}
      ({_html(stock.structure.status)})</p>
      <p><strong>Provider:</strong> {_html(stock.provider_name)} ·
      <strong>Latest trade date:</strong> {_html(stock.latest_trade_date.isoformat())} ·
      <strong>Bars:</strong> {stock.bar_count}</p>
      <h4>Evidence</h4><ul>{bullets(stock.evidence)}</ul>
      <h4>Risks</h4><ul>{bullets(stock.risks)}</ul>
      <h4>Key levels</h4><ul>{bullets(stock.key_levels)}</ul>
      <h4>Next conditions</h4><ul>{bullets(stock.next_conditions)}</ul>
    </article>"""


def _md(value: object) -> str:
    text = str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    for character in r"\`*_{}[]()#+-.!|<>":
        text = text.replace(character, f"\\{character}")
    return text


def _html(value: object) -> str:
    return html.escape(str(value), quote=True)


__all__ = ["render_html", "render_markdown", "render_site_index"]
