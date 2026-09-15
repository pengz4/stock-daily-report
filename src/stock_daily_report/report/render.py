"""Deterministic Markdown and HTML renderers for validated reports."""

from __future__ import annotations

import html
from collections.abc import Sequence

from stock_daily_report.market_scan.models import MarketIndexState, MarketState
from stock_daily_report.report.labels import (
    component_label,
    evidence_label,
    metric_label,
    reason_label,
    risk_label,
    status_label,
    structure_label,
)
from stock_daily_report.report.models import (
    MarketConsensusRanking,
    MarketRanking,
    MarketRankingComponents,
    MarketRankings,
    PoolOverview,
    PoolOverviewRow,
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
        f"# A股每日研报 — {_md(metadata.report_date.isoformat())}",
        "",
        f"- 生成时间: {_md(metadata.generated_at.isoformat())}",
        f"- 最新数据时间: {_md(metadata.latest_source_timestamp.isoformat())}",
        f"- 数据质量: {_md(status_label(metadata.quality_status))}",
        f"- 自选股数量: {metadata.stock_count}",
        *(
            [f"- 深度分析数量: {metadata.analyzed_stock_count}"]
            if metadata.analyzed_stock_count is not None
            else []
        ),
        f"- 数据源: {_md(', '.join(metadata.provider_names))}",
        f"- 输入快照: `{_md(metadata.snapshot_path)}`",
        f"- 快照哈希: `{metadata.snapshot_hash}`",
        f"- 配置哈希: `{metadata.config_hash}`",
        f"- 分析器版本: {_md(metadata.analyzer_versions.structural)}",
        "",
    ]
    lines.extend(_render_market_state_markdown(report.market_state))
    lines.extend(
        [
            "## 市场摘要",
            "",
            _md(report.market_summary.text),
            "",
        ]
    )
    lines.extend(_render_market_rankings_markdown(report.market_rankings))
    lines.extend(
        [
            "## 自选股追踪",
            "",
        ]
    )
    for stock in _stocks_for_display(report.stocks):
        lines.extend(_render_stock_markdown(stock))
    lines.extend(_render_pool_overview_markdown(report.pool_overview))
    return "\n".join(lines) + "\n"


def render_html(report: ReportDocument) -> str:
    """Render a self-contained report page with escaped dynamic values."""

    metadata = report.metadata
    title = _html(f"A股每日研报 — {metadata.report_date.isoformat()}")
    header = _render_report_header(report, report.market_rankings)
    market_state = _render_market_state_html(report.market_state)
    market_summary = _render_market_summary_html(report)
    consensus_panel = _render_consensus_panel_html(report.market_rankings)
    trend_panel = _render_profile_panel_html(
        "trend",
        "趋势策略",
        report.market_rankings,
    )
    balanced_panel = _render_profile_panel_html(
        "balanced",
        "均衡策略",
        report.market_rankings,
    )
    stock_sections = "\n".join(
        _render_watchlist_card(stock) for stock in _stocks_for_display(report.stocks)
    )
    watchlist_panel = f"""<section>
      <h2>自选股跟踪</h2>
      {stock_sections}
    </section>"""
    pool_panel = _render_pool_overview_html(report.pool_overview)
    return f"""<!doctype html>
<html lang="zh-CN">
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
    {_render_panel_navigation()}
    {_render_mobile_panel("panel-market", "大盘", market_state + market_summary, open=True)}
    {_render_mobile_panel("panel-consensus", "多策略共识", consensus_panel)}
    {_render_mobile_panel("panel-trend", "趋势策略", trend_panel)}
    {_render_mobile_panel("panel-balanced", "均衡策略", balanced_panel)}
    {_render_mobile_panel("panel-watchlist", "自选股追踪", watchlist_panel)}
    {_render_mobile_panel("panel-pool", "全池速览", pool_panel)}
  </main>
</body>
</html>
"""


def _render_market_state_markdown(state: MarketState | None) -> list[str]:
    lines = ["## 市场状态", ""]
    if state is None:
        lines.extend(["市场状态不可用：未提供市场扫描状态。", ""])
        return lines
    lines.extend(
        [
            f"- 状态: {_md(_market_state_status_label(state.status))}",
            f"- 结论: {_md(state.conclusion)}",
            f"- 规则版本: {_md(state.rule_version)}",
            "",
            "| 指数 | 代码 | 收盘价 | 日涨跌 | 20日均线关系 | 60日均线关系 | 趋势 | 数据源 |",
            "| --- | --- | ---: | ---: | --- | --- | --- | --- |",
        ]
    )
    if state.status == "unavailable":
        lines.insert(2, "市场状态不可用：指数与市场广度均未提供有效数据。")
    for index in state.indices:
        lines.append(
            "| "
            + " | ".join(
                (
                    _md(index.name),
                    _md(index.code),
                    _md(_format_market_number(index.close)),
                    _md(_format_market_change(index.change_pct)),
                    _md(_market_relation_label(index.close_vs_ma20)),
                    _md(_market_relation_label(index.close_vs_ma60)),
                    _md(_market_trend_label(index.trend)),
                    _md(index.provider or "未提供"),
                )
            )
            + " |"
        )
    breadth = state.breadth
    lines.extend(
        [
            "",
            "### 市场广度",
            "",
            (
                f"上涨 {_md(_format_market_count(breadth.advancing_count))} "
                f"（{_md(_format_market_ratio(breadth.advancing_ratio))}），"
                f"下跌 {_md(_format_market_count(breadth.declining_count))} "
                f"（{_md(_format_market_ratio(breadth.declining_ratio))}），"
                f"平盘 {_md(_format_market_count(breadth.unchanged_count))}；"
                f"涨跌比 {_md(_format_market_ratio(breadth.advance_decline_ratio, digits=2))}。"
            ),
            (
                f"有效样本 {_md(_format_market_count(breadth.valid_count))} / "
                f"{_md(_format_market_count(breadth.total_count))}，"
                f"数据源 {_md(breadth.provider or '未提供')}。"
            ),
            "",
        ]
    )
    return lines


