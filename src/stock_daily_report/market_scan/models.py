"""Validated immutable models for full-market scan artifacts."""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, date, datetime
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    StrictInt,
    field_validator,
    model_validator,
)

SCAN_SCHEMA_VERSION = 2
Profile = Literal["trend", "balanced"]
ScanStatusValue = Literal[
    "universe_excluded",
    "not_processed",
    "history_excluded",
    "history_failed",
    "valid",
]


def _serialize_reason_counts(value: Mapping[str, int]) -> dict[str, int]:
    return dict(value)


ReasonCounts = Annotated[
    Mapping[str, int],
    PlainSerializer(_serialize_reason_counts, return_type=dict[str, int]),
]


class RankingComponents(BaseModel):
    """Bounded score components persisted for an explainable ranking row."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trend: float = Field(ge=0.0, le=100.0)
    momentum: float = Field(ge=0.0, le=100.0)
    volume: float = Field(ge=0.0, le=100.0)
    structure: float = Field(ge=0.0, le=100.0)
    risk: float = Field(ge=0.0, le=100.0)

    @field_validator("*", mode="before")
    @classmethod
    def require_finite_components(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("ranking components must be numeric")  # noqa: TRY004
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        if not math.isfinite(number):
            raise ValueError("ranking components must be finite")
        return value


class RankingRecord(BaseModel):
    """One ranked symbol with score evidence and provider provenance."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    profile: Profile
    rank: int = Field(ge=1, le=30)
    score: float = Field(ge=0.0, le=100.0)
    components: RankingComponents
    evidence_codes: tuple[str, ...]
    risk_codes: tuple[str, ...]
    latest_trade_date: date
    provider_name: str = Field(min_length=1)

    @field_validator("score", mode="before")
    @classmethod
    def require_finite_score(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("ranking score must be numeric")  # noqa: TRY004
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        if not math.isfinite(number):
            raise ValueError("ranking score must be finite")
        return value

    @field_validator("evidence_codes", "risk_codes")
    @classmethod
    def require_unique_codes(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not code.strip() for code in value):
            raise ValueError("evidence and risk codes must not be blank")
        if len(value) != len(set(value)):
            raise ValueError("evidence and risk codes must be unique")
        return value


class ProfileRankings(BaseModel):
    """The two independently limited ranking profiles."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    trend: tuple[RankingRecord, ...] = ()
    balanced: tuple[RankingRecord, ...] = ()

    @model_validator(mode="after")
    def validate_rankings(self) -> ProfileRankings:
        for profile, records in (
            ("trend", self.trend),
            ("balanced", self.balanced),
        ):
            if len(records) > 30:
                raise ValueError(f"{profile} ranking must contain at most 30 rows")
            if any(record.profile != profile for record in records):
                raise ValueError(f"{profile} ranking contains another profile")
            if tuple(record.rank for record in records) != tuple(
                range(1, len(records) + 1)
            ):
                raise ValueError(f"{profile} ranks must be consecutive from one")
            codes = tuple(record.code for record in records)
            if len(codes) != len(set(codes)):
                raise ValueError(f"{profile} ranking codes must be unique")
        return self


class ConsensusRecord(BaseModel):
    """A symbol present in both profile rankings."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    trend_rank: int = Field(ge=1, le=30)
    balanced_rank: int = Field(ge=1, le=30)
    trend_score: float = Field(ge=0.0, le=100.0)
    balanced_score: float = Field(ge=0.0, le=100.0)
    latest_trade_date: date
    provider_name: str = Field(min_length=1)

    @field_validator("trend_score", "balanced_score", mode="before")
    @classmethod
    def require_finite_scores(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("consensus scores must be numeric")  # noqa: TRY004
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        if not math.isfinite(number):
            raise ValueError("consensus scores must be finite")
        return value


class ScanStatus(BaseModel):
    """Deterministic per-code outcome retained for failure isolation audits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1)
    status: ScanStatusValue
    reason_codes: tuple[str, ...]
    provider_name: str | None = None
    provider_names: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_reasons(self) -> ScanStatus:
        if len(self.reason_codes) != len(set(self.reason_codes)):
            raise ValueError("status reason codes must be unique")
        if any(not reason.strip() for reason in self.reason_codes):
            raise ValueError("status reason codes must not be blank")
        if self.status == "valid" and self.reason_codes:
            raise ValueError("valid status must not include failure reasons")
        if self.status != "valid" and not self.reason_codes:
            raise ValueError("non-valid status must include at least one reason")
        if any(not name.strip() for name in self.provider_names):
            raise ValueError("status provider names must not be blank")
        if self.provider_names != tuple(sorted(set(self.provider_names))):
            raise ValueError("status provider names must be sorted and unique")
        return self


MarketStateStatus = Literal["available", "partial", "unavailable"]


class MarketIndexState(BaseModel):
    """One configured index's normalized market-state observation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    code: str = Field(pattern=r"^\d{6}$")
    status: MarketStateStatus
    latest_trade_date: date | None = None
    close: float | None = None
    change_pct: float | None = None
    close_vs_ma20: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("close_vs_ma20", "ma20_relation"),
    )
    close_vs_ma60: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("close_vs_ma60", "ma60_relation"),
    )
    trend: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("trend", "trend_label"),
    )
    provider: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("provider", "provider_name"),
    )
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)

    @field_validator("close", "change_pct", mode="before")
    @classmethod
    def require_finite_numbers(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool):
            raise ValueError("market index values must be numeric")  # noqa: TRY004
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        if not math.isfinite(number):
            raise ValueError("market index values must be finite")
        return value

    @model_validator(mode="after")
    def validate_state_fields(self) -> MarketIndexState:
        values = (
            self.latest_trade_date,
            self.close,
            self.change_pct,
            self.close_vs_ma20,
            self.close_vs_ma60,
            self.trend,
        )
        if self.status == "available" and any(value is None for value in values):
            raise ValueError(
                "available market index state requires complete market data"
            )
        if (
            self.status == "available"
            and self.close is not None
            and self.close <= 0
        ):
            raise ValueError(
                "available market index close must be strictly positive"
            )
        if self.status == "unavailable" and any(value is not None for value in values):
            raise ValueError(
                "unavailable market index state must not include market data"
            )
        if self.status == "unavailable" and not self.error_code:
            raise ValueError(
                "unavailable market index state requires an error_code"
            )
        return self

    @property
    def ma20_relation(self) -> str | None:
        return self.close_vs_ma20

    @property
    def ma60_relation(self) -> str | None:
        return self.close_vs_ma60

    @property
    def trend_label(self) -> str | None:
        return self.trend

    @property
    def provider_name(self) -> str | None:
        return self.provider


