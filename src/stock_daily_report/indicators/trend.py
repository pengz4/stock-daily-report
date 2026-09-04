"""Evidence-based, conservative trend labels derived from technical metrics."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from stock_daily_report.indicators.technical import (
    TechnicalMetrics,
    calculate_technical_metrics,
)
from stock_daily_report.models import DailyBar

TrendLabel = Literal["偏强", "观察", "偏弱", "风险升高", "等待确认"]

MINIMUM_TREND_BARS = 61
HIGH_REALIZED_VOLATILITY20 = 0.45
MAXIMUM_DRAWDOWN60 = -0.20
MINIMUM_CONSTRUCTIVE_RSI14 = 50.0


@dataclass(frozen=True)
class TrendEvidence:
    """One satisfied or blocking named trend predicate."""

    code: str
    detail: str


@dataclass(frozen=True)
class TrendClassification:
    """A product label with predicate evidence, never a generic score."""

    label: TrendLabel
    evidence: tuple[TrendEvidence, ...]

    @property
    def evidence_codes(self) -> tuple[str, ...]:
        """Stable shorthand codes for consumers that do not render details."""

        return tuple(item.code for item in self.evidence)


def classify_trend(bars: Sequence[DailyBar]) -> TrendClassification:
    """Classify the final bar using only the supplied trailing price history."""

    metrics = calculate_technical_metrics(bars)
    if not _has_required_history(metrics):
        return TrendClassification(
            label="等待确认",
            evidence=(
                TrendEvidence(
                    "insufficient_trend_history",
                    f"requires at least {MINIMUM_TREND_BARS} chronological bars "
                    "for MA60, 60-day return, volatility, and drawdown",
                ),
            ),
        )

    bullish_evidence = _bullish_evidence(metrics)
    if bullish_evidence is not None:
        risk_evidence = _risk_evidence(metrics)
        if risk_evidence:
            return TrendClassification(
                label="风险升高",
                evidence=(*bullish_evidence, *risk_evidence),
            )
        return TrendClassification(label="偏强", evidence=bullish_evidence)

    weak_evidence = _weak_evidence(metrics)
    if weak_evidence is not None:
        return TrendClassification(label="偏弱", evidence=weak_evidence)

    return TrendClassification(
        label="观察",
        evidence=(
            TrendEvidence(
                "mixed_or_neutral_trend_signals",
                "moving-average, return, MACD, and RSI predicates do not agree",
            ),
        ),
    )


def _has_required_history(metrics: TechnicalMetrics) -> bool:
    return (
        metrics.bar_count >= MINIMUM_TREND_BARS
        and metrics.ma20 is not None
        and metrics.ma60 is not None
        and metrics.return20 is not None
        and metrics.return60 is not None
        and metrics.macd_line is not None
        and metrics.macd_signal is not None
        and metrics.rsi14 is not None
        and metrics.realized_volatility20 is not None
        and metrics.drawdown60 is not None
    )


def _bullish_evidence(metrics: TechnicalMetrics) -> tuple[TrendEvidence, ...] | None:
    assert metrics.ma20 is not None
    assert metrics.ma60 is not None
    assert metrics.return20 is not None
    assert metrics.return60 is not None
    assert metrics.macd_line is not None
    assert metrics.macd_signal is not None
    assert metrics.rsi14 is not None
    close = _required_latest_close(metrics)
    if not (
        close > metrics.ma20 > metrics.ma60
        and metrics.return20 > 0.0
        and metrics.return60 > 0.0
        and metrics.macd_line > metrics.macd_signal
        and metrics.rsi14 >= MINIMUM_CONSTRUCTIVE_RSI14
    ):
        return None
    return (
        TrendEvidence(
            "price_above_rising_moving_averages",
            f"close {close:.4f} > MA20 {metrics.ma20:.4f} > MA60 {metrics.ma60:.4f}",
        ),
        TrendEvidence(
            "positive_20_and_60_day_returns",
            f"return20={metrics.return20:.4%}, return60={metrics.return60:.4%}",
        ),
        TrendEvidence(
            "macd_line_above_signal",
            f"MACD line {metrics.macd_line:.4f} > signal {metrics.macd_signal:.4f}",
        ),
        TrendEvidence(
            "rsi_in_constructive_range",
            f"RSI14 {metrics.rsi14:.2f} >= {MINIMUM_CONSTRUCTIVE_RSI14:.0f}",
        ),
    )


def _weak_evidence(metrics: TechnicalMetrics) -> tuple[TrendEvidence, ...] | None:
    assert metrics.ma20 is not None
    assert metrics.ma60 is not None
    assert metrics.return20 is not None
    assert metrics.return60 is not None
    assert metrics.macd_line is not None
    assert metrics.macd_signal is not None
    close = _required_latest_close(metrics)
    if not (
        close < metrics.ma20 < metrics.ma60
        and metrics.return20 < 0.0
        and metrics.return60 < 0.0
        and metrics.macd_line < metrics.macd_signal
    ):
        return None
    return (
        TrendEvidence(
            "price_below_falling_moving_averages",
            f"close {close:.4f} < MA20 {metrics.ma20:.4f} < MA60 {metrics.ma60:.4f}",
        ),
        TrendEvidence(
            "negative_20_and_60_day_returns",
            f"return20={metrics.return20:.4%}, return60={metrics.return60:.4%}",
        ),
        TrendEvidence(
            "macd_line_below_signal",
            f"MACD line {metrics.macd_line:.4f} < signal {metrics.macd_signal:.4f}",
        ),
    )


def _risk_evidence(metrics: TechnicalMetrics) -> tuple[TrendEvidence, ...]:
    assert metrics.realized_volatility20 is not None
    assert metrics.drawdown60 is not None
    evidence: list[TrendEvidence] = []
    if metrics.realized_volatility20 >= HIGH_REALIZED_VOLATILITY20:
        evidence.append(
            TrendEvidence(
                "realized_volatility20_exceeds_risk_threshold",
                f"volatility20={metrics.realized_volatility20:.4%} >= "
                f"{HIGH_REALIZED_VOLATILITY20:.4%}",
            )
        )
    if metrics.drawdown60 <= MAXIMUM_DRAWDOWN60:
        evidence.append(
            TrendEvidence(
                "drawdown60_exceeds_risk_threshold",
                f"drawdown60={metrics.drawdown60:.4%} <= "
                f"{MAXIMUM_DRAWDOWN60:.4%}",
            )
        )
    return tuple(evidence)


def _required_latest_close(metrics: TechnicalMetrics) -> float:
    assert metrics.close is not None
    return metrics.close


__all__ = [
    "TrendClassification",
    "TrendEvidence",
    "TrendLabel",
    "classify_trend",
]
