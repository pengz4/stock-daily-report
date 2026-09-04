"""Deterministic Markdown and HTML renderers for validated reports."""

from __future__ import annotations

import html
from collections.abc import Sequence

from stock_daily_report.report.models import ReportDocument, StockReport


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
        "## Watchlist",
        "",
    ]
    for stock in report.stocks:
        lines.extend(_render_stock_markdown(stock))
    return "\n".join(lines) + "\n"


def render_html(report: ReportDocument) -> str:
    """Render a self-contained report page with escaped dynamic values."""

    metadata = report.metadata
    stock_sections = "\n".join(_render_stock_html(stock) for stock in report.stocks)
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
    <section>
      <h2>Watchlist</h2>
      {stock_sections}
    </section>
  </main>
</body>
</html>
"""


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
    text = str(value)
    for character in r"\`*_{}[]()#+-.!|<>":
        text = text.replace(character, f"\\{character}")
    return text


def _html(value: object) -> str:
    return html.escape(str(value), quote=True)


__all__ = ["render_html", "render_markdown", "render_site_index"]
