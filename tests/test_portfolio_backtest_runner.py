import json
from datetime import UTC, date, datetime, timedelta

from stock_daily_report.models import (
    BacktestCostsSettings,
    MarketScanSettings,
    PortfolioBacktestSettings,
    Watchlist,
)


class _MemoryProvider:
    def __init__(self, bars):
        self.bars = bars

    def get_daily_bars(self, code, *, start=None, end=None):
        return [
            bar for bar in self.bars[code]
            if (start is None or bar.trade_date >= start)
            and (end is None or bar.trade_date <= end)
        ]


def _bar(day, price):
    from stock_daily_report.models import DailyBar

    return DailyBar(
        trade_date=day,
        open=price,
        high=price + 0.1,
        low=price - 0.1,
        close=price,
        volume=10_000,
        amount=price * 10_000,
        turnover_rate=1,
        adjustment_mode="qfq",
        provider_name="fixture",
        source_timestamp=datetime.combine(day, datetime.min.time(), UTC),
    )


def test_runner_scores_only_asof_history_and_marks_short_fixture_insufficient(
    tmp_path, monkeypatch
):
    from stock_daily_report.backtest import portfolio_runner

    days = [date(2026, 1, 5) + timedelta(days=index) for index in range(4)]
    provider = _MemoryProvider(
        {"600519": [_bar(day, 10 + index) for index, day in enumerate(days)]}
    )
    portfolio_settings = PortfolioBacktestSettings(
        rule_version="portfolio-backtest-v1",
        profile="trend",
        max_positions=1,
        rebalance_every_sessions=1,
        initial_cash=100_000,
        minimum_sample_count=20,
        out_of_sample_start=days[0],
        costs=BacktestCostsSettings(
            commission_bps=0,
            slippage_bps=0,
            price_limit_pct=0.1,
            price_tick=0.01,
        ),
    )
    scan_settings = MarketScanSettings(
        rule_version="market-scan-v1",
        trend_limit=30,
        balanced_limit=30,
        minimum_history_bars=1,
        minimum_latest_amount=1,
        minimum_coverage_ratio=0.8,
        max_workers=1,
        max_candidates=10,
    )
    original_score = portfolio_runner.score_candidate
    scored_dates = []

    def inspect_point_in_time(code, bars, *, as_of):
        assert all(bar.trade_date <= as_of for bar in bars)
        scored_dates.append(as_of)
        return original_score(code, bars, as_of=as_of)

    monkeypatch.setattr(portfolio_runner, "score_candidate", inspect_point_in_time)
    report_path = portfolio_runner.run_portfolio_backtest(
        portfolio_settings,
        scan_settings,
        Watchlist(stocks=[{"code": "600519", "name": "贵州茅台"}]),
        provider,
        output_root=tmp_path,
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert scored_dates == days
    assert report["report_type"] == "portfolio-backtest"
    assert report["evidence_status"] == "not_enough_evidence"
    assert report["excess_return"] is None
    assert report["metric_assumptions"]["turnover_definition"] == (
        "gross traded notional divided by initial capital"
    )
    assert report["data_limitations"]
    assert all(
        trade["trade_date"] > trade["signal_date"] for trade in report["trades"]
    )
