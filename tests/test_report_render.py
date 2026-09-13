import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import ClassVar

import pytest

import stock_daily_report.report.models as report_models
from stock_daily_report.report.models import (
    AnalyzerMetadata,
    MarketSummary,
    ReportDocument,
    ReportMetadata,
    ReportMetrics,
    StockReport,
    StructureLevel,
    StructureSummary,
)
from stock_daily_report.report.render import render_html, render_markdown


def _market_rankings_metadata():
    return {
        "scan_date": date(2026, 9, 4),
        "generated_at": datetime(2026, 9, 4, 8, 30, tzinfo=UTC),
        "rule_version": "market-scan-v1",
        "config_hash": "c" * 64,
        "input_hash": "d" * 64,
        "provider_names": ("fixture", "fake-universe"),
        "universe_count": 100,
        "eligible_count": 80,
        "valid_count": 72,
        "coverage": 0.9,
        "exclusion_counts": (
            report_models.MarketScanReasonCount(code="halted", count=4),
            report_models.MarketScanReasonCount(code="st", count=20),
        ),
        "failure_counts": (
            report_models.MarketScanReasonCount(code="network_error", count=8),
            report_models.MarketScanReasonCount(code="price_missing", count=2),
        ),
    }


def _market_ranking_components(
    trend: float,
    momentum: float,
    volume: float,
    structure: float,
    risk: float,
):
    return report_models.MarketRankingComponents(
        trend=trend,
        momentum=momentum,
        volume=volume,
        structure=structure,
        risk=risk,
    )


def _market_ranking_record(
    *,
    code: str,
    name: str,
    profile: str,
    rank: int,
    score: float,
    components,
    evidence_codes: tuple[str, ...],
    risk_codes: tuple[str, ...],
    latest_trade_date: date = date(2026, 9, 4),
    provider_name: str = "fixture",
):
    return report_models.MarketRanking(
        code=code,
        name=name,
        profile=profile,
        rank=rank,
        score=score,
        components=components,
        evidence_codes=evidence_codes,
        risk_codes=risk_codes,
        latest_trade_date=latest_trade_date,
        provider_name=provider_name,
    )


def _market_consensus_record(
    *,
    code: str,
    name: str,
    trend_rank: int,
    trend_score: float,
    balanced_rank: int,
    balanced_score: float,
    trend_components,
    balanced_components,
    evidence_codes: tuple[str, ...],
    risk_codes: tuple[str, ...],
    latest_trade_date: date = date(2026, 9, 4),
    provider_name: str = "fixture",
):
    return report_models.MarketConsensusRanking(
        code=code,
        name=name,
        trend_rank=trend_rank,
        trend_score=trend_score,
        balanced_rank=balanced_rank,
        balanced_score=balanced_score,
        trend_components=trend_components,
        balanced_components=balanced_components,
        evidence_codes=evidence_codes,
        risk_codes=risk_codes,
        latest_trade_date=latest_trade_date,
        provider_name=provider_name,
    )


def _market_rankings_available_rows():
    shared_codes = {
        "first": "600519",
        "second": "000001",
    }
    shared_components = _market_ranking_components(90.0, 80.0, 70.0, 60.0, 50.0)
    shared_evidence = ("close_above_ma20",)
    shared_risks = ("elevated_volatility",)
    escaped_evidence = ("close_above_ma20", "trend & signal")
    escaped_risks = ("elevated_volatility", "risk <tight>")

    trend_rows = [
        _market_ranking_record(
            code=shared_codes["first"],
            name="贵州茅台",
            profile="trend",
            rank=1,
            score=82.0,
            components=shared_components,
            evidence_codes=shared_evidence,
            risk_codes=shared_risks,
        ),
        _market_ranking_record(
            code=shared_codes["second"],
            name="平安银行",
            profile="trend",
            rank=2,
            score=81.0,
            components=shared_components,
            evidence_codes=escaped_evidence,
            risk_codes=escaped_risks,
        ),
    ]
    for rank, code, name in (
        (3, "600010", "趋势样本03"),
        (4, "600011", "趋势样本04"),
        (5, "600012", "趋势样本05"),
        (6, "600013", "趋势样本06"),
        (7, "600014", "趋势样本07"),
        (8, "600015", "趋势样本08"),
        (9, "600016", "趋势样本09"),
        (10, "600017", "趋势样本10"),
        (11, "600018", "趋势样本11"),
        (12, "600019", "趋势样本12"),
        (13, "600020", "趋势样本13"),
        (14, "600021", "趋势样本14"),
        (15, "600022", "趋势样本15"),
        (16, "600023", "趋势样本16"),
        (17, "600024", "趋势样本17"),
        (18, "600025", "趋势样本18"),
        (19, "600026", "趋势样本19"),
        (20, "600027", "趋势样本20"),
        (21, "600028", "趋势样本21"),
        (22, "600029", "趋势样本22"),
        (23, "600030", "趋势样本23"),
        (24, "600031", "趋势样本24"),
        (25, "600032", "趋势样本25"),
        (26, "600033", "趋势样本26"),
        (27, "600034", "趋势样本27"),
        (28, "600035", "趋势样本28"),
        (29, "600036", "趋势样本29"),
        (30, "600037", "趋势样本30"),
    ):
        trend_rows.append(
            _market_ranking_record(
                code=code,
                name=name,
                profile="trend",
                rank=rank,
                score=79.5 - (rank - 3) * 0.5,
                components=_market_ranking_components(
                    95.0 - rank,
                    85.0 - rank,
                    75.0 - rank,
                    65.0 - rank,
                    55.0 - rank,
                ),
                evidence_codes=(f"trend evidence {rank} <escape>",),
                risk_codes=(f"trend risk {rank} & review",),
            )
        )

    balanced_rows = [
        _market_ranking_record(
            code=shared_codes["second"],
            name="平安银行",
            profile="balanced",
            rank=1,
            score=76.0,
            components=shared_components,
            evidence_codes=escaped_evidence,
            risk_codes=escaped_risks,
        ),
        _market_ranking_record(
            code=shared_codes["first"],
            name="贵州茅台",
            profile="balanced",
            rank=2,
            score=75.0,
            components=shared_components,
            evidence_codes=shared_evidence,
            risk_codes=shared_risks,
        ),
    ]
    for rank, code, name in (
        (3, "000002", "平衡样本03"),
        (4, "000333", "平衡样本04"),
        (5, "000568", "平衡样本05"),
    ):
        balanced_rows.append(
            _market_ranking_record(
                code=code,
                name=name,
                profile="balanced",
                rank=rank,
                score=74.5 - (rank - 3) * 0.5,
                components=_market_ranking_components(
                    92.0 - rank,
                    82.0 - rank,
                    72.0 - rank,
                    62.0 - rank,
                    52.0 - rank,
                ),
                evidence_codes=(f"balanced evidence {rank} [escape]",),
                risk_codes=(f"balanced risk {rank} <watch>",),
            )
        )

    consensus_rows = [
        _market_consensus_record(
            code=shared_codes["first"],
            name="贵州茅台",
            trend_rank=1,
            trend_score=82.0,
            balanced_rank=2,
            balanced_score=75.0,
            trend_components=shared_components,
            balanced_components=shared_components,
            evidence_codes=shared_evidence,
            risk_codes=shared_risks,
        ),
        _market_consensus_record(
            code=shared_codes["second"],
            name="平安银行",
            trend_rank=2,
            trend_score=81.0,
            balanced_rank=1,
            balanced_score=76.0,
            trend_components=shared_components,
            balanced_components=shared_components,
            evidence_codes=escaped_evidence,
            risk_codes=escaped_risks,
        ),
    ]
    return tuple(trend_rows), tuple(balanced_rows), tuple(consensus_rows)