class MarketBreadth(BaseModel):
    """Normalized advancing/declining breadth for one quote snapshot."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MarketStateStatus
    advancing_count: StrictInt | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("advancing_count", "up_count"),
    )
    declining_count: StrictInt | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("declining_count", "down_count"),
    )
    unchanged_count: StrictInt | None = Field(
        default=None,
        ge=0,
        validation_alias=AliasChoices("unchanged_count", "flat_count"),
    )
    advancing_ratio: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("advancing_ratio", "up_ratio"),
    )
    declining_ratio: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        validation_alias=AliasChoices("declining_ratio", "down_ratio"),
    )
    advance_decline_ratio: float | None = Field(default=None, ge=0.0)
    limit_up_count: StrictInt | None = Field(default=None, ge=0)
    limit_down_count: StrictInt | None = Field(default=None, ge=0)
    valid_count: StrictInt | None = Field(default=None, ge=0)
    total_count: StrictInt | None = Field(default=None, ge=0)
    provider: str | None = Field(
        default=None,
        min_length=1,
        validation_alias=AliasChoices("provider", "provider_name"),
    )
    error_code: str | None = Field(default=None, min_length=1)
    error_message: str | None = Field(default=None, min_length=1)

    @field_validator(
        "advancing_ratio",
        "declining_ratio",
        "advance_decline_ratio",
        mode="before",
    )
    @classmethod
    def require_finite_ratios(cls, value: object) -> object:
        if value is None:
            return value
        if isinstance(value, bool):
            raise ValueError("market breadth values must be numeric")  # noqa: TRY004
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        if not math.isfinite(number):
            raise ValueError("market breadth values must be finite")
        return value

    @model_validator(mode="after")
    def validate_counts(self) -> MarketBreadth:
        if (
            self.valid_count is not None
            and self.total_count is not None
            and self.valid_count > self.total_count
        ):
            raise ValueError("valid_count must not exceed total_count")
        counts = (
            self.advancing_count,
            self.declining_count,
            self.unchanged_count,
            self.advancing_ratio,
            self.declining_ratio,
            self.advance_decline_ratio,
            self.valid_count,
            self.total_count,
        )
        if self.status == "available" and any(value is None for value in counts):
            raise ValueError("available market breadth requires complete data")
        if self.status == "unavailable" and any(value is not None for value in counts):
            raise ValueError(
                "unavailable market breadth must not include breadth data"
            )
        if self.status == "unavailable" and not self.error_code:
            raise ValueError("unavailable market breadth requires an error_code")
        return self

    @property
    def up_count(self) -> int | None:
        return self.advancing_count

    @property
    def down_count(self) -> int | None:
        return self.declining_count

    @property
    def flat_count(self) -> int | None:
        return self.unchanged_count

    @property
    def up_ratio(self) -> float | None:
        return self.advancing_ratio

    @property
    def down_ratio(self) -> float | None:
        return self.declining_ratio

    @property
    def provider_name(self) -> str | None:
        return self.provider


class MarketState(BaseModel):
    """Immutable, auditable index and breadth state for one report date."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    report_date: date
    generated_at: datetime
    rule_version: Literal["market-state-v1"]
    indices: tuple[MarketIndexState, ...] = Field(
        validation_alias=AliasChoices("indices", "index_states")
    )
    breadth: MarketBreadth
    status: MarketStateStatus
    conclusion: str = Field(min_length=1)

    @field_validator("generated_at")
    @classmethod
    def require_aware_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(UTC)

    @model_validator(mode="after")
    def validate_report_date(self) -> MarketState:
        if not self.indices:
            raise ValueError("market state indices must not be empty")
        if any(
            index.latest_trade_date is not None
            and index.latest_trade_date > self.report_date
            for index in self.indices
        ):
            raise ValueError(
                "market index latest_trade_date must not exceed report_date"
            )
        component_statuses = [index.status for index in self.indices]
        component_statuses.append(self.breadth.status)
        if all(status == "available" for status in component_statuses):
            expected_status = "available"
        elif all(status == "unavailable" for status in component_statuses):
            expected_status = "unavailable"
        else:
            expected_status = "partial"
        if self.status != expected_status:
            raise ValueError(
                "market state status must match index and breadth statuses"
            )
        return self

    @property
    def index_states(self) -> tuple[MarketIndexState, ...]:
        return self.indices


