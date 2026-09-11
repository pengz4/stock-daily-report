from datetime import date
from pathlib import Path

import pytest

from stock_daily_report import cli as cli_module
from stock_daily_report.config import ConfigurationError
from stock_daily_report.providers.base import ProviderAvailabilityError


def test_market_scan_cli_accepts_date_settings_and_output_root(
    monkeypatch, tmp_path, capsys
):
    settings = object()
    universe_provider = object()
    history_provider = object()
    settings_path = tmp_path / "market-scan.yaml"
    output_root = tmp_path / "output"
    artifact_path = output_root / "market-scans" / "2026-09-11" / "scan.json"
    calls = []

    monkeypatch.setattr(
        cli_module,
        "load_market_scan_settings",
        lambda path: settings if path == settings_path else pytest.fail(str(path)),
    )
    monkeypatch.setattr(
        cli_module,
        "AkShareUniverseProvider",
        lambda: universe_provider,
    )
    monkeypatch.setattr(
        cli_module,
        "AkShareMarketDataProvider",
        lambda: history_provider,
    )

    def fake_run_market_scan(
        configured_settings,
        configured_universe_provider,
        configured_history_provider,
        *,
        report_date,
        output_root: Path,
    ):
        calls.append(
            (
                configured_settings,
                configured_universe_provider,
                configured_history_provider,
                report_date,
                output_root,
            )
        )
        return artifact_path

    monkeypatch.setattr(cli_module, "run_market_scan", fake_run_market_scan)

    result = cli_module.main(
        [
            "market-scan",
            "--date",
            "2026-09-11",
            "--settings",
            str(settings_path),
            "--output-root",
            str(output_root),
        ]
    )

    assert result == 0
    assert calls == [
        (
            settings,
            universe_provider,
            history_provider,
            date(2026, 9, 11),
            output_root,
        )
    ]
    assert capsys.readouterr().out == f"{artifact_path}\n"


@pytest.mark.parametrize(
    ("error", "message"),
    [
        (ConfigurationError("Invalid market scan configuration"), "Invalid market"),
        (
            ProviderAvailabilityError("akshare", "network_error", "offline"),
            "akshare[network_error]: offline",
        ),
    ],
)
def test_market_scan_cli_reports_expected_failures_without_traceback(
    monkeypatch, capsys, error, message
):
    monkeypatch.setattr(
        cli_module,
        "load_market_scan_settings",
        lambda _path: (_ for _ in ()).throw(error),
    )

    result = cli_module.main(["market-scan", "--date", "2026-09-11"])

    captured = capsys.readouterr()
    assert result == 1
    assert message in captured.err
    assert "Traceback" not in captured.err