def _render_market_state_html(state: MarketState | None) -> str:
    if state is None:
        return """<section class="market-state unavailable">
      <h2>市场状态</h2>
      <p class="empty-state">市场状态不可用：未提供市场扫描状态。</p>
    </section>"""
    rows = "\n".join(_render_market_index_row(index) for index in state.indices)
    breadth = state.breadth
    state_class = (
        "market-state unavailable"
        if state.status == "unavailable"
        else "market-state"
    )
    unavailable_message = (
        '<p class="empty-state">市场状态不可用：指数与市场广度均未提供有效数据。</p>'
        if state.status == "unavailable"
        else ""
    )
    return f"""<section class="{state_class}">
      <h2>市场状态</h2>
      {unavailable_message}
      <p class="market-state__conclusion"><strong>{_html(_market_state_status_label(state.status))}</strong>：{_html(state.conclusion)}</p>
      <p class="market-state__rule">规则版本：{_html(state.rule_version)}</p>
      <div class="market-index-table-wrap">
        <table class="market-index-table">
          <thead>
            <tr><th>指数</th><th>代码</th><th>收盘价</th><th>日涨跌</th><th>20日均线</th><th>60日均线</th><th>趋势</th><th>数据源</th></tr>
          </thead>
          <tbody>
            {rows}
          </tbody>
        </table>
      </div>
      <section class="market-breadth">
        <h3>市场广度</h3>
        <p>
          上涨 {_html(_format_market_count(breadth.advancing_count))}（{_html(_format_market_ratio(breadth.advancing_ratio))}），
          下跌 {_html(_format_market_count(breadth.declining_count))}（{_html(_format_market_ratio(breadth.declining_ratio))}），
          平盘 {_html(_format_market_count(breadth.unchanged_count))}；
          涨跌比 {_html(_format_market_ratio(breadth.advance_decline_ratio, digits=2))}。
        </p>
        <p>有效样本 {_html(_format_market_count(breadth.valid_count))} / {_html(_format_market_count(breadth.total_count))}；数据源 {_html(breadth.provider or "未提供")}。</p>
      </section>
    </section>"""


def _render_market_index_row(index: MarketIndexState) -> str:
    return (
        "<tr>"
        f"<td>{_html(index.name)}</td>"
        f"<td>{_html(index.code)}</td>"
        f'<td class="numeric">{_html(_format_market_number(index.close))}</td>'
        f'<td class="numeric">{_html(_format_market_change(index.change_pct))}</td>'
        f"<td>{_html(_market_relation_label(index.close_vs_ma20))}</td>"
        f"<td>{_html(_market_relation_label(index.close_vs_ma60))}</td>"
        f"<td>{_html(_market_trend_label(index.trend))}</td>"
        f"<td>{_html(index.provider or '未提供')}</td>"
        "</tr>"
    )


def _render_panel_navigation() -> str:
    entries = (
        ("大盘", "panel-market"),
        ("多策略共识", "panel-consensus"),
        ("趋势策略", "panel-trend"),
        ("均衡策略", "panel-balanced"),
        ("自选股追踪", "panel-watchlist"),
        ("全池速览", "panel-pool"),
    )
    links = "\n".join(
        f'      <a href="#{anchor}">{_html(label)}</a>' for label, anchor in entries
    )
    return f"""<nav class="report-panels" aria-label="报告分区">
{links}
</nav>"""


def _render_mobile_panel(
    panel_id: str,
    label: str,
    body: str,
    *,
    open: bool = False,
) -> str:
    open_attribute = " open" if open else ""
    return f"""<details id="{_html(panel_id)}" class="mobile-panel"{open_attribute}>
      <summary>{_html(label)}</summary>
      {body}
    </details>"""


def _render_consensus_panel_html(rankings: MarketRankings) -> str:
    warning = _render_ranking_warning(rankings)
    if rankings.status == "unavailable":
        warning_html = f"\n      {warning}" if warning else ""
        return f"""<section class="market-rankings unavailable">
      <h2>多策略共识</h2>{warning_html}
      <p>全市场排名不可用。</p>
      <p><strong>原因：</strong> {_html(reason_label(rankings.unavailable_reason or "not_provided"))}</p>
    </section>"""
    warning_html = f"\n      {warning}" if warning else ""
    return f"""<section class="market-rankings">
      <h2>多策略共识</h2>{warning_html}
      {_render_consensus_cards(rankings.consensus)}
    </section>"""


