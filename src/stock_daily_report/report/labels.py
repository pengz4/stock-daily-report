"""Human-readable labels for report-facing technical analysis fields."""

from __future__ import annotations

from collections.abc import Mapping

METRIC_LABELS: Mapping[str, str] = {
    "close": "最新收盘价",
    "ma5": "5日均线",
    "ma10": "10日均线",
    "ma20": "20日均线",
    "ma60": "60日均线",
    "ma120": "120日均线",
    "return20": "20日收益率",
    "return60": "60日收益率",
    "return120": "120日收益率",
    "realized_volatility20": "20日年化波动率",
    "drawdown60": "60日最大回撤",
    "volume_ratio20": "20日成交量比",
    "recent_high20": "20日最高价",
    "recent_low20": "20日最低价",
}

COMPONENT_LABELS: Mapping[str, str] = {
    "trend": "趋势",
    "momentum": "动量",
    "volume": "成交量",
    "structure": "结构",
    "risk": "风险",
}

EVIDENCE_LABELS: Mapping[str, str] = {
    "close_above_ma20": "收盘价高于20日均线",
    "ma20_above_ma60": "20日均线高于60日均线",
    "ma60_above_ma120": "60日均线高于120日均线",
    "macd_line_above_signal": "MACD线上穿信号线",
    "macd_line_below_signal": "MACD线低于信号线",
    "ma20_slope_positive": "20日均线向上",
    "ma60_slope_positive": "60日均线向上",
    "positive_20_day_momentum": "20日动量为正",
    "positive_60_day_momentum": "60日动量为正",
    "positive_20_and_60_day_returns": "20日和60日收益率均为正",
    "negative_20_and_60_day_returns": "20日和60日收益率均为负",
    "positive_macd_histogram": "MACD柱体为正",
    "momentum_capped_for_overheating": "动量因过热被封顶",
    "volume_above_20_day_average": "成交量高于20日均量",
    "volume_with_positive_price_change": "放量且价格上涨",
    "trailing_volume_observed": "已观察到近期成交量",
    "volume_confirmation_unavailable": "成交量确认数据不可用",
    "risk_measures_within_moderate_ranges": "风险指标处于中等范围",
    "overheated_short_term_momentum": "短期动量过热",
    "high_realized_volatility": "实现波动率偏高",
    "deep_trailing_drawdown": "近期回撤较深",
    "price_extended_above_ma20": "价格明显高于20日均线",
    "confirmed_fractal_structure": "分型结构已确认",
    "confirmed_stroke_structure": "笔结构已确认",
    "confirmed_central_structure": "中枢结构已确认",
    "confirmed_upward_structure": "向上结构已确认",
    "confirmed_central_breakout_observation": "出现中枢突破观察",
    "price_above_rising_moving_averages": "价格位于上行均线之上",
    "price_below_falling_moving_averages": "价格位于下行均线之下",
    "rsi_in_constructive_range": "RSI处于建设性区间",
    "insufficient_trend_history": "趋势历史数据不足",
    "mixed_or_neutral_trend_signals": "趋势信号混合或中性",
    "insufficient_history": "历史数据不足",
    "invalid_price_metric": "价格指标无效",
    "close_or_ma20_unavailable": "收盘价或20日均线不可用",
    "required_metric_data_unavailable": "必要指标数据不可用",
    "high_realized_volatility20": "20日波动率偏高",
    "realized_volatility20_exceeds_risk_threshold": "20日波动率超过风险阈值",
    "large_drawdown60": "60日回撤较大",
    "drawdown60_exceeds_risk_threshold": "60日回撤超过风险阈值",
    "overextended_from_ma20": "相对20日均线明显乖离",
    "adverse_volume_behavior": "成交量行为不利",
    "volume_ratio20_unavailable": "20日成交量比不可用",
    "incomplete_structure": "结构尚未完成",
    "technical_signals_require_confirmation": "技术信号仍需确认",
}

RISK_LABELS: Mapping[str, str] = {
    "stale_data": "数据过期",
    "insufficient_history": "历史数据不足",
    "invalid_data_quality": "数据质量无效",
    "insufficient_metric_data": "指标数据不足",
    "high_realized_volatility20": "20日波动率偏高",
    "overextended_from_ma20": "相对20日均线明显乖离",
    "large_drawdown60": "60日回撤较大",
    "adverse_volume_behavior": "成交量行为不利",
    "incomplete_structure": "结构尚未完成",
    "overheated_short_term_momentum": "短期动量过热",
    "high_realized_volatility": "实现波动率偏高",
    "deep_trailing_drawdown": "近期回撤较深",
    "price_extended_above_ma20": "价格明显高于20日均线",
}

