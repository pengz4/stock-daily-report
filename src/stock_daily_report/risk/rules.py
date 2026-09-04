"""Risk predicates for the risk-first decision layer."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path

from stock_daily_report.chan.common import SimplifiedChanResult
from stock_daily_report.config import ConfigurationError, load_settings
from stock_daily_report.indicators.technical import TechnicalMetrics
from stock_daily_report.models import RiskRulesSettings, Settings
from stock_daily_report.quality.checks import DataQualityResult

_REQUIRED_DECISION_METRICS = (
    "close",
    "ma20",
    "ma60",
    "return20",
    "return60",
    "macd_line",
    "macd_signal",
    "rsi14",
    "realized_volatility20",
    "drawdown60",
    "volume_ratio20",
)
_PRICE_METRICS = (
    "close",
    "ma5",
    "ma10",
    "ma20",
    "ma60",
    "ma120",
    "recent_high20",
    "recent_low20",
)


@dataclass(frozen=True)
class RiskAssessment:
    """Stable result of evaluating every configured risk predicate."""

    risk_codes: tuple[str, ...]
    evidence: tuple[str, ...]
    hard_data_failure: bool
    insufficient_data: bool
    incomplete_structure: bool


def evaluate_risk_rules(
    metrics: TechnicalMetrics,
    structure: SimplifiedChanResult,
    quality: DataQualityResult,
    rules: RiskRulesSettings,
) -> RiskAssessment:
    """Evaluate configured rules without coercing missing values to zero."""

    risk_codes: list[str] = []
    evidence: list[str] = []
    hard_data_failure = False
    insufficient_data = False

    def add_risk(code: str) -> None:
        if code not in risk_codes:
            risk_codes.append(code)

    def add_evidence(code: str) -> None:
        if code not in evidence:
            evidence.append(code)

    issue_codes = quality.issue_codes
    if not quality.analysis_allowed or issue_codes:
        for issue_code in issue_codes or ("invalid_data_quality",):
            add_evidence(f"data_quality_{issue_code}")
            if issue_code == "stale_last_trade_date":
                add_risk("stale_data")
                hard_data_failure = True
            elif issue_code == "insufficient_history":
                add_risk("insufficient_history")
                insufficient_data = True
            else:
                add_risk("invalid_data_quality")
                hard_data_failure = True

    if quality.bar_count < rules.minimum_history_bars:
        add_risk("insufficient_history")
        add_evidence("insufficient_history")
        insufficient_data = True

    invalid_price_metrics = tuple(
        name
        for name in _PRICE_METRICS
        if _is_invalid_price(getattr(metrics, name))
    )
    if invalid_price_metrics:
        add_risk("invalid_data_quality")
        add_evidence("invalid_price_metric")
        hard_data_failure = True

    close = _positive_finite(metrics.close)
    ma20 = _positive_finite(metrics.ma20)
    if close is None or ma20 is None:
        add_risk("insufficient_metric_data")
        add_evidence("close_or_ma20_unavailable")
        insufficient_data = True

    missing_required_metrics = tuple(
        name
        for name in _REQUIRED_DECISION_METRICS
        if _finite(getattr(metrics, name)) is None
    )
    if missing_required_metrics:
        add_risk("insufficient_metric_data")
        add_evidence("required_metric_data_unavailable")
        insufficient_data = True

    volatility = _finite(metrics.realized_volatility20)
    if volatility is not None and volatility >= rules.high_realized_volatility20:
        add_risk("high_realized_volatility20")
        add_evidence("high_realized_volatility20")

    drawdown = _finite(metrics.drawdown60)
    if drawdown is not None and drawdown <= rules.large_drawdown60:
        add_risk("large_drawdown60")
        add_evidence("large_drawdown60")

    if close is not None and ma20 is not None and close > ma20:
        distance = close / ma20 - 1.0
        if math.isfinite(distance) and distance >= rules.overextension_ma20_distance:
            add_risk("overextended_from_ma20")
            add_evidence("overextended_from_ma20")

    volume_ratio = _finite(metrics.volume_ratio20)
    if volume_ratio is not None and volume_ratio <= rules.adverse_volume_ratio20:
        add_risk("adverse_volume_behavior")
        add_evidence("adverse_volume_behavior")
    elif volume_ratio is None:
        add_evidence("volume_ratio20_unavailable")

    incomplete_structure = (
        structure.state.status == "candidate"
        or structure.state.label == "not_enough_data"
    )
    if incomplete_structure:
        add_risk("incomplete_structure")
        add_evidence("incomplete_structure")

    return RiskAssessment(
        risk_codes=tuple(risk_codes),
        evidence=tuple(evidence),
        hard_data_failure=hard_data_failure,
        insufficient_data=insufficient_data,
        incomplete_structure=incomplete_structure,
    )


def resolve_risk_rules(settings: Settings | None = None) -> RiskRulesSettings:
    """Load the repository's configured rules or require them on supplied settings."""

    if settings is None:
        settings = load_settings(_default_settings_path())
    if settings.risk_rules is None:
        raise ConfigurationError("risk_rules configuration is required for decisions")
    return settings.risk_rules


def configuration_hash(rules: RiskRulesSettings) -> str:
    """Return a canonical SHA-256 hash of the active risk-rule configuration."""

    canonical = json.dumps(
        rules.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _default_settings_path() -> Path:
    return Path(__file__).resolve().parents[3] / "config" / "settings.yaml"


def _finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value


def _positive_finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value) or value <= 0:
        return None
    return value


def _is_invalid_price(value: float | None) -> bool:
    return value is not None and _positive_finite(value) is None


__all__ = [
    "RiskAssessment",
    "configuration_hash",
    "evaluate_risk_rules",
    "resolve_risk_rules",
]