def _render_profile_panel_html(
    profile: str,
    label: str,
    rankings: MarketRankings,
) -> str:
    if rankings.status == "unavailable":
        return f"""<section class="market-rankings unavailable">
      <h2>{_html(label)}</h2>
      <p class="empty-state">{_html(label)}排名不可用。</p>
    </section>"""
    records = rankings.trend if profile == "trend" else rankings.balanced
    consensus_by_code = {record.code: record for record in rankings.consensus}
    return f"""<section class="market-rankings">
      <h2>{_html(label)}</h2>
      {_render_ranking_profile(profile, records, consensus_by_code)}
    </section>"""


def _format_market_count(value: int | None) -> str:
    return "未提供" if value is None else str(value)


def _format_market_number(value: float | None) -> str:
    return "未提供" if value is None else f"{value:.2f}"


def _format_market_change(value: float | None) -> str:
    return "未提供" if value is None else f"{value:+.2f}%"


def _format_market_ratio(value: float | None, *, digits: int = 2) -> str:
    return "未提供" if value is None else f"{value:.{digits}%}"


def _market_relation_label(value: str | None) -> str:
    return {
        "above": "高于",
        "below": "低于",
        "equal": "接近",
    }.get(value or "", "未提供")


def _market_trend_label(value: str | None) -> str:
    return {
        "bullish": "偏强",
        "bearish": "偏弱",
        "neutral": "中性",
        "insufficient": "数据不足",
    }.get(value or "", "未提供")


def _market_state_status_label(value: str) -> str:
    return {
        "available": "可用",
        "partial": "部分可用",
        "unavailable": "不可用",
    }.get(value, status_label(value))


def _render_market_rankings_markdown(rankings: MarketRankings) -> list[str]:
    lines = ["## 全市场排名", ""]
    if rankings.status == "unavailable":
        lines.extend(
            [
                "全市场排名不可用。",
                "",
                f"- 原因: {_md(reason_label(rankings.unavailable_reason or 'not_provided'))}",
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
            "### 多策略共识",
            "",
            (
                "多策略共识是基于相同技术输入得到的排名交集，"
                "不代表经过独立回测验证的预测结论。"
            ),
            "",
            "| 代码 | 名称 | 趋势排名 | 均衡排名 | 评分构成 | 证据 | 风险 | 日期 | 数据源 |",
            "| --- | --- | ---: | ---: | --- | --- | --- | --- | --- |",
        ]
    )
    lines.extend(_render_consensus_markdown(row) for row in rankings.consensus)
    if not rankings.consensus:
        lines.append("| — | 无精确交集 | — | — | — | — | — | — | — |")
    lines.extend(["", "### 趋势策略 Top 30", ""])
    lines.extend(_ranking_markdown_table(rankings.trend, rankings.consensus))
    lines.extend(["", "### 均衡策略 Top 30", ""])
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
        f"- 扫描日期: {_md(_optional_market_metadata(rankings.scan_date))}",
        f"- 生成时间: {_md(generated_at)}",
        f"- 规则版本: {_md(_optional_market_metadata(rankings.rule_version))}",
        (
            "- 数据源: "
            f"{_md(', '.join(rankings.provider_names) or '—')}"
        ),
        (
            f"- 扫描覆盖率: {coverage} "
            f"（有效 {_optional_market_metadata(rankings.valid_count)} / "
            f"候选 {_optional_market_metadata(rankings.eligible_count)}；"
            f"股票池 {_optional_market_metadata(rankings.universe_count)}）"
        ),
        f"- 排除统计: {_md(_format_reason_counts(rankings.exclusion_counts))}",
        f"- 失败统计: {_md(_format_reason_counts(rankings.failure_counts))}",
    ]


def _ranking_markdown_table(
    records: Sequence[MarketRanking],
    consensus: Sequence[MarketConsensusRanking],
) -> list[str]:
    consensus_by_code = {record.code: record for record in consensus}
    lines = [
        "| 排名 | 代码 | 名称 | 评分 | 共识 | 评分构成 | 证据 | 风险 | 日期 | 数据源 |",
        "| ---: | --- | --- | ---: | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        consensus_record = consensus_by_code.get(record.code)
        consensus_text = (
            "—"
            if consensus_record is None
            else (
                "多策略共识 — "
                f"趋势第 {consensus_record.trend_rank} 名 / "
                f"{consensus_record.trend_score:.2f}；"
                f"均衡第 {consensus_record.balanced_rank} 名 / "
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
                    _md(_format_codes(record.evidence_codes, kind="evidence")),
                    _md(_format_codes(record.risk_codes, kind="risk")),
                    _md(record.latest_trade_date.isoformat()),
                    _md(record.provider_name),
                )
            )
            + " |"
        )
    return lines


