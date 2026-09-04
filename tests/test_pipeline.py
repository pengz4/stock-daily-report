import json
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta

import pytest

from stock_daily_report.models import DailyBar, Settings, Watchlist
from stock_daily_report.pipeline import PipelineError, run_daily_report


class RecordingProvider:
    name = "fixture"

    def __init__(self, bars_by_code, *, fail_code=None, on_call=None):
        self.bars_by_code = bars_by_code
        self.fail_code = fail_code
        self.on_call = on_call
        self.calls = []

    def get_daily_bars(self, code, *, start=None, end=None):
        self.calls.append(code)
        if self.on_call is not None:
            self.on_call(code)
        if code == self.fail_code:
            return list(self.bars_by_code[code][:10])
        return list(self.bars_by_code[code])


def make_bars(code: str, count: int = 80) -> list[DailyBar]:
    start = date(2026, 6, 17)
    return [
        DailyBar(
            trade_date=start + timedelta(days=index),
            open=100.0 + index,
            high=102.0 + index,
            low=99.0 + index,
            close=101.0 + index,
            volume=1_000.0 + index,
            amount=(101.0 + index) * 1_000.0,
            turnover_rate=0.1,
            adjustment_mode="qfq",
            provider_name="fixture",
            source_timestamp=datetime(2026, 9, 4, 8, tzinfo=UTC),
        )
        for index in range(count)
    ]


@pytest.fixture
def fixture_settings() -> Settings:
    return Settings(
        rule_version={"name": "simplified", "version": "v1"},
        notifications={"enabled_channels": []},
        market_data={
            "primary_provider": "fixture",
            "fallback_provider": "unused",
            "minimum_history_bars": 60,
            "max_completed_trading_day_lag": 1,
        },
        risk_rules={
            "rule_version": "risk-v1",
            "high_realized_volatility20": 0.45,
            "overextension_ma20_distance": 0.20,
            "large_drawdown60": -0.20,
            "adverse_volume_ratio20": 0.50,
            "minimum_history_bars": 61,
        },
    )


def test_daily_pipeline_writes_json_markdown_and_html(tmp_path, fixture_settings):
    watchlist = Watchlist(
        stocks=[{"code": "600519", "name": "贵州茅台", "group": "consumer"}]
    )
    provider = RecordingProvider({"600519": make_bars("600519")})

    outputs = run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=provider,
        report_date=date(2026, 9, 4),
        now=lambda: datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
    )

    assert outputs.json_path == tmp_path / "reports/2026-09-04/report.json"
    assert outputs.markdown_path == tmp_path / "reports/2026-09-04/report.md"
    assert outputs.html_path == tmp_path / "reports/2026-09-04/index.html"
    assert outputs.snapshot_path == tmp_path / "snapshots/2026-09-04/input.json"


def test_pipeline_fetches_and_validates_every_symbol_before_writing(tmp_path, fixture_settings):
    watchlist = Watchlist(
        stocks=[
            {"code": "600519", "name": "one"},
            {"code": "000001", "name": "two"},
        ]
    )
    observed_artifacts = []
    provider = RecordingProvider(
        {"600519": make_bars("600519"), "000001": make_bars("000001")},
        fail_code="000001",
        on_call=lambda _code: observed_artifacts.append(
            (tmp_path / "snapshots").exists() or (tmp_path / "reports").exists()
        ),
    )

    with pytest.raises(PipelineError):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            provider=provider,
            report_date=date(2026, 9, 4),
        )

    assert provider.calls == ["000001", "600519"]
    assert observed_artifacts == [False, False]
    report_dir = tmp_path / "reports/2026-09-04"
    assert not (report_dir / "report.json").exists()
    assert not (report_dir / "report.md").exists()
    assert not (report_dir / "index.html").exists()


@pytest.mark.parametrize("failure_kind", ["stale", "invalid"])
def test_quality_failure_leaves_no_success_artifacts(
    tmp_path, fixture_settings, failure_kind
):
    bars = make_bars("600519")
    if failure_kind == "stale":
        bars = [
            bar.model_copy(update={"trade_date": bar.trade_date - timedelta(days=20)})
            for bar in bars
        ]
    else:
        invalid = bars[-1].model_dump(mode="json")
        invalid["close"] = 0
        bars[-1] = invalid
    provider = RecordingProvider({"600519": bars})

    with pytest.raises(PipelineError):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            provider=provider,
            report_date=date(2026, 9, 4),
        )

    report_dir = tmp_path / "reports/2026-09-04"
    assert not any((report_dir / name).exists() for name in ("report.json", "report.md", "index.html"))
    assert not (tmp_path / "snapshots/2026-09-04/input.json").exists()


