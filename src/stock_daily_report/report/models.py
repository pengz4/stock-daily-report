"""Immutable, JSON-serializable models for daily report artifacts."""

from __future__ import annotations

from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from stock_daily_report.decision import DecisionLabel

REPORT_SCHEMA_VERSION = 1


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

    schema_version: Literal[REPORT_SCHEMA_VERSION] = REPORT_SCHEMA_VERSION
    metadata: ReportMetadata
    market_summary: MarketSummary
    stocks: tuple[StockReport, ...]


__all__ = [
    "REPORT_SCHEMA_VERSION",
    "AnalyzerMetadata",
    "MarketSummary",
    "ReportDocument",
    "ReportMetadata",
    "ReportMetrics",
    "StockReport",
    "StructureLevel",
    "StructureSummary",
]
