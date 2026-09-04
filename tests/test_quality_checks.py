import hashlib
import json
import os
import subprocess
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from stock_daily_report.models import DailyBar
from stock_daily_report.quality.checks import DataQualitySettings, validate_bars

FIXTURE_DIRECTORY = Path(__file__).parents[1] / "fixtures" / "bars"


@pytest.fixture
def bars() -> list[DailyBar]:
    return [
        DailyBar(
            trade_date=date(2026, 9, day),
            open=100.0 + index,
            high=103.0 + index,
            low=99.0 + index,
            close=101.0 + index,
            volume=1_000.0,
            amount=101_000.0,
            turnover_rate=0.1,
            adjustment_mode="qfq",
            provider_name="test",
            source_timestamp=datetime(2026, 9, day, 8, tzinfo=UTC),
        )
        for index, day in enumerate((1, 2, 3, 4))
    ]


def test_quality_gate_rejects_duplicate_trading_dates(bars):
    duplicated = [*bars, bars[-1]]

    result = validate_bars(
        "600519",
        duplicated,
        as_of=date(2026, 9, 4),
        settings=DataQualitySettings(minimum_history_bars=1),
    )

    assert result.is_valid is False
    assert result.analysis_allowed is False
    assert "duplicate_trade_date" in result.issue_codes


def test_quality_gate_collects_chronological_and_raw_ohlc_issues(bars):
    invalid = bars[-1].model_dump()
    invalid.update({"trade_date": "2026-09-02", "open": -1, "high": 3, "low": 4})
    invalid.pop("close")
    out_of_range = bars[-2].model_dump()
    out_of_range["close"] = 200

    result = validate_bars(
        "600519",
        [bars[2], invalid, out_of_range],
        as_of=date(2026, 9, 4),
        settings=DataQualitySettings(minimum_history_bars=4),
    )

    assert result.analysis_allowed is False
    assert set(result.issue_codes) >= {
        "non_chronological_trade_dates",
        "duplicate_trade_date",
        "missing_ohlc",
        "negative_ohlc",
        "high_lt_low",
        "close_outside_low_high",
        "insufficient_history",
    }
    assert len(result.issues) >= len(result.issue_codes)


def test_quality_gate_rejects_stale_weekday_data_but_not_friday_on_weekend(bars):
    settings = DataQualitySettings(
        minimum_history_bars=1, max_completed_trading_day_lag=0
    )
    stale = validate_bars(
        "600519", bars[:1], as_of=date(2026, 9, 4), settings=settings
    )
    weekend = validate_bars(
        "600519", bars, as_of=date(2026, 9, 5), settings=settings
    )

    assert "stale_last_trade_date" in stale.issue_codes
    assert "stale_last_trade_date" not in weekend.issue_codes


def test_quality_gate_rejects_future_trade_dates(bars):
    future = bars[-1].model_copy(update={"trade_date": date(2026, 9, 5)})

    result = validate_bars(
        "600519",
        [*bars, future],
        as_of=date(2026, 9, 4),
        settings=DataQualitySettings(minimum_history_bars=1),
    )

    assert result.analysis_allowed is False
    assert "future_trade_date" in result.issue_codes


class RecordingProvider:
    def __init__(self, name, response=None, error=None):
        self.name = name
        self.response = response
        self.error = error
        self.calls = 0

    def get_daily_bars(self, code, *, start=None, end=None):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


def _service(primary, fallback, cache_directory, *, now=None):
    from stock_daily_report.providers.service import MarketDataService

    return MarketDataService(
        {primary.name: primary, fallback.name: fallback},
        primary_provider=primary.name,
        fallback_provider=fallback.name,
        cache_directory=cache_directory,
        cache_ttl_seconds=30,
        quality_settings=DataQualitySettings(minimum_history_bars=1),
        now=now,
    )


def test_service_falls_back_only_for_expected_provider_failure(tmp_path, bars):
    from stock_daily_report.providers.base import ProviderAvailabilityError

    primary = RecordingProvider(
        "primary",
        error=ProviderAvailabilityError("primary", "network_error", "unavailable"),
    )
    fallback = RecordingProvider("fallback", response=bars)

    fetched = _service(primary, fallback, tmp_path).fetch(
        "600519", as_of=date(2026, 9, 4)
    )

    assert fetched.provider_name == "fallback"
    assert fetched.quality.analysis_allowed is True
    assert (primary.calls, fallback.calls) == (1, 1)


