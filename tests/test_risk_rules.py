from datetime import UTC, date, datetime, timedelta

import pytest
from pydantic import ValidationError

from stock_daily_report.chan.common import SimplifiedChanResult, StructureState
from stock_daily_report.indicators.technical import TechnicalMetrics
from stock_daily_report.models import DailyBar, RiskRulesSettings, Settings
from stock_daily_report.quality.checks import DataQualityIssue, DataQualityResult


def valid_quality() -> DataQualityResult:
    return DataQualityResult(
        code="600519",
        as_of=date(2026, 9, 4),
        issues=(),
        bar_count=80,
        analysis_allowed=True,
    )


def metrics(**overrides: float | None) -> TechnicalMetrics:
    values: dict[str, object] = {
        "as_of": date(2026, 9, 4),
        "bar_count": 80,
        "close": 120.0,
        "ma5": 111.0,
        "ma10": 109.0,
        "ma20": 100.0,
        "ma60": 90.0,
        "ma120": None,
        "macd_line": 2.0,
        "macd_signal": 1.0,
        "macd_histogram": 1.0,
        "rsi14": 60.0,
        "return20": 0.10,
        "return60": 0.20,
        "return120": None,
        "realized_volatility20": 0.20,
        "drawdown60": -0.05,
        "volume_ratio20": 1.2,
        "recent_high20": 125.0,
        "recent_low20": 95.0,
    }
    values.update(overrides)
    return TechnicalMetrics(**values)


def make_bars(closes: list[float]) -> list[DailyBar]:
    start = date(2026, 1, 1)
    return [
        DailyBar(
            trade_date=start + timedelta(days=index),
            open=close,
            high=close + 1.0,
            low=close - 1.0,
            close=close,
            volume=100.0,
            amount=close * 100.0,
            turnover_rate=0.1,
            adjustment_mode="qfq",
            provider_name="test",
            source_timestamp=datetime(2026, 1, 1, tzinfo=UTC)
            + timedelta(days=index),
        )
        for index, close in enumerate(closes)
    ]


def structure(*, status: str = "confirmed", label: str = "confirmed_upward"):
    return SimplifiedChanResult(
        processed_bars=(),
        fractals=(),
        strokes=(),
        central_candidates=(),
        central_areas=(),
        support_resistance=(),
        observations=(),
        state=StructureState(
            label=label,  # type: ignore[arg-type]
            formed_at=date(2026, 9, 1),
            confirmed_at=date(2026, 9, 3) if status == "confirmed" else None,
            tradable_at=date(2026, 9, 4) if status == "confirmed" else None,
            status=status,  # type: ignore[arg-type]
            rule_version="simplified-v1",
        ),
    )


def test_overextension_marks_risk_increased():
    from stock_daily_report.decision import decide

    decision = decide(
        metrics=metrics(close=125.0, ma20=100.0),
        structure=structure(),
        quality=valid_quality(),
    )

    assert decision.label == "风险升高"
    assert "overextended_from_ma20" in decision.risk_codes


@pytest.mark.parametrize(
    ("metric", "value", "code"),
    [
        ("realized_volatility20", 0.50, "high_realized_volatility20"),
        ("drawdown60", -0.25, "large_drawdown60"),
        ("volume_ratio20", 0.20, "adverse_volume_behavior"),
    ],
)
def test_each_named_market_risk_overrides_bullish_label(metric, value, code):
    from stock_daily_report.decision import decide

    decision = decide(
        metrics=metrics(**{metric: value}),
        structure=structure(),
        quality=valid_quality(),
    )

    assert decision.label == "风险升高"
    assert code in decision.risk_codes


def test_incomplete_structure_waits_for_confirmation():
    from stock_daily_report.decision import decide

    decision = decide(
        metrics=metrics(close=105.0, ma20=100.0),
        structure=structure(status="candidate", label="candidate_upward"),
        quality=valid_quality(),
    )

    assert decision.label == "等待确认"
    assert "incomplete_structure" in decision.risk_codes


@pytest.mark.parametrize(
    "issues",
    [
        (DataQualityIssue("missing_ohlc", "close is missing", 0),),
        (DataQualityIssue("stale_last_trade_date", "stale", None),),
        (DataQualityIssue("insufficient_history", "too short", None),),
    ],
)
def test_data_quality_failures_never_receive_bullish_label(issues):
    from stock_daily_report.decision import decide

    quality = DataQualityResult(
        code="600519",
        as_of=date(2026, 9, 4),
        issues=issues,
        bar_count=20,
        analysis_allowed=False,
    )

    decision = decide(
        metrics=metrics(),
        structure=structure(),
        quality=quality,
    )

    assert decision.label in {"风险升高", "等待确认"}
    assert decision.label != "偏强"
    assert decision.risk_codes


