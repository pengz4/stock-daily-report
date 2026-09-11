"""Deterministic ``market-scan-v1`` opportunity scoring.

Scores describe how closely trailing evidence matches two fixed research
profiles. They are ranking inputs, not forecasts or trading instructions.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer
from stock_daily_report.indicators.technical import (
    TechnicalMetrics,
    calculate_technical_metrics,
)
from stock_daily_report.models import DailyBar

RULE_VERSION = "market-scan-v1"
Profile = Literal["trend", "balanced"]

TREND_WEIGHTS = {
    "trend": 0.45,
    "momentum": 0.30,
    "volume": 0.15,
    "risk": 0.10,
}
BALANCED_WEIGHTS = {
    "trend": 0.30,
    "momentum": 0.20,
    "volume": 0.15,
    "structure": 0.20,
    "risk": 0.15,
}


@dataclass(frozen=True)
class ScoreComponents:
    """Named, bounded component scores shared by both profiles."""

    trend: float
    momentum: float
    volume: float
    structure: float
    risk: float


@dataclass(frozen=True)
class ProfileScore:
    """One fixed-profile score with its descriptive evidence."""

    code: str
    profile: Profile
    rule_version: str
    as_of: date | None
    total: float
    components: ScoreComponents
    evidence_codes: tuple[str, ...]
    risk_codes: tuple[str, ...]


@dataclass(frozen=True)
class CandidateScores:
    """Trend and balanced scores calculated from the same trailing prefix."""

    trend: ProfileScore
    balanced: ProfileScore


@dataclass(frozen=True)
class _ComponentResult:
    score: float
    evidence_codes: tuple[str, ...] = ()
    risk_codes: tuple[str, ...] = ()


def score_candidate(
    code: str,
    bars: Sequence[DailyBar],
    *,
    as_of: date | None = None,
) -> CandidateScores:
    """Score one code using only bars at or before ``as_of``."""

    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty string")
    selected = tuple(
        bar for bar in bars if as_of is None or bar.trade_date <= as_of
    )
    metrics = calculate_technical_metrics(selected)
    trend = _score_trend(metrics)
    momentum = _score_momentum(metrics)
    volume = _score_volume(metrics)
    structure = _score_structure(selected)
    risk = _score_risk(metrics)
    components = ScoreComponents(
        trend=_bounded(trend.score),
        momentum=_bounded(momentum.score),
        volume=_bounded(volume.score),
        structure=_bounded(structure.score),
        risk=_bounded(risk.score),
    )
    risk_codes = _stable_codes(momentum.risk_codes, risk.risk_codes)
    risk_evidence = () if risk_codes else risk.evidence_codes
    common_evidence = _stable_codes(
        trend.evidence_codes,
        momentum.evidence_codes,
        volume.evidence_codes,
        risk_evidence,
    )
    score_date = metrics.as_of
    return CandidateScores(
        trend=_profile_score(
            code,
            "trend",
            score_date,
            components,
            common_evidence,
            risk_codes,
        ),
        balanced=_profile_score(
            code,
            "balanced",
            score_date,
            components,
            _stable_codes(common_evidence, structure.evidence_codes),
            risk_codes,
        ),
    )


def rank_scores(scores: Sequence[ProfileScore]) -> tuple[ProfileScore, ...]:
    """Return a stable score-descending, code-ascending ranking."""

    return tuple(sorted(scores, key=lambda score: (-score.total, score.code)))


def _profile_score(
    code: str,
    profile: Profile,
    as_of: date | None,
    components: ScoreComponents,
    evidence_codes: tuple[str, ...],
    risk_codes: tuple[str, ...],
) -> ProfileScore:
    weights = TREND_WEIGHTS if profile == "trend" else BALANCED_WEIGHTS
    total = math.fsum(
        getattr(components, component) * weight
        for component, weight in weights.items()
    )
    return ProfileScore(
        code=code,
        profile=profile,
        rule_version=RULE_VERSION,
        as_of=as_of,
        total=_bounded(total),
        components=components,
        evidence_codes=evidence_codes,
        risk_codes=risk_codes,
    )


def _score_trend(metrics: TechnicalMetrics) -> _ComponentResult:
    alignment = 0.0
    evidence: list[str] = []
    if _ordered(metrics.close, metrics.ma20):
        alignment += 35.0
        evidence.append("close_above_ma20")
    if _ordered(metrics.ma20, metrics.ma60):
        alignment += 35.0
        evidence.append("ma20_above_ma60")
    if _ordered(metrics.ma60, metrics.ma120):
        alignment += 15.0
        evidence.append("ma60_above_ma120")
    if _ordered(metrics.macd_line, metrics.macd_signal):
        alignment += 15.0
        evidence.append("macd_line_above_signal")

    slopes = [
        _scaled(metrics.ma20_slope5, -0.01, 0.02),
        _scaled(metrics.ma60_slope5, -0.005, 0.015),
    ]
    available_slopes = [value for value in slopes if value is not None]
    slope_score = (
        math.fsum(available_slopes) / len(available_slopes)
        if available_slopes
        else 0.0
    )
    if metrics.ma20_slope5 is not None and metrics.ma20_slope5 > 0.0:
        evidence.append("ma20_slope_positive")
    if metrics.ma60_slope5 is not None and metrics.ma60_slope5 > 0.0:
        evidence.append("ma60_slope_positive")
    return _ComponentResult(
        score=alignment * 0.65 + slope_score * 0.35,
        evidence_codes=tuple(evidence),
    )


def _score_momentum(metrics: TechnicalMetrics) -> _ComponentResult:
    return20 = _scaled(metrics.return20, -0.05, 0.15) or 0.0
    return60 = _scaled(metrics.return60, -0.10, 0.35) or 0.0
    macd = (
        100.0
        if metrics.macd_histogram is not None and metrics.macd_histogram > 0.0
        else 0.0
    )
    raw_score = return20 * 0.45 + return60 * 0.35 + macd * 0.20
    evidence: list[str] = []
    risks: list[str] = []
    if metrics.return20 is not None and metrics.return20 > 0.0:
        evidence.append("positive_20_day_momentum")
    if metrics.return60 is not None and metrics.return60 > 0.0:
        evidence.append("positive_60_day_momentum")
    if macd:
        evidence.append("positive_macd_histogram")

    extension = _ma20_extension(metrics)
    overheated = (
        (metrics.return20 is not None and metrics.return20 > 0.30)
        or (extension is not None and extension > 0.18)
    )
    if overheated:
        raw_score = min(raw_score, 45.0)
        evidence.append("momentum_capped_for_overheating")
        risks.append("overheated_short_term_momentum")
    return _ComponentResult(raw_score, tuple(evidence), tuple(risks))


def _score_volume(metrics: TechnicalMetrics) -> _ComponentResult:
    if metrics.volume_ratio20 is None:
        return _ComponentResult(50.0, ("volume_confirmation_unavailable",))
    score = 20.0 + 80.0 * (
        _scaled(metrics.volume_ratio20, 0.5, 2.0) or 0.0
    ) / 100.0
    evidence = ["trailing_volume_observed"]
    if metrics.volume_ratio20 >= 1.2:
        evidence.append("volume_above_20_day_average")
    if metrics.return20 is not None and metrics.return20 > 0.0:
        score += 10.0
        evidence.append("volume_with_positive_price_change")
    return _ComponentResult(score, tuple(evidence))


def _score_structure(bars: Sequence[DailyBar]) -> _ComponentResult:
    result = SimplifiedChanAnalyzer().analyze(list(bars))
    confirmed_fractals = sum(
        fractal.status == "confirmed" for fractal in result.fractals
    )
    score = min(20.0, confirmed_fractals * 4.0)
    evidence: list[str] = []
    if confirmed_fractals:
        evidence.append("confirmed_fractal_structure")

    confirmed_strokes = sum(
        stroke.status == "confirmed" for stroke in result.strokes
    )
    score += min(30.0, confirmed_strokes * 10.0)
    if confirmed_strokes:
        evidence.append("confirmed_stroke_structure")
    if result.central_areas:
        score += 35.0
        evidence.append("confirmed_central_structure")
    if result.state.status == "confirmed" and result.state.label == "confirmed_up":
        score += 15.0
        evidence.append("confirmed_upward_structure")
    if any(
        observation.status == "confirmed"
        and observation.code == "potential_central_breakout"
        for observation in result.observations
    ):
        score += 15.0
        evidence.append("confirmed_central_breakout_observation")
    return _ComponentResult(score, tuple(evidence))


def _score_risk(metrics: TechnicalMetrics) -> _ComponentResult:
    score = 100.0
    risks: list[str] = []
    volatility = metrics.realized_volatility20
    if volatility is not None and volatility > 0.25:
        score -= 40.0 * min((volatility - 0.25) / 0.50, 1.0)
        if volatility >= 0.45:
            risks.append("high_realized_volatility")

    drawdown = metrics.drawdown60
    if drawdown is not None and drawdown < -0.10:
        score -= 40.0 * min((-drawdown - 0.10) / 0.30, 1.0)
        if drawdown <= -0.20:
            risks.append("deep_trailing_drawdown")

    extension = _ma20_extension(metrics)
    if extension is not None and extension > 0.10:
        score -= 30.0 * min((extension - 0.10) / 0.20, 1.0)
        if extension > 0.15:
            risks.append("price_extended_above_ma20")
    if (
        metrics.return20 is not None
        and metrics.return20 > 0.30
        or extension is not None
        and extension > 0.18
    ):
        score -= 20.0
    evidence = (
        ("risk_measures_within_moderate_ranges",) if not risks else ()
    )
    return _ComponentResult(score, evidence, tuple(risks))


def _ma20_extension(metrics: TechnicalMetrics) -> float | None:
    if metrics.close is None or metrics.ma20 is None:
        return None
    try:
        extension = metrics.close / metrics.ma20 - 1.0
    except (OverflowError, ZeroDivisionError):
        return None
    return extension if math.isfinite(extension) else None


def _ordered(left: float | None, right: float | None) -> bool:
    return left is not None and right is not None and left > right


def _scaled(value: float | None, lower: float, upper: float) -> float | None:
    if value is None or not math.isfinite(value):
        return None
    if value <= lower:
        return 0.0
    if value >= upper:
        return 100.0
    return _bounded((value - lower) / (upper - lower) * 100.0)


def _bounded(value: float) -> float:
    if not math.isfinite(value):
        return 0.0
    return min(max(float(value), 0.0), 100.0)


def _stable_codes(*groups: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(code for group in groups for code in group))


__all__ = [
    "CandidateScores",
    "ProfileScore",
    "ScoreComponents",
    "rank_scores",
    "score_candidate",
]