def test_service_uses_provider_order_and_thresholds_from_settings(tmp_path, bars):
    from stock_daily_report.models import MarketDataSettings
    from stock_daily_report.providers.base import ProviderAvailabilityError
    from stock_daily_report.providers.service import MarketDataService

    primary = RecordingProvider(
        "primary",
        error=ProviderAvailabilityError("primary", "network_error", "unavailable"),
    )
    fallback = RecordingProvider("fallback", response=bars)
    settings = MarketDataSettings(
        primary_provider="primary",
        fallback_provider="fallback",
        cache_directory=str(tmp_path),
        cache_ttl_seconds=30,
        minimum_history_bars=1,
        max_completed_trading_day_lag=0,
    )

    fetched = MarketDataService.from_settings(
        {primary.name: primary, fallback.name: fallback}, settings
    ).fetch("600519", as_of=date(2026, 9, 4))

    assert fetched.provider_name == "fallback"
    assert (primary.calls, fallback.calls) == (1, 1)


def test_service_does_not_fallback_after_invalid_or_partial_primary_data(
    tmp_path, bars
):
    from stock_daily_report.providers.service import DataQualityError

    primary = RecordingProvider("primary", response=bars[:0])
    fallback = RecordingProvider("fallback", response=bars)

    with pytest.raises(DataQualityError, match="insufficient_history") as error:
        _service(primary, fallback, tmp_path).fetch(
            "600519", as_of=date(2026, 9, 4)
        )

    assert "insufficient_history" in error.value.quality.issue_codes
    assert (primary.calls, fallback.calls) == (1, 0)
    assert list(tmp_path.glob("*.json")) == []


def test_service_does_not_fallback_after_invalid_primary_data(tmp_path, bars):
    invalid = bars[-1].model_dump()
    invalid["low"] = -1
    primary = RecordingProvider("primary", response=[*bars[:-1], invalid])
    fallback = RecordingProvider("fallback", response=bars)

    from stock_daily_report.providers.service import DataQualityError

    with pytest.raises(DataQualityError, match="negative_ohlc"):
        _service(primary, fallback, tmp_path).fetch(
            "600519", as_of=date(2026, 9, 4)
        )

    assert (primary.calls, fallback.calls) == (1, 0)
    assert list(tmp_path.glob("*.json")) == []


def test_service_does_not_fallback_after_akshare_missing_core_schema(tmp_path, bars):
    from stock_daily_report.providers.akshare import AkShareMarketDataProvider
    from stock_daily_report.providers.base import ProviderDataError

    primary = AkShareMarketDataProvider(
        fetcher=lambda **_: [{"日期": "2026-09-04", "开盘": 100.0}]
    )
    fallback = RecordingProvider("fallback", response=bars)

    with pytest.raises(ProviderDataError) as error:
        _service(primary, fallback, tmp_path).fetch(
            "600519", as_of=date(2026, 9, 4)
        )

    assert error.value.provider == "akshare"
    assert error.value.code == "provider_schema_invalid"
    assert "Missing required source fields" in error.value.detail
    assert (primary.name, fallback.calls) == ("akshare", 0)


def test_service_qualities_invalid_akshare_records_without_fallback(tmp_path, bars):
    from stock_daily_report.providers.akshare import AkShareMarketDataProvider
    from stock_daily_report.providers.service import DataQualityError

    primary = AkShareMarketDataProvider(
        fetcher=lambda **_: [
            {
                "日期": "2026-09-04",
                "开盘": 100.0,
                "最高": 99.0,
                "最低": -1.0,
                "收盘": 101.0,
                "成交量": 1_000.0,
                "成交额": 101_000.0,
                "换手率": 0.1,
            },
            {
                "日期": "2026-09-04",
                "开盘": 100.0,
                "最高": 99.0,
                "最低": 100.0,
                "收盘": 101.0,
                "成交量": 1_000.0,
                "成交额": 101_000.0,
                "换手率": 0.1,
            },
        ]
    )
    fallback = RecordingProvider("fallback", response=bars)

    with pytest.raises(DataQualityError) as error:
        _service(primary, fallback, tmp_path).fetch(
            "600519", as_of=date(2026, 9, 4)
        )

    assert error.value.provider == "akshare"
    assert set(error.value.quality.issue_codes) >= {
        "negative_ohlc",
        "high_lt_low",
        "close_outside_low_high",
    }
    assert fallback.calls == 0


def test_service_falls_back_after_akshare_network_availability_error(tmp_path, bars):
    from stock_daily_report.providers.akshare import AkShareMarketDataProvider

    primary = AkShareMarketDataProvider(
        fetcher=lambda **_: (_ for _ in ()).throw(OSError("offline"))
    )
    fallback = RecordingProvider("fallback", response=bars)

    fetched = _service(primary, fallback, tmp_path).fetch(
        "600519", as_of=date(2026, 9, 4)
    )

    assert fetched.provider_name == "fallback"
    assert (primary.name, fallback.calls) == ("akshare", 1)


