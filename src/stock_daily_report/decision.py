"""Validated, immutable decision outputs built from typed analysis results."""

from __future__ import annotations

import math
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stock_daily_report.chan.common import SimplifiedChanResult
from stock_daily_report.indicators.technical import TechnicalMetrics
from stock_daily_report.indicators.trend import TrendClassification
from stock_daily_report.models import Settings
from stock_daily_report.quality.checks import DataQualityResult
from stock_daily_report.risk.rules import (
    configuration_hash,
    evaluate_risk_rules,
    resolve_risk_rules,
)

DecisionLabel = Literal["偏强", "观察", "偏弱", "风险升高", "等待确认"]


class KeyPriceLevel(BaseModel):
    """A finite price level copied from a typed technical or structure input."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: Literal["support", "resistance", "reference"]
    price: float
    source: str = Field(min_length=1)

    @field_validator("price")
    @classmethod
    def require_finite_positive_price(cls, value: float) -> float:
        if not math.isfinite(value) or value <= 0:
            raise ValueError("key price levels must be finite and positive")
        return value


class Decision(BaseModel):
    """The complete risk-first decision contract for downstream consumers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: DecisionLabel
    evidence: tuple[str, ...]
    risk_codes: tuple[str, ...]
    key_levels: tuple[KeyPriceLevel, ...]
    next_conditions: tuple[str, ...]
    rule_version: str = Field(min_length=1)
    config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @property
    def key_price_levels(self) -> tuple[KeyPriceLevel, ...]:
        """Compatibility name for consumers that spell out price levels."""

        return self.key_levels


def decide(
    *,
    metrics: TechnicalMetrics,
    structure: SimplifiedChanResult,
    quality: DataQualityResult,
    trend: TrendClassification | None = None,
    settings: Settings | None = None,
) -> Decision:
    """Produce one deterministic label with evidence, risks, and next conditions."""

    rules = resolve_risk_rules(settings)
    assessment = evaluate_risk_rules(metrics, structure, quality, rules)
    evidence = _unique(_trend_evidence(metrics, structure, trend))
    evidence = _unique((*evidence, *assessment.evidence))
    risk_codes = assessment.risk_codes
    label = _label(
        metrics=metrics,
        structure=structure,
        trend=trend,
        risk_codes=risk_codes,
        hard_data_failure=assessment.hard_data_failure,
        insufficient_data=assessment.insufficient_data,
    )
    return Decision(
        label=label,
        evidence=evidence,
        risk_codes=risk_codes,
        key_levels=_key_levels(metrics, structure),
        next_conditions=_next_conditions(risk_codes),
        rule_version=rules.rule_version,
        config_hash=configuration_hash(rules),
    )


def _label(
    *,
    metrics: TechnicalMetrics,
    structure: SimplifiedChanResult,
    trend: TrendClassification | None,
    risk_codes: tuple[str, ...],
    hard_data_failure: bool,
    insufficient_data: bool,
) -> DecisionLabel:
    high_risk_codes = {
        "invalid_data_quality",
        "stale_data",
        "high_realized_volatility20",
        "overextended_from_ma20",
        "large_drawdown60",
        "adverse_volume_behavior",
    }
    if hard_data_failure or high_risk_codes.intersection(risk_codes):
        return "风险升高"
    if insufficient_data or "incomplete_structure" in risk_codes:
        return "等待确认"
    if trend is not None:
        return trend.label
    return _derived_label(metrics, structure)


def _derived_label(
    metrics: TechnicalMetrics, structure: SimplifiedChanResult
) -> DecisionLabel:
    state_label = structure.state.label
    if state_label in {"candidate_upward", "candidate_downward", "not_enough_data"}:
        return "等待确认"
    close, ma20, ma60 = _finite(metrics.close), _finite(metrics.ma20), _finite(metrics.ma60)
    return20, return60 = _finite(metrics.return20), _finite(metrics.return60)
    if (
        close is not None
        and ma20 is not None
        and ma60 is not None
        and return20 is not None
        and return60 is not None
    ):
        if close > ma20 > ma60 and return20 > 0 and return60 > 0:
            return "偏强"
        if close < ma20 < ma60 and return20 < 0 and return60 < 0:
            return "偏弱"
    if state_label == "confirmed_upward":
        return "偏强"
    if state_label == "confirmed_downward":
        return "偏弱"
    return "观察"


def _trend_evidence(
    metrics: TechnicalMetrics,
    structure: SimplifiedChanResult,
    trend: TrendClassification | None,
) -> tuple[str, ...]:
    if trend is not None:
        return trend.evidence_codes
    if structure.state.status == "confirmed":
        return (f"structure_{structure.state.label}",)
    if _finite(metrics.close) is None or _finite(metrics.ma20) is None:
        return ("insufficient_metric_data",)
    return ("technical_signals_require_confirmation",)


def _key_levels(
    metrics: TechnicalMetrics, structure: SimplifiedChanResult
) -> tuple[KeyPriceLevel, ...]:
    levels: list[KeyPriceLevel] = []
    for level in structure.support_resistance:
        if math.isfinite(level.price) and level.price > 0:
            levels.append(
                KeyPriceLevel(
                    kind=level.kind,
                    price=level.price,
                    source=level.source,
                )
            )
    ma20 = _finite(metrics.ma20)
    if ma20 is not None and ma20 > 0:
        levels.append(KeyPriceLevel(kind="reference", price=ma20, source="technical_ma20"))
    return tuple(levels)


def _next_conditions(risk_codes: tuple[str, ...]) -> tuple[str, ...]:
    conditions = {
        "invalid_data_quality": "补齐并重新验证有效的 OHLC 数据",
        "stale_data": "获取最新完成交易日数据并通过时效检查",
        "insufficient_history": "补足配置要求的历史数据长度",
        "insufficient_metric_data": "补齐 MA20、价格及相关技术指标输入",
        "high_realized_volatility20": "20日实现波动率回落至风险阈值以下",
        "overextended_from_ma20": "价格回到 MA20 附近并保持有效趋势",
        "large_drawdown60": "60日回撤收窄至风险阈值以内",
        "adverse_volume_behavior": "成交量比恢复至配置阈值以上并得到价格确认",
        "incomplete_structure": "结构由候选状态转为已确认状态",
    }
    selected = [conditions[code] for code in risk_codes if code in conditions]
    return tuple(selected or ("继续观察价格、技术指标与结构确认状态",))


def _unique(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(values))


def _finite(value: float | None) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    return value


__all__ = ["Decision", "DecisionLabel", "KeyPriceLevel", "decide"]
