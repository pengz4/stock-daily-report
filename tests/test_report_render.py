from datetime import UTC, date, datetime

import pytest

import stock_daily_report.report.models as report_models
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
            report_models.MarketScanReasonCount(code="st", count=20),
        ),
        "failure_counts": (
            report_models.MarketScanReasonCount(code="network_error", count=8),
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