def test_service_does_not_fallback_for_unexpected_primary_error(tmp_path, bars):
    primary = RecordingProvider("primary", error=RuntimeError("programming error"))
    fallback = RecordingProvider("fallback", response=bars)

    with pytest.raises(RuntimeError, match="programming error"):
        _service(primary, fallback, tmp_path).fetch(
            "600519", as_of=date(2026, 9, 4)
        )

    assert (primary.calls, fallback.calls) == (1, 0)


def test_service_reports_each_expected_provider_failure(tmp_path):
    from stock_daily_report.providers.base import ProviderAvailabilityError
    from stock_daily_report.providers.service import AllProvidersFailedError

    primary = RecordingProvider(
        "primary",
        error=ProviderAvailabilityError("primary", "network_error", "timeout"),
    )
    fallback = RecordingProvider(
        "fallback",
        error=ProviderAvailabilityError(
            "fallback", "upstream_service_error", "maintenance"
        ),
    )

    with pytest.raises(AllProvidersFailedError) as error:
        _service(primary, fallback, tmp_path).fetch(
            "600519", as_of=date(2026, 9, 4)
        )

    assert [(failure.provider, failure.code) for failure in error.value.failures] == [
        ("primary", "network_error"),
        ("fallback", "upstream_service_error"),
    ]
    assert "primary[network_error]" in str(error.value)
    assert "fallback[upstream_service_error]" in str(error.value)


def test_raw_cache_redacts_recursive_credentials_and_enforces_expiry(tmp_path):
    from stock_daily_report.providers.service import RawResponseCache

    current_time = datetime(2026, 9, 4, 8, tzinfo=UTC)
    cache = RawResponseCache(
        tmp_path,
        ttl_seconds=30,
        secrets=("configured-secret",),
        now=lambda: current_time,
    )
    response = {
        "api_key": "top-secret",
        "nested": {
            "authorization": "Bearer top-secret",
            "note": "configured-secret",
        },
        "items": [{"token": "token-value", "close": 101.0}],
    }

    cache.store("akshare", "600519", None, None, response)
    cached_path = next(tmp_path.glob("*.json"))

    serialized = cached_path.read_text(encoding="utf-8")
    assert "top-secret" not in serialized
    assert "configured-secret" not in serialized
    assert "token-value" not in serialized
    assert cache.load("akshare", "600519", None, None) == {
        "items": [{"close": 101.0}],
        "nested": {"note": "***REDACTED***"},
    }

    current_time += timedelta(seconds=31)
    assert cache.load("akshare", "600519", None, None) is None
    assert not cached_path.exists()


def test_service_reuses_valid_cached_response_until_it_expires(tmp_path, bars):
    current_time = datetime(2026, 9, 4, 8, tzinfo=UTC)
    primary = RecordingProvider("primary", response=bars)
    fallback = RecordingProvider("fallback", response=bars)
    service = _service(
        primary,
        fallback,
        tmp_path,
        now=lambda: current_time,
    )

    first = service.fetch("600519", as_of=date(2026, 9, 4))
    second = service.fetch("600519", as_of=date(2026, 9, 4))
    current_time += timedelta(seconds=31)
    third = service.fetch("600519", as_of=date(2026, 9, 4))

    assert (first.from_cache, second.from_cache, third.from_cache) == (
        False,
        True,
        False,
    )
    assert primary.calls == 2