def _market_rankings():
    trend, balanced, consensus = _market_rankings_available_rows()
    return report_models.MarketRankings(
        status="available",
        unavailable_reason=None,
        **_market_rankings_metadata(),
        trend=trend,
        balanced=balanced,
        consensus=consensus,
    )


def _market_rankings_unavailable():
    return report_models.MarketRankings(
        status="unavailable",
        unavailable_reason="scan_rankings_unavailable",
        **_market_rankings_metadata(),
        trend=(),
        balanced=(),
        consensus=(),
    )


def _market_rankings_no_consensus():
    metadata = _market_rankings_metadata()
    metadata["failure_counts"] = ()
    trend = (
        _market_ranking_record(
            code="600100",
            name="趋势空交集01",
            profile="trend",
            rank=1,
            score=72.0,
            components=_market_ranking_components(88.0, 78.0, 68.0, 58.0, 48.0),
            evidence_codes=("disjoint trend evidence <one>",),
            risk_codes=("disjoint trend risk & one",),
        ),
        _market_ranking_record(
            code="600101",
            name="趋势空交集02",
            profile="trend",
            rank=2,
            score=71.0,
            components=_market_ranking_components(87.0, 77.0, 67.0, 57.0, 47.0),
            evidence_codes=("disjoint trend evidence <two>",),
            risk_codes=("disjoint trend risk & two",),
        ),
    )
    balanced = (
        _market_ranking_record(
            code="000100",
            name="平衡空交集01",
            profile="balanced",
            rank=1,
            score=74.0,
            components=_market_ranking_components(86.0, 76.0, 66.0, 56.0, 46.0),
            evidence_codes=("disjoint balanced evidence [one]",),
            risk_codes=("disjoint balanced risk <one>",),
        ),
        _market_ranking_record(
            code="000101",
            name="平衡空交集02",
            profile="balanced",
            rank=2,
            score=73.0,
            components=_market_ranking_components(85.0, 75.0, 65.0, 55.0, 45.0),
            evidence_codes=("disjoint balanced evidence [two]",),
            risk_codes=("disjoint balanced risk <two>",),
        ),
    )
    return report_models.MarketRankings(
        status="available",
        unavailable_reason=None,
        **metadata,
        trend=trend,
        balanced=balanced,
        consensus=(),
    )


def test_market_rankings_no_consensus_fixture_is_isolated_and_valid():
    rankings = _market_rankings_no_consensus()

    assert rankings.status == "available"
    assert rankings.consensus == ()
    assert {record.code for record in rankings.trend}.isdisjoint(
        {record.code for record in rankings.balanced}
    )
    assert rankings.failure_counts == ()


def _document(
    name: str = "<script>alert(1)</script>",
    *,
    market_rankings=None,
) -> ReportDocument:
    document = {
        "metadata": ReportMetadata(
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
        "market_summary": MarketSummary(
            status="unavailable",
            text="Broad market data unavailable; watchlist-only summary.",
        ),
        "stocks": (
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
    }
    if market_rankings is not None:
        document["market_rankings"] = market_rankings
    return ReportDocument.model_validate(document)


@dataclass
class _HtmlElement:
    tag: str
    attrs: dict[str, str]
    children: list[object] = field(default_factory=list)

    def normalized_text(self) -> str:
        return " ".join(self._text_content().split())

    def normalized_visible_text(
        self,
        *,
        exclude_selectors: tuple[str, ...] = (),
    ) -> str:
        excluded = {
            id(node)
            for selector in exclude_selectors
            for node in self.select(selector)
        }
        return " ".join(self._visible_text(excluded).split())

    def select(self, selector: str) -> list["_HtmlElement"]:
        return _select_all(self, selector)

    def _text_content(self) -> str:
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, _HtmlElement):
                parts.append(child._text_content())
            else:
                parts.append(child)
        return "".join(parts)

    def _visible_text(self, excluded: set[int]) -> str:
        parts: list[str] = []
        for child in self.children:
            if isinstance(child, _HtmlElement):
                if id(child) in excluded:
                    continue
                parts.append(child._visible_text(excluded))
            else:
                parts.append(child)
        return "".join(parts)


class _HtmlTreeBuilder(HTMLParser):
    _VOID_TAGS: ClassVar[frozenset[str]] = frozenset(
        {"br", "hr", "img", "input", "link", "meta"}
    )

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _HtmlElement(tag="document", attrs={})
        self._stack = [self.root]

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        element = _HtmlElement(
            tag=tag,
            attrs={key: value or "" for key, value in attrs},
        )
        self._stack[-1].children.append(element)
        if tag not in self._VOID_TAGS:
            self._stack.append(element)

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self._stack) - 1, 0, -1):
            if self._stack[index].tag == tag:
                del self._stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if data:
            self._stack[-1].children.append(data)