def _render_consensus_markdown(record: MarketConsensusRanking) -> str:
    component_text = (
        f"趋势策略：{_format_components(record.trend_components)}；"
        f"均衡策略：{_format_components(record.balanced_components)}"
    )
    return (
        "| "
        + " | ".join(
            (
                _md(record.code),
                _md(record.name),
                f"趋势第 {record.trend_rank} 名 / {record.trend_score:.2f}",
                f"均衡第 {record.balanced_rank} 名 / {record.balanced_score:.2f}",
                _md(component_text),
                _md(_format_codes(record.evidence_codes, kind="evidence")),
                _md(_format_codes(record.risk_codes, kind="risk")),
                _md(record.latest_trade_date.isoformat()),
                _md(record.provider_name),
            )
        )
        + " |"
    )


def _render_pool_overview_markdown(pool: PoolOverview | None) -> list[str]:
    """Render the lightweight full-pool overview table; empty in legacy mode."""
    if pool is None:
        return []
    lines = [
        "",
        "## 全池速览",
        "",
        f"- 行情快照日期: {_md(pool.quote_date.isoformat())}",
    ]
    if pool.unavailable_reason:
        lines.append(
            f"- 行情快照不可用: {_md(reason_label(pool.unavailable_reason))}"
        )
    ranked = _pool_has_scan_ranking(pool)
    header = "| 代码 | 名称 | 分组 | 层级 | 最新价 | 涨跌幅% | 成交额(元) |"
    divider = "| ---: | --- | --- | --- | ---: | ---: | ---: |"
    if ranked:
        header = "| 排名 | 评分 | 代码 | 名称 | 分组 | 层级 | 最新价 | 涨跌幅% | 成交额(元) |"
        divider = "| ---: | ---: | ---: | --- | --- | --- | ---: | ---: | ---: |"
    lines.extend(["", header, divider])
    for row in _pool_rows_for_display(pool):
        cells = [
            _md(row.code),
            _md(row.name),
            _md(row.group),
            _md(row.priority),
            _EM_DASH if row.latest_price is None else f"{row.latest_price:.2f}",
            _EM_DASH if row.change_pct is None else f"{row.change_pct:+.2f}",
            _EM_DASH if row.amount is None else f"{row.amount:.0f}",
        ]
        if ranked:
            cells = [
                _EM_DASH if row.scan_rank is None else str(row.scan_rank),
                _EM_DASH if row.scan_score is None else f"{row.scan_score:.2f}",
                *cells,
            ]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _pool_rows_for_display(pool: PoolOverview) -> Sequence[PoolOverviewRow]:
    """Order rows by scan rank when present, then by change percent desc."""
    return sorted(
        pool.rows,
        key=lambda row: (
            row.scan_rank is None,
            row.scan_rank if row.scan_rank is not None else 0,
            row.change_pct is None,
            -(row.change_pct if row.change_pct is not None else 0.0),
            row.code,
        ),
    )


def _pool_has_scan_ranking(pool: PoolOverview) -> bool:
    return any(row.scan_rank is not None for row in pool.rows)


def _render_pool_overview_html(pool: PoolOverview | None) -> str:
    """Render the lightweight full-pool overview section for the HTML page."""
    if pool is None:
        return ""
    unavailable_html = (
        f'\n      <p class="pool-overview-warning">行情快照不可用：'
        f"{_html(reason_label(pool.unavailable_reason))}</p>"
        if pool.unavailable_reason
        else ""
    )
    ranked = _pool_has_scan_ranking(pool)
    header_cells = "<th>排名</th><th>评分</th>" if ranked else ""
    row_html: list[str] = []
    for row in _pool_rows_for_display(pool):
        rank_cells = ""
        if ranked:
            rank_text = _EM_DASH if row.scan_rank is None else str(row.scan_rank)
            score_text = _EM_DASH if row.scan_score is None else f"{row.scan_score:.2f}"
            rank_cells = (
                f'<td class="numeric">{_html(rank_text)}</td>'
                f'<td class="numeric">{_html(score_text)}</td>'
            )
        row_html.append(
            f'<tr class="pool-row" data-priority="{_html(row.priority)}">\n'
            f"        {rank_cells}"
            f"<td>{_html(row.code)}</td>"
            f"<td>{_html(row.name)}</td>"
            f"<td>{_html(row.group)}</td>"
            f"<td>{_html(row.priority)}</td>"
            f'<td class="numeric">{_format_html_float(row.latest_price)}</td>'
            f'<td class="numeric {_pool_change_class(row.change_pct)}">'
            f"{_format_html_float(row.change_pct)}</td>"
            f'<td class="numeric">{_format_html_amount(row.amount)}</td>'
            f"\n      </tr>"
        )
    rows = "\n".join(row_html)
    return f"""<section class="pool-overview">
      <h2>全池速览</h2>{unavailable_html}
      <p>行情快照日期：{_html(pool.quote_date.isoformat())}</p>
      <table class="pool-table">
        <thead>
          <tr>{header_cells}<th>代码</th><th>名称</th><th>分组</th><th>层级</th><th>最新价</th><th>涨跌幅%</th><th>成交额(元)</th></tr>
        </thead>
        <tbody>
{rows}
        </tbody>
      </table>
    </section>"""