def test_cache_restore_failure_is_atomic_and_retains_recovery_state(
    tmp_path, bars, monkeypatch
):
    import stock_daily_report.providers.service as service_module
    from stock_daily_report.providers.service import CacheRollbackError

    primary = RecordingProvider("primary", response=bars)
    fallback = RecordingProvider("fallback", response=bars)
    service = _service(primary, fallback, tmp_path)
    service.fetch("600519", end=date(2026, 9, 4), as_of=date(2026, 9, 4))
    existing_path = service._cache._path_for(
        "primary", "600519", None, date(2026, 9, 4)
    )
    existing_bytes = existing_path.read_bytes()
    changed_bars = [
        bar.model_copy(update={"close": bar.close + 1.0}) for bar in bars
    ]
    primary.response = changed_bars
    monkeypatch.setattr(service._cache, "load", lambda *_args, **_kwargs: None)
    service.fetch(
        "600519", end=date(2026, 9, 4), as_of=date(2026, 9, 4), defer_cache=True
    )
    service.fetch(
        "000001", end=date(2026, 9, 4), as_of=date(2026, 9, 4), defer_cache=True
    )
    new_path = service._cache._path_for(
        "primary", "000001", None, date(2026, 9, 4)
    )
    original_replace = service_module.os.replace
    existing_replacements = 0

    def fail_cache_restore(source, destination):
        nonlocal existing_replacements
        destination = Path(destination)
        if destination == existing_path:
            existing_replacements += 1
            if existing_replacements >= 2:
                raise OSError("injected cache restore failure")
        if destination == new_path:
            raise OSError("injected cache commit failure")
        return original_replace(source, destination)

    monkeypatch.setattr(service_module.os, "replace", fail_cache_restore)

    with pytest.raises(CacheRollbackError, match="recovery"):
        service.commit_staged_cache_writes()

    recovery_directories = list(tmp_path.glob(".cache-recovery-*"))
    assert len(recovery_directories) == 1
    recovery_directory = recovery_directories[0]
    assert (recovery_directory / "manifest.json").exists()
    assert (recovery_directory / existing_path.name).read_bytes() == existing_bytes
    assert existing_path.read_bytes() != b""


def test_cache_batch_recovery_survives_hard_exit_after_first_write(tmp_path):
    from stock_daily_report.providers.service import RawResponseCache

    cache = RawResponseCache(tmp_path, ttl_seconds=30)
    existing_path = cache._path_for("primary", "600519", None, date(2026, 9, 4))
    new_path = cache._path_for("primary", "000001", None, date(2026, 9, 4))
    existing_path.write_bytes(b"old cache bytes")
    repo_root = Path(__file__).parents[1]
    child = f"""
import os
from datetime import date
from pathlib import Path
from stock_daily_report.providers.service import MarketDataService

class Provider:
    def get_daily_bars(self, code, *, start=None, end=None):
        return []

root = Path({str(tmp_path)!r})
service = MarketDataService(
    {{"primary": Provider(), "fallback": Provider()}},
    primary_provider="primary",
    fallback_provider="fallback",
    cache_directory=root,
    cache_ttl_seconds=30,
)
service._staged_cache_writes = [
    ("primary", "600519", None, date(2026, 9, 4), [{{"close": 2}}]),
    ("primary", "000001", None, date(2026, 9, 4), [{{"close": 3}}]),
]
original_store = service._cache._store_unlocked
calls = 0

def interrupt_after_first_write(*args):
    global calls
    calls += 1
    original_store(*args)
    if calls == 1:
        os._exit(74)

service._cache._store_unlocked = interrupt_after_first_write
service.commit_staged_cache_writes()
"""
    result = subprocess.run(
        [sys.executable, "-c", child],
        cwd=repo_root,
        env={
            **os.environ,
            "PYTHONPATH": str(repo_root / "src"),
        },
        check=False,
    )
    assert result.returncode == 74
    assert existing_path.read_bytes() != b"old cache bytes"
    assert not new_path.exists()
    assert list(tmp_path.glob(".cache-recovery-*"))

    recovery_cache = RawResponseCache(tmp_path, ttl_seconds=30)
    recovery_cache.recover_pending_manifests()

    assert existing_path.read_bytes() == b"old cache bytes"
    assert not new_path.exists()
    assert not list(tmp_path.glob(".cache-recovery-*"))