def test_report_json_is_deterministic_and_contains_auditing_metadata(
    tmp_path, fixture_settings
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "贵州茅台"}])
    provider = RecordingProvider({"600519": make_bars("600519")})
    outputs = run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=provider,
        report_date=date(2026, 9, 4),
        now=lambda: datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
    )
    first = outputs.json_path.read_bytes()
    document = json.loads(first)

    assert first == outputs.json_path.read_bytes()
    assert document["schema_version"] == 1
    assert document["metadata"]["snapshot_path"] == "snapshots/2026-09-04/input.json"
    assert document["metadata"]["config_hash"] == outputs.report.metadata.config_hash
    assert document["metadata"]["analyzer_versions"]["structural"] == "simplified-v1"
    assert document["metadata"]["stock_count"] == 1
    assert "webhook" not in outputs.json_path.read_text(encoding="utf-8")


def test_report_artifacts_do_not_leak_notification_webhooks(tmp_path, fixture_settings):
    settings = fixture_settings.model_copy(
        update={
            "notifications": {
                "enabled_channels": ["wecom"],
                "wecom_webhook_url": "https://secret.example/webhook",
            }
        }
    )
    outputs = run_daily_report(
        settings,
        output_root=tmp_path,
        watchlist=Watchlist(stocks=[{"code": "600519", "name": "visible"}]),
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=date(2026, 9, 4),
    )

    for path in (outputs.json_path, outputs.markdown_path, outputs.html_path):
        assert "secret.example" not in path.read_text(encoding="utf-8")


def test_cli_daily_fixture_invocation(tmp_path, fixture_settings):
    fixture_dir = tmp_path / "fixtures"
    fixture_dir.mkdir()
    rows = make_bars("600519")
    fixture_file = fixture_dir / "600519.csv"
    fields = (
        "trade_date,open,high,low,close,volume,amount,turnover_rate,"
        "adjustment_mode,provider_name,source_timestamp\n"
    )
    fixture_file.write_text(
        fields
        + "".join(
            ",".join(
                [
                    bar.trade_date.isoformat(),
                    str(bar.open),
                    str(bar.high),
                    str(bar.low),
                    str(bar.close),
                    str(bar.volume),
                    str(bar.amount),
                    str(bar.turnover_rate),
                    bar.adjustment_mode,
                    bar.provider_name,
                    bar.source_timestamp.isoformat(),
                ]
            )
            + "\n"
            for bar in rows
        ),
        encoding="utf-8",
    )
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        """rule_version:
  name: simplified
  version: v1
notifications:
  enabled_channels: []
market_data:
  primary_provider: fixture
  fallback_provider: unused
  minimum_history_bars: 60
  max_completed_trading_day_lag: 1
risk_rules:
  rule_version: risk-v1
  high_realized_volatility20: 0.45
  overextension_ma20_distance: 0.20
  large_drawdown60: -0.20
  adverse_volume_ratio20: 0.50
  minimum_history_bars: 61
""",
        encoding="utf-8",
    )
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text(
        "stocks:\n  - code: '600519'\n    name: 贵州茅台\n", encoding="utf-8"
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "stock_daily_report.cli",
            "daily",
            "--date",
            "2026-09-04",
            "--settings",
            str(settings_path),
            "--watchlist",
            str(watchlist_path),
            "--fixture-directory",
            str(fixture_dir),
            "--output-root",
            str(tmp_path / "published"),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "published/reports/2026-09-04/index.html").exists()


def test_static_site_index_links_to_relative_dated_report_pages(tmp_path, fixture_settings):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "贵州茅台"}])
    provider = RecordingProvider({"600519": make_bars("600519")})
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=provider,
        report_date=date(2026, 9, 4),
    )

    site_index = (tmp_path / "site/index.html").read_text(encoding="utf-8")
    assert "../reports/2026-09-04/index.html" in site_index