def _pool_change_class(change_pct: float | None) -> str:
    if change_pct is None:
        return ""
    if change_pct > 0:
        return "up"
    if change_pct < 0:
        return "down"
    return "flat"


def _format_html_amount(amount: float | None) -> str:
    return _EM_DASH if amount is None else f"{amount:.0f}"



def _render_market_rankings_html(rankings: MarketRankings) -> str:
    warning = _render_ranking_warning(rankings)
    if rankings.status == "unavailable":
        warning_html = f"\n      {warning}" if warning else ""
        return f"""<section class="market-rankings unavailable">
      <h2>全市场排名</h2>{warning_html}
      <p>全市场排名不可用。</p>
      <p><strong>原因：</strong> {_html(reason_label(rankings.unavailable_reason or "not_provided"))}</p>
    </section>"""

    consensus_by_code = {record.code: record for record in rankings.consensus}
    warning_html = f"\n      {warning}" if warning else ""
    return f"""<section class="market-rankings">
      <h2>全市场排名</h2>{warning_html}
      {_render_consensus_cards(rankings.consensus)}
      {_render_ranking_profile("trend", rankings.trend, consensus_by_code)}
      {_render_ranking_profile("balanced", rankings.balanced, consensus_by_code)}
    </section>"""


def _render_report_header(report: ReportDocument, rankings: MarketRankings) -> str:
    metadata = report.metadata
    return f"""<header class="report-header">
      <p><a href="../../site/index.html">返回报告列表</a></p>
      <p><strong>报告日期：</strong> {_html(metadata.report_date.strftime("%Y/%m/%d"))}</p>
      <p><strong>最新交易日：</strong> {_render_latest_trading_text(report, rankings)}</p>
      <p class="quality-badge">数据质量：{_html(status_label(metadata.quality_status))}</p>
      {_render_runtime_metadata(report, rankings)}
    </header>"""


def _render_market_summary_html(report: ReportDocument) -> str:
    summary_metrics = _render_summary_metrics(report.market_rankings)
    summary_metrics_html = f"\n      {summary_metrics}" if summary_metrics else ""
    return f"""<section class="market-summary">
      <h2>市场摘要</h2>
      <p>{_html(report.market_summary.text)}</p>{summary_metrics_html}
    </section>"""


def _render_runtime_metadata(report: ReportDocument, rankings: MarketRankings) -> str:
    metadata = report.metadata
    has_ranking_metadata = _has_market_ranking_metadata(rankings)
    report_fields = "".join(
        (
            _render_html_field("报告生成时间", metadata.generated_at.isoformat()),
            _render_html_field(
                "报告最新数据时间",
                metadata.latest_source_timestamp.isoformat(),
            ),
            _render_html_field(
                "报告数据源",
                _sequence_text(metadata.provider_names),
            ),
            _render_html_field("股票数量", metadata.stock_count),
            *(
                (_render_html_field("深度分析数量", metadata.analyzed_stock_count),)
                if metadata.analyzed_stock_count is not None
                else ()
            ),
            _render_html_field("快照路径", metadata.snapshot_path),
            _render_html_field("快照哈希", metadata.snapshot_hash),
            _render_html_field("报告配置哈希", metadata.config_hash),
            _render_html_field(
                "分析器",
                metadata.analyzer_versions.structural,
            ),
            (
                ""
                if has_ranking_metadata
                else _render_html_field("排名状态", status_label(rankings.status))
            ),
            (
                ""
                if has_ranking_metadata
                else _render_html_field(
                    "排名原因",
                    reason_label(rankings.unavailable_reason or "not_provided"),
                )
            ),
        )
    )
    ranking_metadata_section = ""
    if has_ranking_metadata:
        ranking_fields = "".join(
            (
                _render_html_field("排名状态", status_label(rankings.status)),
                _render_html_field(
                    "排名原因",
                    reason_label(rankings.unavailable_reason or "not_provided"),
                ),
                _render_html_field(
                    "排名扫描日期",
                    rankings.scan_date.isoformat()
                    if rankings.scan_date is not None
                    else None,
                ),
                _render_html_field(
                    "排名生成时间",
                    rankings.generated_at.isoformat()
                    if rankings.generated_at is not None
                    else None,
                ),
                _render_html_field("排名规则版本", rankings.rule_version),
                _render_html_field(
                    "排名数据源",
                    _sequence_text(rankings.provider_names),
                ),
                _render_html_field("排名配置哈希", rankings.config_hash),
                _render_html_field("排名输入哈希", rankings.input_hash),
                _render_html_field("股票池总数", rankings.universe_count),
                _render_html_field("候选股票", rankings.eligible_count),
                _render_html_field("有效股票", rankings.valid_count),
                _render_html_field(
                    "扫描覆盖率",
                    f"{rankings.coverage:.2%}"
                    if rankings.coverage is not None
                    else None,
                ),
                _render_html_field(
                    "排除统计",
                    _format_reason_counts(rankings.exclusion_counts),
                ),
                _render_html_field(
                    "失败统计",
                    _format_reason_counts(rankings.failure_counts),
                ),
            )
        )
        ranking_metadata_section = f"""
      <div class="runtime-metadata__section">
        <h3>排名元数据</h3>
        {ranking_fields}
      </div>"""
    body = f"""<div class="runtime-metadata__section">
        <h3>报告元数据</h3>
        {report_fields}
      </div>{ranking_metadata_section}"""
    return _render_html_details("运行时元数据", body, class_name="runtime-metadata")


