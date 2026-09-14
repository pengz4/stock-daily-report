"""Immutable, JSON-serializable models for daily report artifacts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from stock_daily_report.decision import DecisionLabel

REPORT_SCHEMA_VERSION = 2


class AnalyzerMetadata(BaseModel):
    """Versions of analyzers that contributed to a report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    structural: str = Field(min_length=1)


class ReportMetadata(BaseModel):
    """Audit metadata shared by all report renderings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_date: date
    generated_at: datetime
    latest_source_timestamp: datetime
    snapshot_path: str = Field(min_length=1)
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_names: tuple[str, ...]
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    analyzer_versions: AnalyzerMetadata
    quality_status: Literal["passed"]
    stock_count: int = Field(ge=1)


class MarketSummary(BaseModel):
    """A deliberately narrow summary that cannot imply unavailable breadth."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["unavailable", "validated_watchlist"]
    text: str = Field(min_length=1)


class MarketRankingComponents(BaseModel):
    """Explainable bounded components copied from a validated market scan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trend: float = Field(ge=0.0, le=100.0)
    momentum: float = Field(ge=0.0, le=100.0)
    volume: float = Field(ge=0.0, le=100.0)
    structure: float = Field(ge=0.0, le=100.0)
    risk: float = Field(ge=0.0, le=100.0)


class MarketScanReasonCount(BaseModel):
    """One deterministic coverage exclusion or failure count."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=1)
    count: int = Field(ge=1)


class MarketRanking(BaseModel):
    """One row in a trend or balanced full-market ranking."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    profile: Literal["trend", "balanced"]
    rank: int = Field(ge=1, le=30)
    score: float = Field(ge=0.0, le=100.0)
    components: MarketRankingComponents
    evidence_codes: tuple[str, ...]
    risk_codes: tuple[str, ...]
    latest_trade_date: date
    provider_name: str = Field(min_length=1)


class MarketConsensusRanking(BaseModel):
    """One exact cross-profile intersection with both ranks and scores."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    trend_rank: int = Field(ge=1, le=30)
    trend_score: float = Field(ge=0.0, le=100.0)
    balanced_rank: int = Field(ge=1, le=30)
    balanced_score: float = Field(ge=0.0, le=100.0)
    trend_components: MarketRankingComponents
    balanced_components: MarketRankingComponents
    evidence_codes: tuple[str, ...]
    risk_codes: tuple[str, ...]
    latest_trade_date: date
    provider_name: str = Field(min_length=1)


MarketRankingsUnavailableReason = Literal[
    "scan_artifact_missing",
    "scan_artifact_invalid",
    "scan_date_mismatch",
    "scan_incomplete",
    "scan_rankings_unavailable",
    "not_provided",
]


class MarketRankings(BaseModel):
    """Full-market dual rankings or an explicit structured unavailable state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["available", "unavailable"]
    unavailable_reason: MarketRankingsUnavailableReason | None
    scan_date: date | None = None
    generated_at: datetime | None = None
    rule_version: str | None = None
    config_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    provider_names: tuple[str, ...] = ()
    universe_count: int | None = Field(default=None, ge=0)
    eligible_count: int | None = Field(default=None, ge=0)
    valid_count: int | None = Field(default=None, ge=0)
    coverage: float | None = Field(default=None, ge=0.0, le=1.0)
    exclusion_counts: tuple[MarketScanReasonCount, ...] = ()
    failure_counts: tuple[MarketScanReasonCount, ...] = ()
    trend: tuple[MarketRanking, ...] = ()
    balanced: tuple[MarketRanking, ...] = ()
    consensus: tuple[MarketConsensusRanking, ...] = ()

    @model_validator(mode="after")
    def validate_state(self) -> MarketRankings:
        required_metadata = (
            self.scan_date,
            self.generated_at,
            self.rule_version,
            self.config_hash,
            self.input_hash,
            self.universe_count,
            self.eligible_count,
            self.valid_count,
            self.coverage,
        )
        if self.status == "unavailable":
            if self.unavailable_reason is None:
                raise ValueError("unavailable market rankings require a reason")
            metadata_absent = all(value is None for value in required_metadata) and not (
                self.provider_names
                or self.exclusion_counts
                or self.failure_counts
            )
            metadata_complete = all(
                value is not None for value in required_metadata
            )
            if not metadata_absent and not metadata_complete:
                raise ValueError(
                    "unavailable market rankings metadata must be absent or complete"
                )
            return self
        if self.unavailable_reason is not None:
            raise ValueError("available market rankings cannot have unavailable reason")
        if any(value is None for value in required_metadata):
            raise ValueError("available market rankings require complete metadata")
        if not self.trend or not self.balanced:
            raise ValueError("available market rankings require both profiles")
        for profile, records in (
            ("trend", self.trend),
            ("balanced", self.balanced),
        ):
            if any(record.profile != profile for record in records):
                raise ValueError(f"{profile} rankings contain another profile")
            codes = tuple(record.code for record in records)
            if len(codes) != len(set(codes)):
                raise ValueError(f"{profile} ranking codes must be unique")
            if tuple(record.rank for record in records) != tuple(
                range(1, len(records) + 1)
            ):
                raise ValueError(f"{profile} ranks must be consecutive from one")
        trend = {record.code: record for record in self.trend}
        balanced = {record.code: record for record in self.balanced}
        consensus_codes = tuple(record.code for record in self.consensus)
        if len(consensus_codes) != len(set(consensus_codes)):
            raise ValueError("consensus ranking codes must be unique")
        if set(consensus_codes) != set(trend).intersection(balanced):
            raise ValueError("consensus must exactly match ranking intersection")
        for record in self.consensus:
            trend_record = trend[record.code]
            balanced_record = balanced[record.code]
            if (
                record.trend_rank != trend_record.rank
                or record.trend_score != trend_record.score
                or record.balanced_rank != balanced_record.rank
                or record.balanced_score != balanced_record.score
            ):
                raise ValueError("consensus ranks and scores must match rankings")
            if (
                record.name != trend_record.name
                or record.name != balanced_record.name
                or record.provider_name != trend_record.provider_name
                or record.provider_name != balanced_record.provider_name
                or record.latest_trade_date != trend_record.latest_trade_date
                or record.latest_trade_date != balanced_record.latest_trade_date
            ):
                raise ValueError("consensus metadata must match rankings")
            if (
                record.trend_components != trend_record.components
                or record.balanced_components != balanced_record.components
            ):
                raise ValueError("consensus components must match rankings")
            expected_evidence = tuple(
                sorted(
                    set(trend_record.evidence_codes).union(
                        balanced_record.evidence_codes
                    )
                )
            )
            expected_risks = tuple(
                sorted(
                    set(trend_record.risk_codes).union(
                        balanced_record.risk_codes
                    )
                )
            )
            if (
                record.evidence_codes != expected_evidence
                or record.risk_codes != expected_risks
            ):
                raise ValueError(
                    "consensus evidence and risk codes must equal ranking unions"
                )
        for counts in (self.exclusion_counts, self.failure_counts):
            codes = tuple(item.code for item in counts)
            if codes != tuple(sorted(set(codes))):
                raise ValueError("market scan reason counts must be sorted and unique")
        return self


def _default_market_rankings() -> MarketRankings:
    return MarketRankings(
        status="unavailable",
        unavailable_reason="not_provided",
    )


class ReportMetrics(BaseModel):
    """Typed technical values copied into the published report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    close: float | None = None
    ma20: float | None = None
    ma60: float | None = None
    return20: float | None = None
    return60: float | None = None
    realized_volatility20: float | None = None
    drawdown60: float | None = None
    volume_ratio20: float | None = None
    recent_high20: float | None = None
    recent_low20: float | None = None