_SELECTOR_ATTR_RE = re.compile(r'\[([a-zA-Z0-9_-]+)="([^"]*)"\]')
_SELECTOR_CLASS_RE = re.compile(r"\.([a-zA-Z0-9_-]+)")
_SELECTOR_TAG_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_-]*")


def _html_tree(html: str) -> _HtmlElement:
    builder = _HtmlTreeBuilder()
    builder.feed(html)
    builder.close()
    return builder.root


def _select_all(node: _HtmlElement, selector: str) -> list[_HtmlElement]:
    matches = [node]
    for token in selector.split():
        next_matches: list[_HtmlElement] = []
        seen: set[int] = set()
        for current in matches:
            for descendant in _iter_descendants(current):
                if (
                    _matches_selector_part(descendant, token)
                    and id(descendant) not in seen
                ):
                    next_matches.append(descendant)
                    seen.add(id(descendant))
        matches = next_matches
    return matches


def _iter_descendants(node: _HtmlElement):
    for child in node.children:
        if isinstance(child, _HtmlElement):
            yield child
            yield from _iter_descendants(child)


def _matches_selector_part(node: _HtmlElement, token: str) -> bool:
    attrs = dict(_SELECTOR_ATTR_RE.findall(token))
    token = _SELECTOR_ATTR_RE.sub("", token)
    tag_match = _SELECTOR_TAG_RE.match(token)
    tag = tag_match.group(0) if tag_match else None
    classes = set(_SELECTOR_CLASS_RE.findall(token))
    node_classes = {
        value for value in node.attrs.get("class", "").split() if value
    }
    return (
        (tag is None or node.tag == tag)
        and classes.issubset(node_classes)
        and all(node.attrs.get(name) == value for name, value in attrs.items())
    )


def _require_one(node: _HtmlElement, selector: str) -> _HtmlElement:
    matches = node.select(selector)
    assert matches, f"expected selector {selector!r} in HTML contract"
    return matches[0]


def _extract_first_code(node: _HtmlElement) -> str:
    match = re.search(r"\b\d{6}\b", node.normalized_text())
    assert match, f"expected a six-digit stock code in {node.normalized_text()!r}"
    return match.group(0)


def _extract_leading_rank(node: _HtmlElement) -> int:
    match = re.match(
        r"^(?:rank\s+)?#?\s*(\d{1,2})\b",
        node.normalized_text(),
        flags=re.IGNORECASE,
    )
    assert match, f"expected a leading rank in {node.normalized_text()!r}"
    return int(match.group(1))


def _assert_text_contains_number(text: str, value: float) -> None:
    tokens = {str(value), f"{value:.1f}", f"{value:.2f}"}
    assert any(token in text for token in tokens), (
        f"expected one of {sorted(tokens)!r} in {text!r}"
    )


def _assert_code_count_pair(text: str, *, code: str, count: int) -> None:
    assert re.search(rf"{re.escape(code)}\D+{count}\b", text), (
        f"expected {code!r} paired with {count} in {text!r}"
    )


def _assert_details_closed(node: _HtmlElement) -> None:
    assert "open" not in node.attrs


def _ranking_pairs(records) -> set[tuple[int, str]]:
    return {(record.rank, record.code) for record in records}


def _row_rank_code_pairs(rows: list[_HtmlElement]) -> set[tuple[int, str]]:
    return {(_extract_leading_rank(row), _extract_first_code(row)) for row in rows}


def _row_detail_controls(row: _HtmlElement) -> list[_HtmlElement]:
    controls = row.select("details.ranking-detail")
    if controls:
        return controls
    return [
        child
        for child in row.children
        if isinstance(child, _HtmlElement)
        and child.tag == "details"
        and child.select("summary")
    ]


def _assert_rows_have_scoped_ranking_details(rows, expected_records) -> None:
    expected_pairs = _ranking_pairs(expected_records)

    assert _row_rank_code_pairs(rows) == expected_pairs

    actual_pairs: set[tuple[int, str]] = set()
    for row in rows:
        controls = _row_detail_controls(row)
        pair = (_extract_leading_rank(row), _extract_first_code(row))
        assert len(controls) == 1, (
            f"expected one scoped detail control for ranking row {pair!r}"
        )
        _assert_details_closed(controls[0])
        actual_pairs.add(pair)

    assert actual_pairs == expected_pairs


def _document_with_shifted_watchlist_date(market_rankings) -> ReportDocument:
    document = _document(name="visible", market_rankings=market_rankings)
    shifted_stock = document.stocks[0].model_copy(
        update={
            "latest_trade_date": date(2026, 9, 3),
            "latest_source_timestamp": datetime(2026, 9, 3, 15, 45, tzinfo=UTC),
        }
    )
    return document.model_copy(update={"stocks": (shifted_stock,)})


def _market_rankings_with_dense_consensus():
    rankings = _market_rankings()
    balanced_rows = []
    consensus_rows = []
    for balanced_rank, trend_index in enumerate((1, 0, 3, 2, 5, 4), start=1):
        trend_record = rankings.trend[trend_index]
        balanced_record = _market_ranking_record(
            code=trend_record.code,
            name=trend_record.name,
            profile="balanced",
            rank=balanced_rank,
            score=74.0 - balanced_rank,
            components=trend_record.components,
            evidence_codes=trend_record.evidence_codes,
            risk_codes=trend_record.risk_codes,
            latest_trade_date=trend_record.latest_trade_date,
            provider_name=trend_record.provider_name,
        )
        balanced_rows.append(balanced_record)
        consensus_rows.append(
            _market_consensus_record(
                code=trend_record.code,
                name=trend_record.name,
                trend_rank=trend_record.rank,
                trend_score=trend_record.score,
                balanced_rank=balanced_record.rank,
                balanced_score=balanced_record.score,
                trend_components=trend_record.components,
                balanced_components=balanced_record.components,
                evidence_codes=tuple(sorted(set(trend_record.evidence_codes))),
                risk_codes=tuple(sorted(set(trend_record.risk_codes))),
                latest_trade_date=trend_record.latest_trade_date,
                provider_name=trend_record.provider_name,
            )
        )
    reordered = tuple(
        consensus_rows[index] for index in (4, 1, 5, 0, 3, 2)
    )
    return report_models.MarketRankings.model_validate(
        {
            **rankings.model_dump(),
            "balanced": tuple(
                record.model_dump() for record in balanced_rows
            ),
            "consensus": tuple(
                record.model_dump() for record in reordered
            ),
        }
    )