def _render_summary_metrics(rankings: MarketRankings) -> str:
    cards: list[str] = []
    if rankings.coverage is not None:
        cards.append(
            _render_metric_card(
                "coverage",
                "扫描覆盖率",
                f"{rankings.coverage:.2%}",
            )
        )
    if rankings.valid_count is not None and rankings.eligible_count is not None:
        cards.append(
            _render_metric_card(
                "valid",
                "有效 / 候选股票",
                f"{rankings.valid_count} / {rankings.eligible_count}",
            )
        )
    if rankings.universe_count is not None:
        cards.append(
            _render_metric_card(
                "universe",
                "股票池总数",
                str(rankings.universe_count),
            )
        )
    if rankings.status == "available":
        cards.append(
            _render_metric_card(
                "consensus",
                "多策略共识",
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
                f"扫描日期 {_html(scan_date)} · "
                f"自选股最新 {_format_html_optional(watchlist_text)}"
            )
        return f"扫描日期 {_html(scan_date)}"
    if watchlist_latest is None:
        return _EM_DASH
    return f"自选股最新 {_html(watchlist_latest.isoformat())}"


def _render_ranking_warning(rankings: MarketRankings) -> str:
    if rankings.status == "unavailable":
        return (
            '<p class="ranking-warning">全市场排名不可用：'
            f"{_html(reason_label(rankings.unavailable_reason or 'not_provided'))}</p>"
        )
    if rankings.failure_counts:
        return (
            '<p class="ranking-warning">排名审计提示：存在失败记录 — '
            f"{_html(_format_reason_counts(rankings.failure_counts))}</p>"
        )
    return ""


def _render_consensus_cards(
    consensus: Sequence[MarketConsensusRanking],
) -> str:
    note = (
        '<p class="consensus-note">多策略共识是两个排名在共享技术输入上的'
        "精确交集；它不是独立验证的预测性证据。</p>"
    )
    if not consensus:
        return f"""<section class="consensus-section">
      <h3>多策略共识</h3>
      {note}
      <p class="empty-state">本次扫描没有多策略交集。</p>
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
      <h3>多策略共识</h3>
      {note}
      <div class="consensus-grid">
        {cards}
      </div>
    </section>"""


def _render_consensus_card(record: MarketConsensusRanking) -> str:
    detail_body = "".join(
        (
            _render_html_field("代码", record.code),
            _render_html_field("名称", record.name),
            _render_html_field("趋势排名", record.trend_rank),
            _render_html_field("趋势评分", f"{record.trend_score:.2f}"),
            _render_html_field("均衡排名", record.balanced_rank),
            _render_html_field("均衡评分", f"{record.balanced_score:.2f}"),
            _render_component_group("趋势组件", record.trend_components),
            _render_component_group(
                "均衡组件",
                record.balanced_components,
            ),
            _render_html_tag_field(
                "证据",
                record.evidence_codes,
                kind="evidence",
            ),
            _render_html_tag_field("风险", record.risk_codes, kind="risk"),
            _render_html_field(
                "最新交易日",
                record.latest_trade_date.isoformat(),
            ),
            _render_html_field("数据源", record.provider_name),
        )
    )
    return f"""<article class="consensus-card">
      <div class="consensus-row">
        <h4>{_html(record.code)} — {_html(record.name)}</h4>
        <p class="consensus-trend">趋势排名 {record.trend_rank} / {_format_html_float(record.trend_score)}</p>
        <p class="consensus-balanced">均衡排名 {record.balanced_rank} / {_format_html_float(record.balanced_score)}</p>
        <p>趋势组件：{_html(_format_components(record.trend_components))}</p>
        <p>均衡组件：{_html(_format_components(record.balanced_components))}</p>
        <p>证据：{_render_html_tags(record.evidence_codes, kind="evidence")}</p>
        <p>风险：{_render_html_tags(record.risk_codes, kind="risk")}</p>
        <p>日期：{_html(record.latest_trade_date.isoformat())}</p>
        <p>数据源：{_html(record.provider_name)}</p>
      </div>
      {_render_html_details(
          f"{record.code} 的审计详情",
          detail_body,
          class_name="consensus-detail",
      )}
    </article>"""


def _render_ranking_profile(
    profile: str,
    records: Sequence[MarketRanking],
    consensus_by_code: dict[str, MarketConsensusRanking],
) -> str:
    profile_title = "趋势策略 Top 10" if profile == "trend" else "均衡策略 Top 10"
    profile_scope = "趋势策略 Top 30" if profile == "trend" else "均衡策略 Top 30"
    if not records:
        content = '<p class="empty-state">暂无排名记录。</p>'
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
                f"{profile_title} 完整排名（第 11–30 名）",
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
        "多策略共识"
        if consensus is not None
        else "无共识"
    )
    short_risk = risk_label(record.risk_codes[0]) if record.risk_codes else "无"
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
        _render_html_field("排名", record.rank),
        _render_html_field("代码", record.code),
        _render_html_field("名称", record.name),
        _render_html_field("评分", f"{record.score:.2f}"),
    ]
    if consensus is not None:
        fields.extend(
            (
                _render_html_field(
                    f"趋势排名 {consensus.trend_rank}",
                    f"{consensus.trend_score:.2f}",
                ),
                _render_html_field(
                    f"均衡排名 {consensus.balanced_rank}",
                    f"{consensus.balanced_score:.2f}",
                ),
            )
        )
    fields.extend(
        _render_html_field(
            component_label(component_name),
            f"{getattr(record.components, component_name):.2f}",
        )
        for component_name in _COMPONENT_FIELDS
    )
    fields.extend(
        (
            _render_html_tag_field("证据", record.evidence_codes, kind="evidence"),
            _render_html_tag_field("风险", record.risk_codes, kind="risk"),
            _render_html_field(
                "最新交易日",
                record.latest_trade_date.isoformat(),
            ),
            _render_html_field("数据源", record.provider_name),
        )
    )
    return _render_html_details(
        f"#{record.rank} {record.code} 的详情",
        "".join(fields),
        class_name="ranking-detail",
    )


