import json
from pathlib import Path

from stock_daily_report.backtest.runner import run_backtest
from stock_daily_report.config import load_backtest_settings, load_watchlist
from stock_daily_report.providers.fixture import FixtureMarketDataProvider


def test_run_backtest_writes_report_with_assumptions_and_evaluations(tmp_path):
    root = Path(__file__).parents[1]
    report_path = run_backtest(
        load_backtest_settings(root / "config/backtest.yaml"),
        load_watchlist(root / "config/watchlist.yaml"),
        FixtureMarketDataProvider(root / "fixtures/bars"),
        output_root=tmp_path,
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))

    assert report_path == tmp_path / "reports" / "backtests" / "2026-09-04" / "report.json"
    assert report["backtest_assumptions"]["rule_version"] == "backtest-v1"
    assert "simplified-v1:600519" in report["evaluations"]