def _document_with_watchlist_contract_fields() -> ReportDocument:
    document = _document(name="visible", market_rankings=_market_rankings())
    detailed_stock = document.stocks[0].model_copy(
        update={
            "name": "Contract Fixture Holdings",
            "group": "consumer leaders",
            "provider_name": "fixture-primary",
            "latest_trade_date": date(2026, 9, 3),
            "latest_source_timestamp": datetime(2026, 9, 3, 15, 45, tzinfo=UTC),
            "bar_count": 123,
            "quality_issues": ("gap_risk", "manual_review <needed>"),
            "metrics": ReportMetrics(
                close=100.0,
                ma20=98.5,
                ma60=95.0,
                return20=0.12,
                return60=0.24,
                realized_volatility20=0.31,
                drawdown60=-0.05,
                volume_ratio20=1.5,
                recent_high20=110.0,
                recent_low20=88.0,
            ),
            "structure": StructureSummary(
                state_label="breakout_ready",
                status="candidate",
                rule_version="simplified-v2",
                levels=(
                    StructureLevel(
                        kind="support",
                        price=95.0,
                        source="swing_low",
                    ),
                    StructureLevel(
                        kind="resistance",
                        price=110.0,
                        source="recent_high",
                    ),
                ),
                observations=("tight range", "higher low <tracked>"),
            ),
            "decision_label": "偏强",
            "evidence": ("volume confirmation", "breakout <pending>"),
            "risks": ("overbought & extended",),
            "key_levels": ("support 95", "resistance 110"),
            "next_conditions": ("hold above 100", "break 110"),
        }
    )
    empty_details_stock = StockReport(
        code="000001",
        name="Empty Detail Bank",
        group="banks",
        provider_name="fixture-secondary",
        latest_trade_date=date(2026, 9, 2),
        latest_source_timestamp=datetime(2026, 9, 2, 15, 15, tzinfo=UTC),
        bar_count=81,
        quality_status="passed",
        quality_issues=(),
        metrics=ReportMetrics(close=50.0),
        structure=StructureSummary(
            state_label="range_bound",
            status="confirmed",
            rule_version="simplified-v2",
            levels=(),
            observations=(),
        ),
        decision_label="观察",
        evidence=(),
        risks=(),
        key_levels=(),
        next_conditions=(),
    )
    return document.model_copy(
        update={"stocks": (detailed_stock, empty_details_stock)}
    )


def _document_with_malicious_contract_strings() -> ReportDocument:
    rankings = _market_rankings().model_dump()
    rankings["trend"][0]["name"] = "Trend <script>alert(10)</script>"
    rankings["trend"][0]["evidence_codes"] = ()
    rankings["trend"][0]["risk_codes"] = ()
    rankings["balanced"][1]["name"] = "Trend <script>alert(10)</script>"
    rankings["balanced"][1]["evidence_codes"] = ()
    rankings["balanced"][1]["risk_codes"] = ()
    rankings["consensus"][0]["name"] = "Trend <script>alert(10)</script>"
    rankings["consensus"][0]["evidence_codes"] = ()
    rankings["consensus"][0]["risk_codes"] = ()
    rankings["trend"][1]["evidence_codes"] = (
        "signal <script>alert(11)</script>",
    )
    rankings["balanced"][0]["evidence_codes"] = (
        "signal <script>alert(11)</script>",
    )
    rankings["consensus"][1]["evidence_codes"] = (
        "signal <script>alert(11)</script>",
    )
    document = _document(
        name="Watchlist <script>alert(12)</script>",
        market_rankings=report_models.MarketRankings.model_validate(rankings),
    )
    stock = document.stocks[0].model_copy(
        update={
            "evidence": (),
            "risks": (),
            "key_levels": ("support <img src=x onerror=alert(13)>",),
            "next_conditions": ("wait & see",),
        }
    )
    return document.model_copy(update={"stocks": (stock,)})


def test_renderers_escape_untrusted_markdown_and_html_text():
    document = _document()

    markdown = render_markdown(document)
    html = render_html(document)

    assert r"\<script\>alert\(1\)\</script\>" in markdown
    assert r"safe \[evidence\]" in markdown
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html


def test_markdown_normalizes_newlines_in_untrusted_watchlist_text():
    document = _document().model_copy(
        update={
            "stocks": (
                _document().stocks[0].model_copy(
                    update={
                        "name": "safe name\n# Injected heading\n- injected item",
                        "provider_name": "provider\n## Injected provider",
                    },
                ),
            )
        }
    )

    markdown = render_markdown(document)

    assert "\n# Injected heading\n" not in markdown
    assert "\n- injected item\n" not in markdown
    assert "\n## Injected provider\n" not in markdown
    assert r"safe name \# Injected heading \- injected item" in markdown
    assert r"provider \#\# Injected provider" in markdown


def test_renderers_do_not_include_notification_secrets():
    document = _document(name="visible")
    markdown = render_markdown(document)
    html = render_html(document)

    assert "https://secret.example/webhook" not in markdown
    assert "https://secret.example/webhook" not in html


def test_market_scan_rankings_render_consistently_without_confidence_claims():
    document = _document(name="visible", market_rankings=_market_rankings())

    payload = document.model_dump(mode="json")
    markdown = render_markdown(document)
    html = render_html(document)

    assert payload["schema_version"] == 2
    assert payload["market_rankings"]["trend"][0]["components"]["trend"] == 90.0
    assert payload["market_rankings"]["consensus"][0] == {
        "code": "600519",
        "name": "贵州茅台",
        "trend_rank": 1,
        "trend_score": 82.0,
        "balanced_rank": 2,
        "balanced_score": 75.0,
        "trend_components": {
            "trend": 90.0,
            "momentum": 80.0,
            "volume": 70.0,
            "structure": 60.0,
            "risk": 50.0,
        },
        "balanced_components": {
            "trend": 90.0,
            "momentum": 80.0,
            "volume": 70.0,
            "structure": 60.0,
            "risk": 50.0,
        },
        "evidence_codes": ["close_above_ma20"],
        "risk_codes": ["elevated_volatility"],
        "latest_trade_date": "2026-09-04",
        "provider_name": "fixture",
    }
    for rendered in (markdown, html):
        assert "Trend Top 30" in rendered
        assert "Balanced Top 30" in rendered
        assert "多策略共识" in rendered
        assert "shared technical inputs" in rendered
        assert "not independently validated predictive evidence" in rendered
        assert "600519" in rendered
        assert "82.00" in rendered
        assert "75.00" in rendered
        assert "90.00%" in rendered
        assert "high confidence" not in rendered.lower()
    assert r"close\_above\_ma20" in markdown
    assert r"elevated\_volatility" in markdown
    assert "close_above_ma20" in html
    assert "elevated_volatility" in html
    assert "Trend rank 1 / 82.00" in markdown
    assert "Balanced rank 2 / 75.00" in markdown
    assert 'class="consensus-row"' in html


