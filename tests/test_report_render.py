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


def _market_rankings():
    components = {
        "trend": 90.0,
        "momentum": 80.0,
        "volume": 70.0,
        "structure": 60.0,
        "risk": 50.0,
    }
    trend = report_models.MarketRanking(
        code="600519",
        name="贵州茅台",
        profile="trend",
        rank=1,
        score=82.0,
        components=components,
        evidence_codes=("close_above_ma20",),
        risk_codes=("elevated_volatility",),
        latest_trade_date=date(2026, 9, 4),
        provider_name="fixture",
    )
    balanced_leader = trend.model_copy(
        update={
            "code": "000001",
            "name": "平安银行",
            "profile": "balanced",
            "rank": 1,
            "score": 76.0,
        }
    )
    balanced = trend.model_copy(
        update={"profile": "balanced", "rank": 2, "score": 75.0}
    )
    return report_models.MarketRankings(
        status="available",
        unavailable_reason=None,
        scan_date=date(2026, 9, 4),
        generated_at=datetime(2026, 9, 4, 8, 30, tzinfo=UTC),
        rule_version="market-scan-v1",
        config_hash="c" * 64,
        input_hash="d" * 64,
        provider_names=("fixture", "fake-universe"),
        universe_count=100,
        eligible_count=80,
        valid_count=72,
        coverage=0.9,
        exclusion_counts=(report_models.MarketScanReasonCount(code="st", count=20),),
        failure_counts=(
            report_models.MarketScanReasonCount(code="network_error", count=8),
        ),
        trend=(trend,),
        balanced=(balanced_leader, balanced),
        consensus=(
            report_models.MarketConsensusRanking(
                code="600519",
                name="贵州茅台",
                trend_rank=1,
                trend_score=82.0,
                balanced_rank=2,
                balanced_score=75.0,
                trend_components=components,
                balanced_components=components,
                evidence_codes=("close_above_ma20",),
                risk_codes=("elevated_volatility",),
                latest_trade_date=date(2026, 9, 4),
                provider_name="fixture",
            ),
        ),
    )


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
    duplicate["rank"] = len(document[profile]) + 1
    document[profile] = (*document[profile], duplicate)
    if profile == "trend":
        document["consensus"][0]["trend_rank"] = duplicate["rank"]
    else:
        document["consensus"][0]["balanced_rank"] = duplicate["rank"]

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