def test_cache_startup_discards_pre_mutation_recovery_state(tmp_path):
    from stock_daily_report.providers.service import RawResponseCache

    cache = RawResponseCache(tmp_path, ttl_seconds=30)
    existing_path = cache._path_for("primary", "600519", None, date(2026, 9, 4))
    existing_path.write_bytes(b"unchanged cache bytes")
    recovery_directory = tmp_path / ".cache-recovery-preparing"
    recovery_directory.mkdir()
    (recovery_directory / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "state": "preparing",
                "entries": [
                    {
                        "path": existing_path.name,
                        "present": False,
                        "size": None,
                        "sha256": None,
                        "status": "pending",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    recovery_cache = RawResponseCache(tmp_path, ttl_seconds=30)
    recovery_cache.recover_pending_manifests()

    assert existing_path.read_bytes() == b"unchanged cache bytes"
    assert not recovery_directory.exists()


def test_cache_recovery_streams_preimage_without_reading_it_all(
    tmp_path, monkeypatch
):
    from stock_daily_report.providers.service import RawResponseCache

    cache = RawResponseCache(tmp_path, ttl_seconds=30)
    target = cache._path_for("primary", "600519", None, date(2026, 9, 4))
    recovery = tmp_path / ".cache-recovery-stream"
    recovery.mkdir()
    preimage = recovery / target.name
    previous = b"streamed preimage"
    preimage.write_bytes(previous)
    (recovery / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "state": "ready",
                "entries": [
                    {
                        "path": target.name,
                        "present": True,
                        "size": len(previous),
                        "sha256": hashlib.sha256(previous).hexdigest(),
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    original_read_bytes = Path.read_bytes

    def reject_full_preimage_read(path):
        if path == preimage:
            raise AssertionError("recovery preimage was read into memory")
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", reject_full_preimage_read)

    recovery_cache = RawResponseCache(tmp_path, ttl_seconds=30)
    recovery_cache.recover_pending_manifests()

    assert target.read_bytes() == previous


def test_cache_replays_ready_recovery_manifest_on_startup(tmp_path):
    from stock_daily_report.providers.service import RawResponseCache

    cache = RawResponseCache(tmp_path, ttl_seconds=30)
    existing_path = cache._path_for("primary", "600519", None, date(2026, 9, 4))
    new_path = cache._path_for("primary", "000001", None, date(2026, 9, 4))
    existing_path.write_bytes(b"post-commit bytes")
    new_path.write_bytes(b"new entry")

    recovery_directory = tmp_path / ".cache-recovery-manual"
    recovery_directory.mkdir()
    previous_bytes = b"exact preimage bytes"
    (recovery_directory / existing_path.name).write_bytes(previous_bytes)
    (recovery_directory / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "state": "ready",
                "entries": [
                    {
                        "path": existing_path.name,
                        "present": True,
                        "size": len(previous_bytes),
                        "sha256": hashlib.sha256(previous_bytes).hexdigest(),
                    },
                    {
                        "path": new_path.name,
                        "present": False,
                        "size": None,
                        "sha256": None,
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    recovery_cache = RawResponseCache(tmp_path, ttl_seconds=30)
    recovery_cache.recover_pending_manifests()

    assert existing_path.read_bytes() == previous_bytes
    assert not new_path.exists()
    assert not recovery_directory.exists()


def test_cache_recovery_manifest_failure_is_explicit_and_retained(tmp_path):
    from stock_daily_report.providers.service import (
        CacheRollbackError,
        RawResponseCache,
    )

    recovery_directory = tmp_path / ".cache-recovery-manual"
    recovery_directory.mkdir()
    (recovery_directory / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "state": "ready",
                "entries": [
                    {
                        "path": "entry.json",
                        "present": True,
                        "size": 3,
                        "sha256": "not-a-real-digest",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    recovery_cache = RawResponseCache(tmp_path, ttl_seconds=30)
    with pytest.raises(CacheRollbackError, match="recovery"):
        recovery_cache.recover_pending_manifests()

    assert recovery_directory.exists()


def test_akshare_adapter_maps_complete_source_records_without_network():
    from stock_daily_report.providers.akshare import AkShareMarketDataProvider

    provider = AkShareMarketDataProvider(
        fetcher=lambda **_: [
            {
                "日期": "2026-09-04",
                "开盘": 100.0,
                "最高": 103.0,
                "最低": 99.0,
                "收盘": 101.0,
                "成交量": 1_000.0,
                "成交额": 101_000.0,
                "换手率": 0.1,
            }
        ],
        now=lambda: datetime(2026, 9, 4, 16, tzinfo=UTC),
    )

    normalized = provider.get_daily_bars("600519", start=date(2026, 9, 1))

    assert normalized[0] == {
        "trade_date": "2026-09-04",
        "close": 101.0,
        "provider_name": "akshare",
        "adjustment_mode": "qfq",
        "open": 100.0,
        "high": 103.0,
        "low": 99.0,
        "volume": 1_000.0,
        "amount": 101_000.0,
        "turnover_rate": 0.1,
        "source_timestamp": datetime(2026, 9, 4, 16, tzinfo=UTC),
    }


def test_akshare_adapter_distinguishes_invalid_data_from_availability_errors():
    from stock_daily_report.providers.akshare import AkShareMarketDataProvider
    from stock_daily_report.providers.base import (
        ProviderAvailabilityError,
        ProviderDataError,
    )

    malformed = AkShareMarketDataProvider(fetcher=lambda **_: [{"日期": "2026-09-04"}])
    unavailable = AkShareMarketDataProvider(
        fetcher=lambda **_: (_ for _ in ()).throw(OSError("offline"))
    )

    with pytest.raises(
        ProviderDataError, match=r"akshare\[provider_schema_invalid\]"
    ):
        malformed.get_daily_bars("600519")
    with pytest.raises(ProviderAvailabilityError, match=r"akshare\[network_error\]"):
        unavailable.get_daily_bars("600519")