STRUCTURE_LABELS: Mapping[str, str] = {
    "not_enough_data": "数据不足",
    "candidate_upward": "向上候选",
    "candidate_downward": "向下候选",
    "confirmed_upward": "确认向上",
    "confirmed_downward": "确认向下",
    "neutral_consolidation": "中性盘整",
    "candidate": "候选",
    "confirmed": "已确认",
    "support": "支撑位",
    "resistance": "压力位",
    "reference": "参考位",
    "technical_ma20": "20日均线",
    "confirmed_central_range": "已确认中枢区间",
    "confirmed_bottom_fractal": "已确认底分型",
    "confirmed_top_fractal": "已确认顶分型",
    "potential_central_breakout": "潜在中枢突破",
    "swing_low": "摆动低点",
    "recent_high": "近期高点",
    "strict_alternating_fractals": "严格交替分型",
    "strict_three_stroke_overlap": "严格三笔重叠",
    "strict_two_stroke_overlap_candidate": "严格两笔重叠候选",
    "strict_three_stroke_feature_sequence": "严格三笔特征序列",
}

REASON_LABELS: Mapping[str, str] = {
    "halted": "停牌",
    "st": "风险标记股票",
    "network_error": "网络错误",
    "price_missing": "价格缺失",
    "fetch_timeout": "请求超时",
    "history_fetch_failed": "历史数据请求失败",
    "history_data_invalid": "历史数据格式无效",
    "scan_artifact_invalid": "扫描产物无效",
    "scan_date_mismatch": "扫描日期不匹配",
    "scan_incomplete": "扫描未完成",
    "scan_rankings_unavailable": "扫描排名不可用",
    "candidate_limit_exceeded": "超过候选数量上限",
    "special_treatment_name": "特殊处理股票名称",
    "delisting_risk_warning_name": "退市风险名称",
    "unsupported_security": "不支持的证券类型",
    "invalid_latest_quote": "最新行情无效",
    "suspended_quote": "最新行情显示停牌",
    "insufficient_latest_amount": "最新成交额不足",
    "stale_quote_date": "行情日期过旧",
    "future_quote_date": "行情日期晚于报告日期",
    "suspended_latest_bar": "最新K线显示停牌",
    "data_quality_rejected": "数据质量未通过",
    "scoring_failed": "评分失败",
    "scan_artifact_missing": "扫描产物缺失",
    "not_provided": "未提供",
    "invalid_trade_date": "交易日期无效",
    "missing_ohlc": "OHLC价格字段缺失",
    "negative_ohlc": "OHLC价格非正或无效",
    "high_lt_low": "最高价低于最低价",
    "close_outside_low_high": "收盘价超出最高最低价区间",
    "future_trade_date": "交易日期晚于报告日期",
    "insufficient_history": "历史数据不足",
    "stale_last_trade_date": "最新交易日过旧",
    "duplicate_trade_date": "交易日期重复",
    "gap_risk": "价格存在跳空风险",
    "manual_review": "需要人工复核",
}

STATUS_LABELS: Mapping[str, str] = {
    "passed": "通过",
    "available": "可用",
    "unavailable": "不可用",
    "candidate": "候选",
    "confirmed": "已确认",
    "valid": "有效",
    "not_processed": "未处理",
    "universe_excluded": "股票池排除",
    "history_excluded": "历史数据排除",
    "history_failed": "历史数据失败",
}


def label_for_code(
    code: str,
    labels: Mapping[str, str],
    *,
    unknown_prefix: str = "其他",
) -> str:
    """Return a readable label while preserving unknown codes for auditability."""

    return labels.get(code, f"{unknown_prefix}（{code}）")


def metric_label(name: str) -> str:
    return label_for_code(name, METRIC_LABELS, unknown_prefix="指标")


def component_label(name: str) -> str:
    return label_for_code(name, COMPONENT_LABELS, unknown_prefix="评分项")


def evidence_label(code: str) -> str:
    return label_for_code(code, EVIDENCE_LABELS, unknown_prefix="证据")


def risk_label(code: str) -> str:
    return label_for_code(code, RISK_LABELS, unknown_prefix="风险")


def structure_label(value: str) -> str:
    return label_for_code(value, STRUCTURE_LABELS, unknown_prefix="结构")


def reason_label(code: str) -> str:
    return label_for_code(code, REASON_LABELS, unknown_prefix="原因")


def status_label(value: str | None) -> str:
    if value is None:
        return "—"
    return label_for_code(value, STATUS_LABELS, unknown_prefix="状态")


__all__ = [
    "component_label",
    "evidence_label",
    "metric_label",
    "reason_label",
    "risk_label",
    "status_label",
    "structure_label",
]