def test_market_scan_html_contract_renders_header_metrics_and_runtime_metadata():
    document = _document_with_shifted_watchlist_date(_market_rankings())
    html = render_html(document)
    root = _html_tree(html)

    header = _require_one(root, "header.report-header")
    quality_badge = _require_one(header, ".quality-badge")
    runtime_metadata = _require_one(header, "details.runtime-metadata")
    _require_one(runtime_metadata, "summary")
    _require_one(root, ".summary-metrics")
    coverage_card = _require_one(root, '.metric-card[data-metric="coverage"]')
    valid_card = _require_one(root, '.metric-card[data-metric="valid"]')
    universe_card = _require_one(root, '.metric-card[data-metric="universe"]')
    consensus_card = _require_one(root, '.metric-card[data-metric="consensus"]')

    assert document.metadata.quality_status in quality_badge.normalized_text()
    header_text = header.normalized_visible_text(
        exclude_selectors=("details.runtime-metadata",)
    )
    header_text_lower = header_text.lower()
    scan_date = document.market_rankings.scan_date.isoformat()
    watchlist_date = document.stocks[0].latest_trade_date.isoformat()
    assert "scan date" in header_text_lower
    assert "watchlist" in header_text_lower
    assert "latest trading" in header_text_lower
    assert scan_date in header_text
    assert watchlist_date in header_text
    assert header_text.index(scan_date) < header_text.index(watchlist_date)
    _assert_details_closed(runtime_metadata)

    assert f"{document.market_rankings.coverage:.2%}" in coverage_card.normalized_text()
    valid_text = valid_card.normalized_text()
    assert str(document.market_rankings.valid_count) in valid_text
    assert str(document.market_rankings.eligible_count) in valid_text
    assert str(document.market_rankings.universe_count) in universe_card.normalized_text()
    assert str(len(document.market_rankings.consensus)) in consensus_card.normalized_text()

    runtime_text = runtime_metadata.normalized_text()
    for expected in (
        document.metadata.generated_at.isoformat(),
        document.metadata.latest_source_timestamp.isoformat(),
        ", ".join(document.metadata.provider_names),
        str(document.metadata.stock_count),
        document.metadata.snapshot_path,
        document.metadata.snapshot_hash,
        document.metadata.config_hash,
        document.metadata.analyzer_versions.structural,
        document.market_rankings.scan_date.isoformat(),
        document.market_rankings.generated_at.isoformat(),
        document.market_rankings.rule_version,
        ", ".join(document.market_rankings.provider_names),
        document.market_rankings.config_hash,
        document.market_rankings.input_hash,
        str(document.market_rankings.universe_count),
        str(document.market_rankings.eligible_count),
        str(document.market_rankings.valid_count),
        f"{document.market_rankings.coverage:.2%}",
    ):
        assert expected in runtime_text
    for reason_count in (
        *document.market_rankings.exclusion_counts,
        *document.market_rankings.failure_counts,
    ):
        assert reason_count.code in runtime_text
        _assert_code_count_pair(
            runtime_text,
            code=reason_count.code,
            count=reason_count.count,
        )

    unavailable_document = _document_with_shifted_watchlist_date(
        _market_rankings_unavailable()
    )
    unavailable_header = _require_one(
        _html_tree(render_html(unavailable_document)),
        "header.report-header",
    )
    unavailable_header_text = unavailable_header.normalized_visible_text(
        exclude_selectors=("details.runtime-metadata",)
    )
    unavailable_header_text_lower = unavailable_header_text.lower()
    unavailable_watchlist_date = unavailable_document.stocks[0].latest_trade_date.isoformat()
    unavailable_scan_date = unavailable_document.market_rankings.scan_date.isoformat()
    assert "watchlist" in unavailable_header_text_lower
    assert "latest trading" in unavailable_header_text_lower
    assert unavailable_watchlist_date in unavailable_header_text
    assert unavailable_scan_date not in unavailable_header_text


@pytest.mark.parametrize(
    ("record", "expected_position"),
    [
        pytest.param(
            ("600519", 1, 2),
            0,
            id="trend-rank-1-before-trend-rank-2",
        ),
        pytest.param(
            ("000001", 2, 1),
            1,
            id="trend-rank-2-before-later-ranks-even-with-better-balanced-rank",
        ),
    ],
)
def test_market_scan_html_contract_limits_and_orders_consensus_cards(
    record,
    expected_position,
):
    rankings = _market_rankings_with_dense_consensus()
    document = _document(name="visible", market_rankings=rankings)
    root = _html_tree(render_html(document))

    cards = root.select(".consensus-card")
    expected = sorted(
        document.market_rankings.consensus,
        key=lambda ranking: (
            ranking.trend_rank,
            ranking.balanced_rank,
            ranking.code,
        ),
    )[:5]

    assert len(document.market_rankings.consensus) == 6
    assert len(cards) == 5
    assert [_extract_first_code(card) for card in cards] == [
        ranking.code for ranking in expected
    ]
    code, trend_rank, balanced_rank = record
    assert [ranking.code for ranking in expected].index(code) == expected_position
    assert next(
        ranking for ranking in expected if ranking.code == code
    ).trend_rank == trend_rank
    assert next(
        ranking for ranking in expected if ranking.code == code
    ).balanced_rank == balanced_rank
    for card, ranking in zip(cards, expected):
        trend = _require_one(card, ".consensus-trend")
        balanced = _require_one(card, ".consensus-balanced")
        assert str(ranking.trend_rank) in trend.normalized_text()
        assert f"{ranking.trend_score:.2f}" in trend.normalized_text()
        assert str(ranking.balanced_rank) in balanced.normalized_text()
        assert f"{ranking.balanced_score:.2f}" in balanced.normalized_text()