class StructureLevel(BaseModel):
    """A typed support or resistance level for rendering."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["support", "resistance"]
    price: float
    source: str = Field(min_length=1)


class StructureSummary(BaseModel):
    """The simplified-v1 structural state and its auditable levels."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    state_label: str = Field(min_length=1)
    status: Literal["candidate", "confirmed"]
    rule_version: str = Field(min_length=1)
    levels: tuple[StructureLevel, ...]
    observations: tuple[str, ...]


class PoolOverviewRow(BaseModel):
    """One lightweight watchlist row in the pool overview table."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    group: str = Field(min_length=1)
    priority: Literal["core", "extended"]
    latest_price: float | None = None
    change_pct: float | None = None
    amount: float | None = None
    # Watchlist-scanned rank (1-based) and score, when the scan covered it.
    scan_rank: int | None = None
    scan_score: float | None = None


class PoolOverview(BaseModel):
    """Bulk-quote snapshot covering the full watchlist, deep analysis aside."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    quote_date: date
    rows: tuple[PoolOverviewRow, ...]
    unavailable_reason: str | None = None
    # Fingerprints of the watchlist scan that drove the deep-analysis selection
    # (when one existed); None in legacy reports or when the scan is missing.
    scan_date: date | None = None
    config_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    input_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class StockReport(BaseModel):
    """One validated watchlist security in a daily report."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    group: str = Field(min_length=1)
    provider_name: str = Field(min_length=1)
    latest_trade_date: date
    latest_source_timestamp: datetime
    bar_count: int = Field(ge=1)
    quality_status: Literal["passed"]
    quality_issues: tuple[str, ...]
    metrics: ReportMetrics
    structure: StructureSummary
    decision_label: DecisionLabel
    evidence: tuple[str, ...]
    risks: tuple[str, ...]
    key_levels: tuple[str, ...]
    next_conditions: tuple[str, ...]


class ReportDocument(BaseModel):
    """The canonical report document from which JSON, Markdown, and HTML flow."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    @model_validator(mode="before")
    @classmethod
    def supply_legacy_rankings_state(cls, value: object) -> object:
        if not isinstance(value, dict) or "market_rankings" in value:
            return value
        if value.get("schema_version") not in (None, 1):
            return value
        return {**value, "market_rankings": _default_market_rankings()}

    schema_version: Literal[1, REPORT_SCHEMA_VERSION] = REPORT_SCHEMA_VERSION
    metadata: ReportMetadata
    market_summary: MarketSummary
    market_rankings: MarketRankings
    stocks: tuple[StockReport, ...]
    pool_overview: PoolOverview | None = None


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "AnalyzerMetadata",
    "MarketConsensusRanking",
    "MarketRanking",
    "MarketRankingComponents",
    "MarketRankings",
    "MarketRankingsUnavailableReason",
    "MarketScanReasonCount",
    "MarketSummary",
    "PoolOverview",
    "PoolOverviewRow",
    "ReportDocument",
    "ReportMetadata",
    "ReportMetrics",
    "StockReport",
    "StructureLevel",
    "StructureSummary",
]
