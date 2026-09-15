import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from stock_daily_report import cli as cli_module
from stock_daily_report import pipeline as pipeline_module
from stock_daily_report.config import (
    ConfigurationError,
    load_market_scan_settings,
    load_settings,
)
from stock_daily_report.market_scan.models import (
    MarketScanArtifact,
    ProfileRankings,
)
from stock_daily_report.market_scan.report import write_scan_artifact
from stock_daily_report.models import DailyBar
from stock_daily_report.providers.base import ProviderAvailabilityError
from stock_daily_report.providers.service import MarketDataService
from stock_daily_report.providers.universe import AkShareUniverseProvider

REPORT_DATE = date(2026, 9, 11)


def _scan_settings():
    return load_market_scan_settings(
        Path(__file__).parents[1] / "config" / "market_scan.yaml"
    )


def _scan_config_hash(settings, data_settings=None) -> str:
    payload = settings.model_dump(mode="json")
    if data_settings is not None:
        payload = {
            "market_data": data_settings.market_data.model_dump(mode="json"),
            "market_scan": payload,
        }
    encoded = json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _empty_artifact(*, report_date: date, config_hash: str) -> MarketScanArtifact:
    return MarketScanArtifact(
        rule_version="market-scan-v1",
        report_date=report_date,
        generated_at=datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
        universe_count=0,
        eligible_count=0,
        valid_count=0,
        coverage=0.0,
        exclusion_counts={},
        failure_counts={},
        rankings=ProfileRankings(),
        consensus=(),
        statuses=(),
        config_hash=config_hash,
        input_hash="1" * 64,
        provider_names=("akshare-universe", "market-data-service"),
    )


def test_market_scan_cli_accepts_date_settings_and_output_root(
    monkeypatch, tmp_path, capsys
):
    settings = object()
    data_settings = SimpleNamespace(market_data=object())
    universe_provider = object()
    history_service = object()
    index_provider = object()
    settings_path = tmp_path / "market-scan.yaml"
    data_settings_path = tmp_path / "settings.yaml"
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
        "load_settings",
        lambda path: (
            data_settings if path == data_settings_path else pytest.fail(str(path))
        ),
    )
    monkeypatch.setattr(cli_module, "_current_market_date", lambda: REPORT_DATE, raising=False)
    monkeypatch.setattr(
        cli_module,
        "AkShareUniverseProvider",
        lambda: universe_provider,
    )
    monkeypatch.setattr(
        cli_module,
        "AkShareIndexProvider",
        lambda: index_provider,
        raising=False,
    )

    def fake_build_market_data_service(configured_settings, *, output_root):
        assert configured_settings is data_settings
        assert output_root == output_root_path
        return history_service

    output_root_path = output_root
    monkeypatch.setattr(
        cli_module,
        "build_market_data_service",
        fake_build_market_data_service,
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "market_scan_config_hash",
        lambda configured_scan, configured_data: (
            "a" * 64
            if configured_scan is settings and configured_data is data_settings.market_data
            else pytest.fail("unexpected configuration hash inputs")
        ),
        raising=False,
    )

    def fake_run_market_scan(
        configured_settings,
        configured_universe_provider,
        configured_history_provider,
        *,
        report_date,
        output_root: Path,
        configuration_hash,
        index_provider,
    ):
        calls.append(
            (
                configured_settings,
                configured_universe_provider,
                configured_history_provider,
                report_date,
                output_root,
                configuration_hash,
                index_provider,
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
            "--data-settings",
            str(data_settings_path),
            "--output-root",
            str(output_root),
        ]
    )

    assert result == 0
    assert calls == [
        (
            settings,
            universe_provider,
            history_service,
            REPORT_DATE,
            output_root,
            "a" * 64,
            index_provider,
        )
    ]
    assert capsys.readouterr().out == f"{artifact_path}\n"