def _render_component_group(
    title: str,
    components: MarketRankingComponents,
) -> str:
    fields = "".join(
        _render_html_field(
            component_label(component_name),
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
        f"{component_label('trend')} {components.trend:.2f}，"
        f"{component_label('momentum')} {components.momentum:.2f}，"
        f"{component_label('volume')} {components.volume:.2f}，"
        f"{component_label('structure')} {components.structure:.2f}，"
        f"{component_label('risk')} {components.risk:.2f}"
    )


def _format_codes(codes: Sequence[str], *, kind: str = "generic") -> str:
    if not codes:
        return "无"
    if kind == "evidence":
        return "、".join(evidence_label(code) for code in codes)
    if kind == "risk":
        return "、".join(risk_label(code) for code in codes)
    return "、".join(codes)


def _format_reason_counts(counts: Sequence[object]) -> str:
    if not counts:
        return "无"
    return "、".join(f"{reason_label(item.code)} {item.count}项" for item in counts)


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
        return f'<span class="{tag_class}">无</span>'
    labels = (
        [evidence_label(value) for value in values]
        if kind == "evidence"
        else [risk_label(value) for value in values]
        if kind == "risk"
        else list(values)
    )
    return " ".join(
        f'<span class="{tag_class}">{_html(value)}</span>' for value in labels
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
        links = "      <li>暂无已发布的报告。</li>"
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>A股每日研报</title>
  <link rel="stylesheet" href="styles.css">
</head>
<body>
  <main>
    <h1>A股每日研报</h1>
    <p>静态、可校验的每日研报产物。</p>
    <ul>
{links}
    </ul>
  </main>
</body>
</html>
"""


def _render_stock_markdown(stock: StockReport) -> list[str]:
    rank_line = (
        f"- 计算排名: 第 {stock.scan_rank} 名（评分 {stock.scan_score:.2f}）"
        if stock.scan_rank is not None and stock.scan_score is not None
        else "- 计算排名: 未纳入本次排名"
    )
    lines = [
        (
            f"### #{stock.scan_rank} — {_md(stock.code)} — {_md(stock.name)}"
            if stock.scan_rank is not None
            else f"### {_md(stock.code)} — {_md(stock.name)}"
        ),
        "",
        f"- 分组: {_md(stock.group)}",
        rank_line,
        f"- 数据源: {_md(stock.provider_name)}",
        f"- 最新交易日: {_md(stock.latest_trade_date.isoformat())}",
        f"- 最新数据时间: {_md(stock.latest_source_timestamp.isoformat())}",
        f"- K线数量: {stock.bar_count}",
        f"- 数据质量: {_md(status_label(stock.quality_status))}",
        f"- 决策: **{_md(stock.decision_label)}**",
        (
            f"- 结构: {_md(structure_label(stock.structure.state_label))} "
            f"（{_md(structure_label(stock.structure.status))}）"
        ),
        "",
        "### 技术指标",
    ]
    for field_name, value in stock.metrics.model_dump().items():
        formatted = _format_metric_value(field_name, value)
        lines.append(f"- {metric_label(field_name)}: {formatted}")
    lines.append("")
    lines.append("### 证据与风险")
    lines.extend(
        f"- 证据: {_md(evidence_label(item))}"
        for item in stock.evidence or ("无",)
    )
    lines.extend(
        f"- 风险: {_md(risk_label(item))}" for item in stock.risks or ("无",)
    )
    lines.append("### 关键价位")
    lines.extend(
        f"- {_md(_readable_key_level(item))}" for item in stock.key_levels or ("无",)
    )
    lines.append("### 后续条件")
    lines.extend(f"- {_md(item)}" for item in stock.next_conditions or ("无",))
    lines.append("")
    return lines


def _render_stock_html(stock: StockReport) -> str:
    return _render_watchlist_card(stock)


def _render_watchlist_card(stock: StockReport) -> str:
    primary_metrics = "".join(
        _render_html_field(
            metric_label(field_name),
            _format_metric_value(field_name, value),
        )
        for field_name in _PRIMARY_METRIC_FIELDS
        for value in (getattr(stock.metrics, field_name),)
    )
    quality_body = "".join(
        (
            _render_html_field("K线数量", stock.bar_count),
            _render_html_field("质量状态", status_label(stock.quality_status)),
            _render_html_list_field(
                "质量问题",
                tuple(reason_label(item) for item in stock.quality_issues),
            ),
        )
    )
    metrics_body = "".join(
        _render_html_field(
            metric_label(field_name),
            _format_metric_value(field_name, value),
        )
        for field_name in _ALL_METRIC_FIELDS
        for value in (getattr(stock.metrics, field_name),)
    )
    structure_levels = [
        f"{structure_label(level.kind)} {level.price:.2f}（{structure_label(level.source)}）"
        for level in stock.structure.levels
    ]
    structure_body = "".join(
        (
            _render_html_field("规则版本", stock.structure.rule_version),
            _render_html_list_field("结构价位", structure_levels),
            _render_html_list_field(
                "结构观察",
                tuple(structure_label(item) for item in stock.structure.observations),
            ),
        )
    )
    signals_body = "".join(
        (
            _render_html_list_field(
                "证据", tuple(evidence_label(item) for item in stock.evidence)
            ),
            _render_html_list_field(
                "风险", tuple(risk_label(item) for item in stock.risks)
            ),
            _render_html_list_field(
                "关键价位",
                tuple(_readable_key_level(item) for item in stock.key_levels),
            ),
            _render_html_list_field("后续条件", stock.next_conditions),
        )
    )
    return f"""<article class="stock-card">
      <div class="stock-card__identity">
        <h3>{_html(_stock_heading(stock))}</h3>
        <p><strong>分组：</strong> {_html(stock.group)}</p>
        <p><strong>计算排名：</strong> {_html(_stock_rank_text(stock))}</p>
        <p><strong>决策：</strong> {_html(stock.decision_label)}</p>
        <p><strong>结构：</strong> {_html(structure_label(stock.structure.state_label))}（{_html(structure_label(stock.structure.status))}）</p>
        <p><strong>最新交易日：</strong> {_html(stock.latest_trade_date.isoformat())}</p>
        <p><strong>最新数据时间：</strong> {_html(stock.latest_source_timestamp.isoformat())}</p>
        <p><strong>数据源：</strong> {_html(stock.provider_name)}</p>
      </div>
      <div class="stock-card__metrics">
        {primary_metrics}
      </div>
      {_render_html_details("数据质量与审计", quality_body, class_name="stock-detail")}
      {_render_html_details("全部指标", metrics_body, class_name="stock-detail")}
      {_render_html_details("结构分析详情", structure_body, class_name="stock-detail")}
      {_render_html_details("信号、风险与关键价位", signals_body, class_name="stock-detail")}
    </article>"""


def _stocks_for_display(stocks: Sequence[StockReport]) -> list[StockReport]:
    """Display ranked stocks first, then unranked core stocks by code."""

    return sorted(
        stocks,
        key=lambda stock: (
            stock.scan_rank is None,
            stock.scan_rank if stock.scan_rank is not None else 0,
            stock.code,
        ),
    )


def _stock_rank_text(stock: StockReport) -> str:
    if stock.scan_rank is None or stock.scan_score is None:
        return "未纳入本次排名"
    return f"第 {stock.scan_rank} 名（评分 {stock.scan_score:.2f}）"


def _stock_heading(stock: StockReport) -> str:
    prefix = f"#{stock.scan_rank} — " if stock.scan_rank is not None else ""
    return f"{prefix}{stock.code} — {stock.name}"


def _format_metric_value(name: str, value: float | None) -> str:
    if value is None:
        return "—"
    if name in {"return20", "return60", "return120", "drawdown60"}:
        return f"{value:.2%}"
    if name == "realized_volatility20":
        return f"{value:.2%}"
    if name == "volume_ratio20":
        return f"{value:.2f} 倍"
    return f"{value:.2f}"


def _readable_key_level(value: str) -> str:
    main, separator, source = value.partition(" (")
    kind, _, detail = main.partition(" ")
    readable_detail = detail.replace("(", "（").replace(")", "）")
    readable = f"{structure_label(kind)}{(' ' + readable_detail) if detail else ''}"
    if separator:
        readable += f"（{structure_label(source.rstrip(')'))}）"
    return readable


def _md(value: object) -> str:
    text = str(value).replace("\r\n", " ").replace("\r", " ").replace("\n", " ")
    for character in r"\`*_{}[]()#+-.!|<>":
        text = text.replace(character, f"\\{character}")
    return text


def _html(value: object) -> str:
    return html.escape(str(value), quote=True)


__all__ = ["render_html", "render_markdown", "render_site_index"]