def test_market_scan_html_contract_orders_tied_consensus_cards_by_code():
    base_document = _document(name="visible", market_rankings=_market_rankings())

    # This intentionally bypasses model validation because valid rankings cannot
    # contain identical (trend_rank, balanced_rank) pairs; the synthetic tie
    # exercises the renderer's defensive deterministic sort-by-code fallback.
    tied_consensus = (
        report_models.MarketConsensusRanking.model_construct(
            code="600519",
            name="贵州茅台",
            trend_rank=1,
            trend_score=82.0,
            balanced_rank=1,
            balanced_score=76.0,
            trend_components=_market_ranking_components(90.0, 80.0, 70.0, 60.0, 50.0),
            balanced_components=_market_ranking_components(
                90.0, 80.0, 70.0, 60.0, 50.0
            ),
            evidence_codes=("close_above_ma20",),
            risk_codes=("elevated_volatility",),
            latest_trade_date=date(2026, 9, 4),
            provider_name="fixture",
        ),
        report_models.MarketConsensusRanking.model_construct(
            code="000001",
            name="平安银行",
            trend_rank=1,
            trend_score=82.0,
            balanced_rank=1,
            balanced_score=76.0,
            trend_components=_market_ranking_components(90.0, 80.0, 70.0, 60.0, 50.0),
            balanced_components=_market_ranking_components(
                90.0, 80.0, 70.0, 60.0, 50.0
            ),
            evidence_codes=("close_above_ma20",),
            risk_codes=("elevated_volatility",),
            latest_trade_date=date(2026, 9, 4),
            provider_name="fixture",
        ),
    )
    tied_rankings = report_models.MarketRankings.model_construct(
        status="available",
        unavailable_reason=None,
        scan_date=base_document.market_rankings.scan_date,
        generated_at=base_document.market_rankings.generated_at,
        rule_version=base_document.market_rankings.rule_version,
        config_hash=base_document.market_rankings.config_hash,
        input_hash=base_document.market_rankings.input_hash,
        provider_names=base_document.market_rankings.provider_names,
        universe_count=base_document.market_rankings.universe_count,
        eligible_count=base_document.market_rankings.eligible_count,
        valid_count=base_document.market_rankings.valid_count,
        coverage=base_document.market_rankings.coverage,
        exclusion_counts=base_document.market_rankings.exclusion_counts,
        failure_counts=base_document.market_rankings.failure_counts,
        trend=base_document.market_rankings.trend,
        balanced=base_document.market_rankings.balanced,
        consensus=tied_consensus,
    )
    document = ReportDocument.model_construct(
        schema_version=base_document.schema_version,
        metadata=base_document.metadata,
        market_summary=base_document.market_summary,
        market_rankings=tied_rankings,
        stocks=base_document.stocks,
    )

    root = _html_tree(render_html(document))
    cards = root.select(".consensus-card")

    assert [_extract_first_code(card) for card in cards[:2]] == ["000001", "600519"]


def test_market_scan_html_contract_uses_compact_rankings_and_full_disclosure():
    document = _document(name="visible", market_rankings=_market_rankings())
    html = render_html(document)
    root = _html_tree(html)
    expected_trend_visible = list(document.market_rankings.trend[:10])
    expected_trend_full = list(document.market_rankings.trend[10:])
    expected_balanced = list(document.market_rankings.balanced)

    trend_visible = _require_one(
        root,
        '.ranking-profile[data-profile="trend"] .ranking-visible',
    )
    trend_full = _require_one(
        root,
        '.ranking-profile[data-profile="trend"] details.full-ranking',
    )
    balanced_profile = _require_one(
        root,
        '.ranking-profile[data-profile="balanced"]',
    )
    balanced_visible = _require_one(balanced_profile, ".ranking-visible")

    trend_visible_rows = trend_visible.select(".ranking-row")
    trend_full_rows = trend_full.select(".ranking-row")
    balanced_visible_rows = balanced_visible.select(".ranking-row")

    assert root.select(".ranking-table") == []
    assert root.select(".ranking-table-wrap") == []
    assert "<table" not in html.lower()
    assert root.select("article.stock") == []
    _require_one(trend_full, "summary")
    _assert_details_closed(trend_full)
    assert len(trend_visible_rows) == 10
    assert [_extract_leading_rank(row) for row in trend_visible_rows] == list(
        range(1, 11)
    )
    assert [_extract_leading_rank(row) for row in trend_full_rows] == list(
        range(11, 31)
    )
    assert len(balanced_visible_rows) == 5
    assert _row_rank_code_pairs(trend_visible_rows) == _ranking_pairs(
        expected_trend_visible
    )
    assert _row_rank_code_pairs(trend_full_rows) == _ranking_pairs(expected_trend_full)
    assert [_extract_leading_rank(row) for row in balanced_visible_rows] == [
        ranking.rank for ranking in expected_balanced
    ]
    assert [_extract_first_code(row) for row in balanced_visible_rows] == [
        ranking.code for ranking in expected_balanced
    ]
    _assert_rows_have_scoped_ranking_details(
        trend_visible_rows,
        expected_trend_visible,
    )
    _assert_rows_have_scoped_ranking_details(
        trend_full_rows,
        expected_trend_full,
    )
    _assert_rows_have_scoped_ranking_details(
        balanced_visible_rows,
        expected_balanced,
    )
    for row, ranking in zip(balanced_visible_rows, expected_balanced, strict=True):
        row_text = row.normalized_text()
        assert ranking.name in row_text
        assert f"{ranking.score:.2f}" in row_text
    assert balanced_profile.select(".full-ranking") == []


