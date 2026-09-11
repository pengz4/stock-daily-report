import json
from datetime import UTC, date, datetime

import pytest

from stock_daily_report.market_scan.models import (
    ConsensusRecord,
    MarketScanArtifact,
    ProfileRankings,
    RankingRecord,
    ScanStatus,
)
from stock_daily_report.market_scan.report import (
    MarketScanConflictError,
    load_scan_artifact,
    write_scan_artifact,
)


def _ranking(profile: str, score: float) -> RankingRecord:
    return RankingRecord(
        code="600519",
        name="贵州茅台",
        profile=profile,
        rank=1,
        score=score,
        components={
            "trend": 90.0,
            "momentum": 80.0,
            "volume": 70.0,
            "structure": 60.0,
            "risk": 50.0,
        },
        evidence_codes=("close_above_ma20",),
        risk_codes=(),
        latest_trade_date=date(2026, 9, 11),
        provider_name="fixture",
    )


def _artifact(
    *,
    generated_at: datetime,
    input_hash: str = "1" * 64,
) -> MarketScanArtifact:
    trend = _ranking("trend", 82.0)
    balanced = _ranking("balanced", 75.0)
    return MarketScanArtifact(
        schema_version=1,
        rule_version="market-scan-v1",
        report_date=date(2026, 9, 11),
        generated_at=generated_at,
        universe_count=1,
        eligible_count=1,
        valid_count=1,
        coverage=1.0,
        exclusion_counts={},
        failure_counts={},
        rankings=ProfileRankings(trend=(trend,), balanced=(balanced,)),
        consensus=(
            ConsensusRecord(
                code="600519",
                name="贵州茅台",
                trend_rank=1,
                balanced_rank=1,
                trend_score=82.0,
                balanced_score=75.0,
                latest_trade_date=date(2026, 9, 11),
                provider_name="fixture",
            ),
        ),
        statuses=(
            ScanStatus(
                code="600519",
                name="贵州茅台",
                status="valid",
                reason_codes=(),
                provider_name="fixture",
            ),
        ),
        config_hash="0" * 64,
        input_hash=input_hash,
        provider_names=("fake-universe", "fixture"),
    )


def test_scan_artifact_uses_versioned_date_path_and_round_trips(tmp_path):
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))

    path = write_scan_artifact(tmp_path, artifact)
    loaded = load_scan_artifact(path)
    document = json.loads(path.read_text(encoding="utf-8"))

    assert path == tmp_path / "market-scans" / "2026-09-11" / "scan.json"
    assert loaded == artifact
    assert document["schema_version"] == 1
    assert document["universe_count"] == 1
    assert document["rankings"]["trend"][0]["code"] == "600519"


def test_same_date_identical_artifact_is_reused_without_changing_bytes(tmp_path):
    first = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    rerun = _artifact(generated_at=datetime(2026, 9, 11, 9, 0, tzinfo=UTC))

    path = write_scan_artifact(tmp_path, first)
    original_bytes = path.read_bytes()
    reused_path = write_scan_artifact(tmp_path, rerun)

    assert reused_path == path
    assert path.read_bytes() == original_bytes
    assert load_scan_artifact(path).generated_at == first.generated_at


def test_same_date_different_artifact_raises_explicit_conflict(tmp_path):
    first = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    changed = _artifact(
        generated_at=datetime(2026, 9, 11, 9, 0, tzinfo=UTC),
        input_hash="2" * 64,
    )
    write_scan_artifact(tmp_path, first)

    with pytest.raises(
        MarketScanConflictError,
        match="Refusing to overwrite immutable market scan",
    ):
        write_scan_artifact(tmp_path, changed)


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        (
            {"valid_count": 0, "coverage": 0.0},
            "valid_count must equal number of valid statuses",
        ),
        (
            {
                "eligible_count": 0,
                "valid_count": 0,
                "coverage": 0.0,
                "rankings": ProfileRankings(),
                "consensus": (),
                "failure_counts": {"network_error": 1},
                "statuses": (
                    ScanStatus(
                        code="600519",
                        name="贵州茅台",
                        status="history_failed",
                        reason_codes=("network_error",),
                        provider_name="fixture",
                    ),
                ),
            },
            "eligible_count must equal number of history-processed statuses",
        ),
        (
            {"exclusion_counts": {"insufficient_history": 1}},
            "exclusion_counts must match exclusion status reasons",
        ),
        (
            {"failure_counts": {"network_error": 1}},
            "failure_counts must match history_failed status reasons",
        ),
    ],
)
def test_scan_artifact_rejects_counts_inconsistent_with_statuses(updates, message):
    artifact = _artifact(generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC))
    document = artifact.model_dump()
    document.update(updates)

    with pytest.raises(ValueError, match=message):
        MarketScanArtifact.model_validate(document)