def test_market_scan_cli_rejects_non_positive_max_batches(capsys):
    result = cli_module.main(
        [
            "market-scan",
            "--date",
            REPORT_DATE.isoformat(),
            "--resumable",
            "--max-batches",
            "0",
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert captured.err == "--max-batches must be a positive integer\n"


def test_market_scan_cli_wires_index_provider_for_market_scope(
    monkeypatch, tmp_path, capsys
):
    settings = object()
    data_settings = SimpleNamespace(market_data=object())
    universe_provider = object()
    history_service = object()
    index_provider = object()
    artifact_path = tmp_path / "market-scans" / "2026-09-11" / "scan.json"
    observed = {}

    monkeypatch.setattr(cli_module, "_current_market_date", lambda: REPORT_DATE)
    monkeypatch.setattr(cli_module, "load_market_scan_settings", lambda _path: settings)
    monkeypatch.setattr(cli_module, "load_settings", lambda _path: data_settings)
    monkeypatch.setattr(cli_module, "AkShareUniverseProvider", lambda: universe_provider)
    monkeypatch.setattr(
        cli_module,
        "AkShareIndexProvider",
        lambda: index_provider,
        raising=False,
    )
    monkeypatch.setattr(
        cli_module,
        "build_market_data_service",
        lambda _settings, *, output_root: history_service,
    )
    monkeypatch.setattr(
        cli_module,
        "market_scan_config_hash",
        lambda _scan, _data: "a" * 64,
    )

    def fake_run_market_scan(
        configured_settings,
        configured_universe_provider,
        configured_history_provider,
        *,
        report_date,
        output_root,
        configuration_hash,
        index_provider,
    ):
        observed.update(
            settings=configured_settings,
            universe=configured_universe_provider,
            history=configured_history_provider,
            date=report_date,
            root=output_root,
            config=configuration_hash,
            index=index_provider,
        )
        return artifact_path

    monkeypatch.setattr(cli_module, "run_market_scan", fake_run_market_scan)

    result = cli_module.main(
        [
            "market-scan",
            "--date",
            REPORT_DATE.isoformat(),
            "--output-root",
            str(tmp_path),
        ]
    )

    assert result == 0
    assert observed == {
        "settings": settings,
        "universe": universe_provider,
        "history": history_service,
        "date": REPORT_DATE,
        "root": tmp_path,
        "config": "a" * 64,
        "index": index_provider,
    }
    assert capsys.readouterr().out == f"{artifact_path}\n"


def test_market_scan_cli_rejects_backdated_live_scan_without_artifact(
    monkeypatch, tmp_path, capsys
):
    monkeypatch.setattr(cli_module, "_current_market_date", lambda: REPORT_DATE, raising=False)
    monkeypatch.setattr(
        cli_module,
        "load_market_scan_settings",
        lambda _path: _scan_settings(),
    )
    monkeypatch.setattr(
        cli_module,
        "run_market_scan",
        lambda *_args, **_kwargs: pytest.fail("backdated scan must not run"),
    )

    result = cli_module.main(
        [
            "market-scan",
            "--date",
            "2026-09-10",
            "--output-root",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert (
        captured.err
        == "Live market scans only support the current Asia/Shanghai date "
        "2026-09-11; requested 2026-09-10.\n"
    )
    assert not (tmp_path / "market-scans").exists()


def test_market_scan_cli_uses_configured_fallback_and_report_date(
    monkeypatch, tmp_path, capsys
):
    class UnavailableProvider:
        name = "akshare"

        def get_daily_bars(self, code, *, start=None, end=None):
            del code, start, end
            raise ProviderAvailabilityError("akshare", "network_error", "offline")

    class FallbackProvider:
        name = "sina"

        def get_daily_bars(self, code, *, start=None, end=None):
            del code, start
            return [
                DailyBar(
                    trade_date=end,
                    open=10.0,
                    high=11.0,
                    low=9.0,
                    close=10.5,
                    volume=1_000.0,
                    amount=10_500.0,
                    turnover_rate=0.5,
                    adjustment_mode="qfq",
                    provider_name="sina",
                    source_timestamp=datetime(2026, 9, 11, 8, 0, tzinfo=UTC),
                )
            ]

    project_root = Path(__file__).parents[1]
    configured = load_settings(project_root / "config" / "settings.yaml")
    configured = configured.model_copy(
        update={
            "market_data": configured.market_data.model_copy(
                update={
                    "cache_directory": str(tmp_path / "cache"),
                    "minimum_history_bars": 1,
                    "max_completed_trading_day_lag": 0,
                }
            )
        }
    )
    artifact_path = tmp_path / "market-scans" / REPORT_DATE.isoformat() / "scan.json"
    observed = {}

    monkeypatch.setattr(cli_module, "_current_market_date", lambda: REPORT_DATE, raising=False)
    monkeypatch.setattr(
        cli_module,
        "load_market_scan_settings",
        lambda _path: _scan_settings(),
    )
    monkeypatch.setattr(cli_module, "load_settings", lambda _path: configured)
    monkeypatch.setattr(cli_module, "AkShareUniverseProvider", lambda: object())
    monkeypatch.setattr(
        pipeline_module,
        "AkShareMarketDataProvider",
        lambda **_kwargs: UnavailableProvider(),
    )
    monkeypatch.setattr(
        pipeline_module,
        "SinaMarketDataProvider",
        lambda **_kwargs: FallbackProvider(),
    )

    def fake_run_market_scan(
        _settings,
        _universe_provider,
        history_provider,
        *,
        report_date,
        output_root,
        configuration_hash,
        index_provider,
    ):
        assert index_provider is not None
        assert isinstance(history_provider, MarketDataService)
        fetched = history_provider.fetch(
            "600519",
            end=report_date,
            as_of=report_date,
        )
        observed["provider_name"] = fetched.provider_name
        observed["as_of"] = fetched.quality.as_of
        observed["configuration_hash"] = configuration_hash
        assert output_root == tmp_path
        return artifact_path

    monkeypatch.setattr(cli_module, "run_market_scan", fake_run_market_scan)

    result = cli_module.main(
        [
            "market-scan",
            "--date",
            REPORT_DATE.isoformat(),
            "--output-root",
            str(tmp_path),
        ]
    )

    assert result == 0
    assert observed == {
        "provider_name": "sina",
        "as_of": REPORT_DATE,
        "configuration_hash": _scan_config_hash(_scan_settings(), configured),
    }
    assert capsys.readouterr().out == f"{artifact_path}\n"


def test_market_scan_cli_rejects_invalid_existing_artifact(
    monkeypatch, tmp_path, capsys
):
    artifact_path = tmp_path / "market-scans" / REPORT_DATE.isoformat() / "scan.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text("{not-json", encoding="utf-8")

    monkeypatch.setattr(cli_module, "_current_market_date", lambda: REPORT_DATE, raising=False)
    monkeypatch.setattr(cli_module, "load_market_scan_settings", lambda _path: _scan_settings())
    monkeypatch.setattr(
        cli_module,
        "run_market_scan",
        lambda *_args, **_kwargs: pytest.fail("invalid artifact must not be reused"),
    )

    result = cli_module.main(
        [
            "market-scan",
            "--date",
            REPORT_DATE.isoformat(),
            "--output-root",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "must be valid JSON" in captured.err
    assert "Traceback" not in captured.err


def test_market_scan_cli_reuses_valid_backdated_artifact(
    monkeypatch, tmp_path, capsys
):
    settings = _scan_settings()
    data_settings = load_settings(
        Path(__file__).parents[1] / "config" / "settings.yaml"
    )
    artifact = _empty_artifact(
        report_date=date(2026, 9, 10),
        config_hash=_scan_config_hash(settings, data_settings),
    )
    artifact_path = write_scan_artifact(tmp_path, artifact)

    monkeypatch.setattr(cli_module, "_current_market_date", lambda: REPORT_DATE)
    monkeypatch.setattr(cli_module, "load_market_scan_settings", lambda _path: settings)
    monkeypatch.setattr(
        cli_module,
        "load_settings",
        lambda _path: data_settings,
    )
    monkeypatch.setattr(
        cli_module,
        "run_market_scan",
        lambda *_args, **_kwargs: pytest.fail("reuse must not run a live scan"),
    )

    result = cli_module.main(
        [
            "market-scan",
            "--date",
            "2026-09-10",
            "--output-root",
            str(tmp_path),
        ]
    )

    assert result == 0
    assert capsys.readouterr().out == f"{artifact_path}\n"


def test_market_scan_cli_rejects_reuse_after_data_settings_change(
    monkeypatch, tmp_path, capsys
):
    settings = _scan_settings()
    baseline = load_settings(Path(__file__).parents[1] / "config" / "settings.yaml")
    artifact = _empty_artifact(
        report_date=REPORT_DATE,
        config_hash=_scan_config_hash(settings, baseline),
    )
    write_scan_artifact(tmp_path, artifact)
    configured = baseline.model_copy(
        update={
            "market_data": baseline.market_data.model_copy(
                update={"fallback_provider": "fixture"}
            )
        }
    )

    monkeypatch.setattr(cli_module, "_current_market_date", lambda: REPORT_DATE)
    monkeypatch.setattr(cli_module, "load_market_scan_settings", lambda _path: settings)
    monkeypatch.setattr(cli_module, "load_settings", lambda _path: configured)
    monkeypatch.setattr(
        cli_module,
        "run_market_scan",
        lambda *_args, **_kwargs: pytest.fail("mismatched artifact must not be reused"),
    )

    result = cli_module.main(
        [
            "market-scan",
            "--date",
            REPORT_DATE.isoformat(),
            "--output-root",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "configuration does not match" in captured.err


@pytest.mark.parametrize(
    ("artifact_date", "config_hash", "message"),
    [
        (date(2026, 9, 10), None, "report_date does not match requested date"),
        (REPORT_DATE, "f" * 64, "configuration does not match"),
    ],
)
def test_market_scan_cli_rejects_mismatched_existing_artifact(
    monkeypatch,
    tmp_path,
    capsys,
    artifact_date,
    config_hash,
    message,
):
    settings = _scan_settings()
    artifact = _empty_artifact(
        report_date=artifact_date,
        config_hash=config_hash or _scan_config_hash(settings),
    )
    artifact_path = tmp_path / "market-scans" / REPORT_DATE.isoformat() / "scan.json"
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(artifact.model_dump_json(), encoding="utf-8")

    monkeypatch.setattr(cli_module, "_current_market_date", lambda: REPORT_DATE, raising=False)
    monkeypatch.setattr(cli_module, "load_market_scan_settings", lambda _path: settings)
    monkeypatch.setattr(
        cli_module,
        "run_market_scan",
        lambda *_args, **_kwargs: pytest.fail("mismatched artifact must not be reused"),
    )

    result = cli_module.main(
        [
            "market-scan",
            "--date",
            REPORT_DATE.isoformat(),
            "--output-root",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert message in captured.err
    assert "Traceback" not in captured.err


def test_market_scan_cli_reports_invalid_output_root_without_traceback(
    monkeypatch, tmp_path, capsys
):
    settings = _scan_settings()
    data_settings = SimpleNamespace(market_data=object())
    invalid_root = tmp_path / "not-a-directory"
    invalid_root.write_text("occupied", encoding="utf-8")
    artifact = _empty_artifact(
        report_date=REPORT_DATE,
        config_hash=_scan_config_hash(settings),
    )

    monkeypatch.setattr(cli_module, "_current_market_date", lambda: REPORT_DATE)
    monkeypatch.setattr(cli_module, "load_market_scan_settings", lambda _path: settings)
    monkeypatch.setattr(cli_module, "load_settings", lambda _path: data_settings)
    monkeypatch.setattr(
        cli_module,
        "market_scan_config_hash",
        lambda configured_scan, configured_data: (
            "a" * 64
            if configured_scan is settings and configured_data is data_settings.market_data
            else pytest.fail("unexpected configuration hash inputs")
        ),
    )
    monkeypatch.setattr(cli_module, "AkShareUniverseProvider", lambda: object())
    monkeypatch.setattr(
        cli_module,
        "build_market_data_service",
        lambda configured, *, output_root: (
            object()
            if configured is data_settings and output_root == invalid_root
            else pytest.fail("unexpected service configuration")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "run_market_scan",
        lambda *_args, **_kwargs: write_scan_artifact(invalid_root, artifact),
    )

    result = cli_module.main(
        [
            "market-scan",
            "--date",
            REPORT_DATE.isoformat(),
            "--output-root",
            str(invalid_root),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "Could not persist market scan artifact" in captured.err
    assert "Traceback" not in captured.err


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


def test_market_scan_cli_reports_sina_decode_failure_without_traceback(
    monkeypatch, tmp_path, capsys
):
    json_decode_error = pytest.importorskip(
        "akshare.utils.demjson"
    ).JSONDecodeError("rate-limit HTML")
    settings = object()
    data_settings = SimpleNamespace(market_data=object())
    provider = AkShareUniverseProvider(
        fetcher=lambda: (_ for _ in ()).throw(OSError("eastmoney offline")),
        fallback_fetcher=lambda: (_ for _ in ()).throw(json_decode_error),
    )

    monkeypatch.setattr(cli_module, "_current_market_date", lambda: REPORT_DATE)
    monkeypatch.setattr(
        cli_module, "load_market_scan_settings", lambda _path: settings
    )
    monkeypatch.setattr(cli_module, "load_settings", lambda _path: data_settings)
    monkeypatch.setattr(cli_module, "AkShareUniverseProvider", lambda: provider)
    monkeypatch.setattr(
        cli_module,
        "build_market_data_service",
        lambda configured, *, output_root: (
            object()
            if configured is data_settings and output_root == tmp_path
            else pytest.fail("unexpected service configuration")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "market_scan_config_hash",
        lambda configured_scan, configured_data: (
            "a" * 64
            if configured_scan is settings
            and configured_data is data_settings.market_data
            else pytest.fail("unexpected configuration hash inputs")
        ),
    )
    monkeypatch.setattr(
        cli_module,
        "run_market_scan",
        lambda _settings, universe, _history, **_kwargs: universe.get_quotes(),
    )

    result = cli_module.main(
        [
            "market-scan",
            "--date",
            REPORT_DATE.isoformat(),
            "--output-root",
            str(tmp_path),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "all_endpoints_unavailable" in captured.err
    assert "stock_zh_a_spot_em" in captured.err
    assert "eastmoney offline" in captured.err
    assert "stock_zh_a_spot" in captured.err
    assert "invalid_remote_response" in captured.err
    assert "Traceback" not in captured.err