def test_market_scan_html_contract_keeps_detail_fields_and_failure_warning():
    document = _document(name="visible", market_rankings=_market_rankings())
    root = _html_tree(render_html(document))

    warning = _require_one(root, ".ranking-warning")
    trend_visible = _require_one(
        root,
        '.ranking-profile[data-profile="trend"] .ranking-visible',
    )
    detail = _row_detail_controls(
        next(
            row
            for row in trend_visible.select(".ranking-row")
            if (
                _extract_leading_rank(row),
                _extract_first_code(row),
            )
            == (
                document.market_rankings.trend[0].rank,
                document.market_rankings.trend[0].code,
            )
        )
    )[0]
    runtime_metadata = _require_one(root, "details.runtime-metadata")

    assert warning.normalized_text()
    assert root.select('.ranking-profile[data-profile="trend"] .ranking-row')
    _assert_details_closed(detail)
    detail_text = detail.normalized_text()
    for expected in (
        document.market_rankings.trend[0].code,
        document.market_rankings.trend[0].name,
        "trend",
        "momentum",
        "volume",
        "structure",
        "risk",
        "close_above_ma20",
        "elevated_volatility",
        document.market_rankings.trend[0].latest_trade_date.isoformat(),
        document.market_rankings.trend[0].provider_name,
        "Trend rank 1",
        "Balanced rank 2",
        "82.00",
        "75.00",
    ):
        assert expected in detail_text
    components = document.market_rankings.trend[0].components
    for value in (
        components.trend,
        components.momentum,
        components.volume,
        components.structure,
        components.risk,
    ):
        assert f"{value:.2f}" in detail_text
    runtime_text = runtime_metadata.normalized_text()
    _assert_code_count_pair(runtime_text, code="network_error", count=8)


def test_market_scan_html_contract_handles_unavailable_and_no_consensus_states():
    unavailable_root = _html_tree(
        render_html(
            _document(name="visible", market_rankings=_market_rankings_unavailable())
        )
    )

    assert "Full-market rankings unavailable" in unavailable_root.normalized_text()
    assert "scan_rankings_unavailable" in unavailable_root.normalized_text()
    _require_one(unavailable_root, ".ranking-warning")
    assert unavailable_root.select(".ranking-profile") == []
    assert unavailable_root.select(".consensus-card") == []

    no_consensus_root = _html_tree(
        render_html(
            _document(name="visible", market_rankings=_market_rankings_no_consensus())
        )
    )
    empty_state = _require_one(no_consensus_root, ".consensus-section .empty-state")
    assert "No exact intersection for this scan" in empty_state.normalized_text()
    assert no_consensus_root.select(".consensus-card") == []


def test_market_scan_html_contract_retains_watchlist_fields_and_none_states():
    document = _document_with_watchlist_contract_fields()
    root = _html_tree(render_html(document))

    stock_cards = root.select("article.stock-card")
    assert len(stock_cards) == 2
    assert root.select("article.stock") == []

    primary_stock = document.stocks[0]
    primary_card = next(
        card for card in stock_cards if primary_stock.code in card.normalized_text()
    )
    primary_summary_text = primary_card.normalized_visible_text(
        exclude_selectors=("details",)
    )
    primary_text = primary_card.normalized_text()

    for expected in (
        primary_stock.code,
        primary_stock.name,
        primary_stock.group,
        primary_stock.provider_name,
        primary_stock.latest_trade_date.isoformat(),
        primary_stock.latest_source_timestamp.isoformat(),
        str(primary_stock.bar_count),
        primary_stock.quality_status,
        *primary_stock.quality_issues,
        primary_stock.structure.state_label,
        primary_stock.structure.status,
        primary_stock.structure.rule_version,
        *primary_stock.structure.observations,
        primary_stock.decision_label,
        *primary_stock.evidence,
        *primary_stock.risks,
        *primary_stock.key_levels,
        *primary_stock.next_conditions,
    ):
        assert expected in primary_text
    for expected in (
        primary_stock.code,
        primary_stock.name,
        primary_stock.group,
        primary_stock.decision_label,
        primary_stock.structure.state_label,
        primary_stock.structure.status,
        primary_stock.latest_trade_date.isoformat(),
        primary_stock.latest_source_timestamp.isoformat(),
        primary_stock.provider_name,
        "close",
        "ma20",
        "ma60",
    ):
        assert expected in primary_summary_text
    for value in (
        primary_stock.metrics.close,
        primary_stock.metrics.ma20,
        primary_stock.metrics.ma60,
    ):
        assert value is not None
        _assert_text_contains_number(primary_summary_text, value)
    for detail_only in (
        str(primary_stock.bar_count),
        primary_stock.quality_status,
        *primary_stock.quality_issues,
        primary_stock.structure.rule_version,
        *primary_stock.structure.observations,
        *primary_stock.evidence,
        *primary_stock.risks,
        *primary_stock.key_levels,
        *primary_stock.next_conditions,
        "return20",
        "return60",
        "realized_volatility20",
        "drawdown60",
        "volume_ratio20",
        "recent_high20",
        "recent_low20",
    ):
        assert detail_only not in primary_summary_text
    for field_name, value in primary_stock.metrics.model_dump().items():
        assert field_name in primary_text
        if value is not None:
            _assert_text_contains_number(primary_text, value)
    for level in primary_stock.structure.levels:
        assert level.kind in primary_text
        assert level.source in primary_text
        _assert_text_contains_number(primary_text, level.price)
    assert primary_card.select("details summary")

    empty_card = next(
        card for card in stock_cards if document.stocks[1].code in card.normalized_text()
    )
    empty_text = empty_card.normalized_text()
    for pattern in (
        r"Quality issues\W+None",
        r"(?:Structure )?Levels\W+None",
        r"(?:Structure )?Observations\W+None",
        r"Evidence\W+None",
        r"Risks\W+None",
        r"Key levels\W+None",
        r"Next conditions\W+None",
    ):
        assert re.search(pattern, empty_text)