class MarketScanArtifact(BaseModel):
    """Complete schema-versioned output of one full-market scan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = SCAN_SCHEMA_VERSION
    rule_version: Literal["market-scan-v1"]
    report_date: date
    generated_at: datetime
    universe_count: int = Field(ge=0)
    eligible_count: int = Field(ge=0)
    valid_count: int = Field(ge=0)
    coverage: float = Field(ge=0.0, le=1.0)
    exclusion_counts: ReasonCounts
    failure_counts: ReasonCounts
    rankings: ProfileRankings
    consensus: tuple[ConsensusRecord, ...]
    statuses: tuple[ScanStatus, ...]
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    input_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    provider_names: tuple[str, ...]
    market_state: MarketState | None = None

    @field_validator("generated_at")
    @classmethod
    def require_aware_generated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("generated_at must be timezone-aware")
        return value.astimezone(UTC)

    @field_validator("coverage", mode="before")
    @classmethod
    def require_finite_coverage(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("coverage must be numeric")  # noqa: TRY004
        try:
            number = float(value)
        except (TypeError, ValueError):
            return value
        if not math.isfinite(number):
            raise ValueError("coverage must be finite")
        return value

    @field_validator("exclusion_counts", "failure_counts")
    @classmethod
    def validate_reason_counts(cls, value: Mapping[str, int]) -> Mapping[str, int]:
        if any(not reason.strip() for reason in value):
            raise ValueError("reason count keys must not be blank")
        if any(isinstance(count, bool) or count < 1 for count in value.values()):
            raise ValueError("reason counts must be positive integers")
        return MappingProxyType(dict(sorted(value.items())))

    @field_validator("provider_names")
    @classmethod
    def validate_provider_names(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if not value:
            raise ValueError(
                "provider_names must include all referenced providers"
            )
        if any(not name.strip() for name in value):
            raise ValueError("provider names must not be blank")
        if value != tuple(sorted(set(value))):
            raise ValueError("provider names must be sorted and unique")
        return value

    @model_validator(mode="after")
    def validate_artifact_consistency(self) -> MarketScanArtifact:
        if (
            self.market_state is not None
            and self.market_state.report_date != self.report_date
        ):
            raise ValueError(
                "market_state report_date must match artifact report_date"
            )
        if not self.valid_count <= self.eligible_count <= self.universe_count:
            raise ValueError("scan counts must satisfy valid <= eligible <= universe")
        expected_coverage = (
            self.valid_count / self.eligible_count if self.eligible_count else 0.0
        )
        if not math.isclose(self.coverage, expected_coverage, abs_tol=1e-12):
            raise ValueError("coverage must equal valid_count / eligible_count")
        status_codes = tuple(status.code for status in self.statuses)
        if len(status_codes) != self.universe_count:
            raise ValueError("statuses must contain one row per universe symbol")
        if status_codes != tuple(sorted(set(status_codes))):
            raise ValueError("statuses must be sorted by unique code")
        expected_valid_count = sum(
            status.status == "valid" for status in self.statuses
        )
        if self.valid_count != expected_valid_count:
            raise ValueError("valid_count must equal number of valid statuses")
        expected_eligible_count = sum(
            status.status != "universe_excluded" for status in self.statuses
        )
        if self.eligible_count != expected_eligible_count:
            raise ValueError(
                "eligible_count must equal number of history-processed statuses"
            )
        expected_exclusion_counts = Counter(
            reason
            for status in self.statuses
            if status.status in {"universe_excluded", "history_excluded"}
            for reason in status.reason_codes
        )
        if self.exclusion_counts != dict(sorted(expected_exclusion_counts.items())):
            raise ValueError("exclusion_counts must match exclusion status reasons")
        expected_failure_counts = Counter(
            reason
            for status in self.statuses
            if status.status in {"not_processed", "history_failed"}
            for reason in status.reason_codes
        )
        if self.failure_counts != dict(sorted(expected_failure_counts.items())):
            raise ValueError(
                "failure_counts must match failed or unprocessed status reasons"
            )
        if any(
            status.status != "not_processed"
            and "candidate_limit_exceeded" in status.reason_codes
            for status in self.statuses
        ):
            raise ValueError(
                "candidate_limit_exceeded must use not_processed status"
            )
        if any(status.status == "not_processed" for status in self.statuses) and (
            self.rankings.trend or self.rankings.balanced or self.consensus
        ):
            raise ValueError(
                "rankings and consensus must be empty when any symbol is not_processed"
            )

        statuses = {status.code: status for status in self.statuses}
        trend = {record.code: record for record in self.rankings.trend}
        balanced = {record.code: record for record in self.rankings.balanced}
        for records in (self.rankings.trend, self.rankings.balanced):
            for record in records:
                status = statuses.get(record.code)
                if status is None or status.status != "valid":
                    raise ValueError(
                        "ranking code must reference a valid status"
                    )
                if record.latest_trade_date > self.report_date:
                    raise ValueError(
                        "ranking latest_trade_date must not exceed report_date"
                    )
                if (
                    record.name != status.name
                    or record.provider_name != status.provider_name
                ):
                    raise ValueError("ranking metadata must match valid status")
        for code in set(trend).intersection(balanced):
            trend_record = trend[code]
            balanced_record = balanced[code]
            if (
                trend_record.name != balanced_record.name
                or trend_record.provider_name != balanced_record.provider_name
                or trend_record.latest_trade_date
                != balanced_record.latest_trade_date
            ):
                raise ValueError("ranking metadata must match across profiles")

        expected_codes = set(trend).intersection(balanced)
        actual_codes = {record.code for record in self.consensus}
        if actual_codes != expected_codes or len(actual_codes) != len(self.consensus):
            raise ValueError("consensus must exactly match the ranking intersection")
        for record in self.consensus:
            trend_record = trend[record.code]
            balanced_record = balanced[record.code]
            if record.latest_trade_date > self.report_date:
                raise ValueError(
                    "consensus latest_trade_date must not exceed report_date"
                )
            if (
                record.trend_rank != trend_record.rank
                or record.balanced_rank != balanced_record.rank
                or record.trend_score != trend_record.score
                or record.balanced_score != balanced_record.score
            ):
                raise ValueError("consensus ranks and scores must match rankings")
            if (
                record.name != trend_record.name
                or record.provider_name != trend_record.provider_name
                or record.latest_trade_date != trend_record.latest_trade_date
            ):
                raise ValueError("consensus metadata must match rankings")
        referenced_providers = {
            status.provider_name
            for status in self.statuses
            if status.provider_name is not None
        }
        referenced_providers.update(
            provider_name
            for status in self.statuses
            for provider_name in status.provider_names
        )
        referenced_providers.update(
            record.provider_name
            for records in (self.rankings.trend, self.rankings.balanced)
            for record in records
        )
        referenced_providers.update(
            record.provider_name for record in self.consensus
        )
        if self.market_state is not None:
            referenced_providers.update(
                index.provider
                for index in self.market_state.indices
                if index.provider is not None
            )
            if self.market_state.breadth.provider is not None:
                referenced_providers.add(self.market_state.breadth.provider)
        if not referenced_providers.issubset(self.provider_names):
            raise ValueError(
                "provider_names must include all referenced providers"
            )
        return self


__all__ = [
    "SCAN_SCHEMA_VERSION",
    "ConsensusRecord",
    "MarketBreadth",
    "MarketIndexState",
    "MarketScanArtifact",
    "MarketState",
    "MarketStateStatus",
    "ProfileRankings",
    "RankingComponents",
    "RankingRecord",
    "ScanStatus",
]