def test_missing_metrics_are_not_treated_as_zero_or_bullish():
    from stock_daily_report.decision import decide

    decision = decide(
        metrics=metrics(close=None, ma20=None, realized_volatility20=None),
        structure=structure(),
        quality=valid_quality(),
    )

    assert decision.label == "等待确认"
    assert "insufficient_metric_data" in decision.risk_codes
    assert all("nan" not in text.lower() for text in decision.evidence)


@pytest.mark.parametrize(
    "metric",
    [
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
    ],
)
def test_missing_any_required_decision_metric_never_receives_bullish_label(metric):
    from stock_daily_report.decision import decide

    decision = decide(
        metrics=metrics(**{metric: None}),
        structure=structure(),
        quality=valid_quality(),
    )

    assert decision.label == "等待确认"
    assert "insufficient_metric_data" in decision.risk_codes


def test_zero_volume_is_explicitly_adverse_and_outputs_are_deduplicated():
    from stock_daily_report.decision import decide

    decision = decide(
        metrics=metrics(volume_ratio20=0.0),
        structure=structure(),
        quality=valid_quality(),
    )

    assert decision.label == "风险升高"
    assert decision.risk_codes.count("adverse_volume_behavior") == 1
    assert len(decision.evidence) == len(set(decision.evidence))
    assert len(decision.risk_codes) == len(set(decision.risk_codes))


def test_decision_is_deterministic_immutable_and_json_serializable():
    from stock_daily_report.decision import decide

    first = decide(metrics=metrics(), structure=structure(), quality=valid_quality())
    second = decide(metrics=metrics(), structure=structure(), quality=valid_quality())

    assert first == second
    assert first.model_dump_json()
    with pytest.raises(ValidationError):
        first.label = "偏弱"  # type: ignore[misc]
    assert first.rule_version
    assert len(first.config_hash) == 64
    assert first.key_levels[0].source == "technical_ma20"


def test_custom_thresholds_change_behavior_and_configuration_hash():
    from stock_daily_report.decision import decide

    settings = Settings(
        rule_version={"name": "simplified", "version": "v1"},
        notifications={"enabled_channels": []},
        risk_rules=RiskRulesSettings(
            rule_version="risk-test-v1",
            high_realized_volatility20=0.80,
            overextension_ma20_distance=0.30,
            large_drawdown60=-0.40,
            adverse_volume_ratio20=0.20,
            minimum_history_bars=60,
        ),
    )

    decision = decide(
        metrics=metrics(close=120.0, ma20=100.0),
        structure=structure(),
        quality=valid_quality(),
        settings=settings,
    )

    assert decision.label == "偏强"
    assert decision.risk_codes == ()
    assert decision.rule_version == "risk-test-v1"
    assert decision.config_hash != decide(
        metrics=metrics(close=120.0, ma20=100.0),
        structure=structure(),
        quality=valid_quality(),
    ).config_hash


def test_custom_thresholds_filter_supplied_classifier_risk_evidence():
    from stock_daily_report.decision import decide
    from stock_daily_report.indicators.technical import calculate_technical_metrics
    from stock_daily_report.indicators.trend import classify_trend

    closes = [100.0, 130.0, 95.0] + [float(value) for value in range(96, 154)]
    bars = make_bars(closes)
    trend = classify_trend(bars)
    assert trend.label == "风险升高"

    settings = Settings(
        rule_version={"name": "simplified", "version": "v1"},
        notifications={"enabled_channels": []},
        risk_rules=RiskRulesSettings(
            rule_version="risk-test-v1",
            high_realized_volatility20=0.80,
            overextension_ma20_distance=0.30,
            large_drawdown60=-0.40,
            adverse_volume_ratio20=0.20,
            minimum_history_bars=60,
        ),
    )

    decision = decide(
        metrics=calculate_technical_metrics(bars),
        structure=structure(),
        quality=valid_quality(),
        trend=trend,
        settings=settings,
    )

    assert decision.label == "偏强"
    assert decision.risk_codes == ()
    assert "drawdown60_exceeds_risk_threshold" not in decision.evidence
    assert "price_above_rising_moving_averages" in decision.evidence