def test_market_scan_html_contract_escapes_static_controls_and_legacy_fallback():
    document = _document_with_malicious_contract_strings()
    payload_before = document.model_dump(mode="json")
    markdown_before = render_markdown(document)

    html = render_html(document)
    root = _html_tree(html)

    assert payload_before == document.model_dump(mode="json")
    assert markdown_before == render_markdown(document)
    assert "Trend <script>alert(10)</script>" not in html
    assert "signal <script>alert(11)</script>" not in html
    assert "support <img src=x onerror=alert(13)>" not in html
    assert "Trend &lt;script&gt;alert(10)&lt;/script&gt;" in html
    assert "signal &lt;script&gt;alert(11)&lt;/script&gt;" in html
    assert "support &lt;img src=x onerror=alert(13)&gt;" in html
    details = root.select("details")
    summaries = root.select("summary")
    assert details
    assert summaries
    assert all("open" not in detail.attrs for detail in details)
    assert root.select("script") == []
    details_text = " ".join(detail.normalized_text() for detail in details)
    assert re.search(r"Evidence\W+None", details_text)
    assert re.search(r"Risks\W+None", details_text)

    legacy = _document(name="visible").model_dump(mode="json")
    legacy["schema_version"] = 1
    legacy.pop("market_rankings")
    legacy_root = _html_tree(render_html(ReportDocument.model_validate(legacy)))

    assert "not_provided" in legacy_root.normalized_text()
    _require_one(legacy_root, ".ranking-warning")
    assert legacy_root.select(".ranking-profile") == []


def test_market_scan_unavailable_state_is_explicit_in_markdown_and_html():
    unavailable = report_models.MarketRankings(
        status="unavailable",
        unavailable_reason="scan_artifact_missing",
    )
    document = _document(name="visible", market_rankings=unavailable)

    markdown = render_markdown(document)
    html = render_html(document)

    for rendered in (markdown, html):
        assert "Full-market rankings unavailable" in rendered
        assert "scan_artifact_missing" in rendered
        assert "high confidence" not in rendered.lower()


@pytest.mark.parametrize(
    "partial_metadata",
    [
        {"scan_date": date(2026, 9, 4)},
        {"generated_at": datetime(2026, 9, 4, 8, 30, tzinfo=UTC)},
        {"provider_names": ("fixture",)},
    ],
)
def test_market_scan_unavailable_state_rejects_partial_metadata(partial_metadata):
    with pytest.raises(
        ValueError,
        match="unavailable market rankings metadata must be absent or complete",
    ):
        report_models.MarketRankings(
            status="unavailable",
            unavailable_reason="scan_artifact_missing",
            **partial_metadata,
        )


def test_market_scan_renderers_safely_handle_partial_unavailable_metadata():
    unavailable = report_models.MarketRankings.model_construct(
        status="unavailable",
        unavailable_reason="scan_artifact_missing",
        scan_date=date(2026, 9, 4),
    )
    document = _document(name="visible").model_copy(
        update={"market_rankings": unavailable}
    )

    markdown = render_markdown(document)
    html = render_html(document)

    for rendered in (markdown, html):
        assert "Full-market rankings unavailable" in rendered
        assert "—" in rendered
    assert r"2026\-09\-04" in markdown
    assert "2026-09-04" in html


@pytest.mark.parametrize("profile", ["trend", "balanced"])
def test_market_scan_rankings_reject_duplicate_profile_codes(profile):
    document = _market_rankings().model_dump()
    duplicate = dict(document[profile][-1])
    duplicate["code"] = document[profile][0]["code"]
    document[profile] = (*document[profile][:-1], duplicate)

    with pytest.raises(
        ValueError,
        match=rf"{profile} ranking codes must be unique",
    ):
        report_models.MarketRankings.model_validate(document)


def test_market_scan_rankings_reject_duplicate_consensus_codes():
    document = _market_rankings().model_dump()
    document["consensus"] = (
        *document["consensus"],
        dict(document["consensus"][0]),
    )

    with pytest.raises(ValueError, match="consensus ranking codes must be unique"):
        report_models.MarketRankings.model_validate(document)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("name", "Wrong Name", "consensus metadata must match rankings"),
        ("provider_name", "wrong-provider", "consensus metadata must match rankings"),
        (
            "latest_trade_date",
            date(2026, 9, 3),
            "consensus metadata must match rankings",
        ),
        ("trend_rank", 2, "consensus ranks and scores must match rankings"),
        ("trend_score", 81.0, "consensus ranks and scores must match rankings"),
        ("balanced_rank", 1, "consensus ranks and scores must match rankings"),
        ("balanced_score", 74.0, "consensus ranks and scores must match rankings"),
        (
            "trend_components",
            {
                "trend": 89.0,
                "momentum": 80.0,
                "volume": 70.0,
                "structure": 60.0,
                "risk": 50.0,
            },
            "consensus components must match rankings",
        ),
        (
            "balanced_components",
            {
                "trend": 90.0,
                "momentum": 79.0,
                "volume": 70.0,
                "structure": 60.0,
                "risk": 50.0,
            },
            "consensus components must match rankings",
        ),
    ],
)
def test_market_scan_rankings_reject_wrong_consensus_metadata(
    field,
    value,
    message,
):
    document = _market_rankings().model_dump()
    document["consensus"][0][field] = value

    with pytest.raises(ValueError, match=message):
        report_models.MarketRankings.model_validate(document)


@pytest.mark.parametrize(
    ("field", "trend_codes", "balanced_codes", "wrong_union"),
    [
        (
            "evidence_codes",
            ("trend_only", "shared"),
            ("balanced_only", "shared"),
            ("shared",),
        ),
        (
            "risk_codes",
            ("trend_risk", "shared_risk"),
            ("balanced_risk", "shared_risk"),
            ("shared_risk",),
        ),
    ],
)
def test_market_scan_rankings_reject_wrong_consensus_code_union(
    field,
    trend_codes,
    balanced_codes,
    wrong_union,
):
    document = _market_rankings().model_dump()
    document["trend"][0][field] = trend_codes
    document["balanced"][1][field] = balanced_codes
    document["consensus"][0][field] = wrong_union

    with pytest.raises(
        ValueError,
        match="consensus evidence and risk codes must equal ranking unions",
    ):
        report_models.MarketRankings.model_validate(document)


def test_market_scan_schema_accepts_legacy_report_without_rankings():
    legacy = _document(name="visible").model_dump(mode="json")
    legacy["schema_version"] = 1
    legacy.pop("market_rankings")

    document = ReportDocument.model_validate(legacy)

    assert document.schema_version == 1
    assert document.market_rankings.status == "unavailable"
    assert document.market_rankings.unavailable_reason == "not_provided"


def test_market_scan_schema_v2_requires_explicit_rankings_state():
    incomplete = _document(name="visible").model_dump(mode="json")
    incomplete.pop("market_rankings")

    with pytest.raises(ValueError, match="market_rankings"):
        ReportDocument.model_validate(incomplete)