def test_configured_threshold_risks_have_matching_decision_evidence():
    from stock_daily_report.decision import decide
    from stock_daily_report.indicators.trend import TrendClassification, TrendEvidence

    settings = Settings(
        rule_version={"name": "simplified", "version": "v1"},
        notifications={"enabled_channels": []},
        risk_rules=RiskRulesSettings(
            rule_version="risk-test-v1",
            high_realized_volatility20=0.40,
            overextension_ma20_distance=0.30,
            large_drawdown60=-0.10,
            adverse_volume_ratio20=0.20,
            minimum_history_bars=60,
        ),
    )
    trend = TrendClassification(
        label="偏强",
        evidence=(
            TrendEvidence(
                "price_above_rising_moving_averages",
                "neutral trend context",
            ),
            TrendEvidence(
                "realized_volatility20_exceeds_risk_threshold",
                "classifier threshold detail",
            ),
            TrendEvidence(
                "drawdown60_exceeds_risk_threshold",
                "classifier threshold detail",
            ),
        ),
    )

    decision = decide(
        metrics=metrics(realized_volatility20=0.50, drawdown60=-0.15),
        structure=structure(),
        quality=valid_quality(),
        trend=trend,
        settings=settings,
    )

    assert decision.risk_codes == (
        "high_realized_volatility20",
        "large_drawdown60",
    )
    assert "high_realized_volatility20" in decision.evidence
    assert "large_drawdown60" in decision.evidence
    assert "realized_volatility20_exceeds_risk_threshold" not in decision.evidence
    assert "drawdown60_exceeds_risk_threshold" not in decision.evidence
    assert decision.evidence[0] == "price_above_rising_moving_averages"


@pytest.mark.parametrize(
    ("metric", "value"),
    [
        ("close", 0.0),
        ("ma20", 0.0),
        ("ma60", -1.0),
        ("recent_high20", 0.0),
        ("recent_low20", -1.0),
        ("close", float("nan")),
        ("ma20", float("inf")),
    ],
)
def test_invalid_price_metrics_return_data_quality_risk_instead_of_raising(
    metric, value
):
    from stock_daily_report.decision import decide

    decision = decide(
        metrics=metrics(**{metric: value}),
        structure=structure(),
        quality=valid_quality(),
    )

    assert decision.label == "风险升高"
    assert "invalid_data_quality" in decision.risk_codes


def test_supplied_settings_without_risk_rules_are_rejected():
    from stock_daily_report.decision import decide

    settings = Settings(
        rule_version={"name": "simplified", "version": "v1"},
        notifications={"enabled_channels": []},
    )

    with pytest.raises(ValueError, match="risk_rules configuration is required"):
        decide(
            metrics=metrics(),
            structure=structure(),
            quality=valid_quality(),
            settings=settings,
        )


def _valid_decision_kwargs() -> dict[str, object]:
    return {
        "label": "观察",
        "evidence": ("technical_signals_require_confirmation",),
        "risk_codes": (),
        "key_levels": (),
        "next_conditions": ("继续观察价格、技术指标与结构确认状态",),
        "rule_version": "risk-test-v1",
        "config_hash": "0" * 64,
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("risk_codes", ("duplicate", "duplicate")),
        ("evidence", ("duplicate", "duplicate")),
    ],
)
def test_decision_rejects_duplicate_codes(field, value):
    from stock_daily_report.decision import Decision

    with pytest.raises(ValidationError, match="duplicate"):
        Decision(**{**_valid_decision_kwargs(), field: value})


@pytest.mark.parametrize(
    "field",
    [
        "high_realized_volatility20",
        "overextension_ma20_distance",
        "large_drawdown60",
        "adverse_volume_ratio20",
        "minimum_history_bars",
    ],
)
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_risk_thresholds_reject_non_finite_numbers(field, value):
    with pytest.raises(ValidationError, match="finite"):
        RiskRulesSettings(
            rule_version="risk-test-v1",
            high_realized_volatility20=(
                value if field == "high_realized_volatility20" else 0.80
            ),
            overextension_ma20_distance=(
                value if field == "overextension_ma20_distance" else 0.30
            ),
            large_drawdown60=value if field == "large_drawdown60" else -0.40,
            adverse_volume_ratio20=(
                value if field == "adverse_volume_ratio20" else 0.20
            ),
            minimum_history_bars=value if field == "minimum_history_bars" else 60,
        )
