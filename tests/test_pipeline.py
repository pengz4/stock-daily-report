import hashlib
import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import stock_daily_report.cli as cli_module
import stock_daily_report.pipeline as pipeline_module
from stock_daily_report.models import DailyBar, Settings, Watchlist
from stock_daily_report.notify.base import (
    NotificationDeliveryError,
    NotificationOutcome,
)
from stock_daily_report.pipeline import (
    PipelineError,
    PipelineFailure,
    PublicationRollbackError,
    run_daily_report,
)
from stock_daily_report.providers.service import CacheRollbackError, MarketDataService
from stock_daily_report.quality.checks import DataQualitySettings
from stock_daily_report.snapshots import SnapshotError, load_snapshot


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


def test_successful_publication_commits_cache_after_all_outputs_exist(
    tmp_path, fixture_settings, monkeypatch
):
    service = MarketDataService(
        {
            "fixture": RecordingProvider({"600519": make_bars("600519")}),
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=tmp_path / "cache",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    observed: list[tuple[bool, bool, bool, bool]] = []
    original_commit = service.commit_staged_cache_writes

    def commit_after_publication():
        observed.append(
            (
                (tmp_path / "reports/2026-09-04/report.json").exists(),
                (tmp_path / "reports/2026-09-04/report.md").exists(),
                (tmp_path / "reports/2026-09-04/index.html").exists(),
                (tmp_path / "site/index.html").exists(),
            )
        )
        original_commit()

    monkeypatch.setattr(service, "commit_staged_cache_writes", commit_after_publication)

    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
        service=service,
        report_date=date(2026, 9, 4),
    )

    assert observed == [(True, True, True, True)]
    assert list((tmp_path / "cache").glob("*.json"))


def test_publication_completion_precedes_cache_commit(
    tmp_path, fixture_settings, monkeypatch
):
    service = MarketDataService(
        {
            "fixture": RecordingProvider({"600519": make_bars("600519")}),
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=tmp_path / "cache",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    events: list[str] = []
    original_complete = pipeline_module._PublicationTransaction.complete
    original_commit = service.commit_staged_cache_writes

    def record_complete(transaction):
        events.append("publication.complete")
        return original_complete(transaction)

    def record_commit():
        events.append("cache.commit")
        return original_commit()

    monkeypatch.setattr(
        pipeline_module._PublicationTransaction, "complete", record_complete
    )
    monkeypatch.setattr(service, "commit_staged_cache_writes", record_commit)

    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
        service=service,
        report_date=date(2026, 9, 4),
    )

    assert events == ["publication.complete", "cache.commit"]


def test_publication_completion_failure_does_not_commit_cache(
    tmp_path, fixture_settings, monkeypatch
):
    service = MarketDataService(
        {
            "fixture": RecordingProvider({"600519": make_bars("600519")}),
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=tmp_path / "cache",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    monkeypatch.setattr(
        pipeline_module._PublicationTransaction,
        "complete",
        lambda _transaction: (_ for _ in ()).throw(
            OSError("injected completion failure")
        ),
    )

    with pytest.raises(OSError, match="injected completion failure"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            service=service,
            report_date=date(2026, 9, 4),
        )

    assert not list((tmp_path / "cache").glob("*.json"))


def test_post_cache_publication_cleanup_failure_retains_committed_state(
    tmp_path, fixture_settings, monkeypatch
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=date(2026, 9, 4),
    )
    service = MarketDataService(
        {
            "fixture": RecordingProvider({"600519": make_bars("600519")}),
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=tmp_path / "cache",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    original_rmtree = pipeline_module.shutil.rmtree
    failed = False

    def fail_recovery_cleanup(path, *args, **kwargs):
        nonlocal failed
        if Path(path).name == "recovery" and not failed:
            failed = True
            raise OSError("injected post-cache cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(pipeline_module.shutil, "rmtree", fail_recovery_cleanup)

    with pytest.raises(OSError, match="injected post-cache cleanup failure"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            service=service,
            report_date=date(2026, 9, 4),
        )

    assert failed
    assert (tmp_path / "reports/2026-09-04/report.json").exists()
    assert list((tmp_path / "cache").glob("*.json"))
    assert list(tmp_path.glob(".publication-*/recovery"))


def test_cache_recovery_waits_for_publication_lock_before_replaying(
    tmp_path, fixture_settings
):
    from stock_daily_report.providers.service import RawResponseCache

    cache_directory = tmp_path / "cache"
    cache = RawResponseCache(cache_directory, ttl_seconds=30, recover_pending=False)
    target = cache._path_for("fixture", "600519", None, date(2026, 9, 4))
    cache_directory.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"new cache bytes")
    recovery = cache_directory / ".cache-recovery-pending"
    recovery.mkdir()
    previous = b"old cache bytes"
    (recovery / target.name).write_bytes(previous)
    (recovery / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "state": "ready",
                "publication_root": str(tmp_path.resolve()),
                "publication_owner_token": hashlib.sha256(
                    str(tmp_path.resolve()).encode("utf-8")
                ).hexdigest(),
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
    service = MarketDataService(
        {
            "fixture": RecordingProvider({"600519": make_bars("600519")}),
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=cache_directory,
        cache_ttl_seconds=30,
        quality_settings=DataQualitySettings(minimum_history_bars=60),
    )

    with pipeline_module._publication_lock(tmp_path, date(2026, 9, 4)):
        pipeline_module._recover_pending_publications_if_idle(
            tmp_path, date(2026, 9, 4), service=service
        )
        assert target.read_bytes() == b"new cache bytes"
        assert recovery.exists()

    pipeline_module._recover_pending_publications_if_idle(
        tmp_path, date(2026, 9, 4), service=service
    )

    assert target.read_bytes() == previous
    assert not recovery.exists()


def test_pending_cache_recovery_is_a_precondition_before_fetching(
    tmp_path, fixture_settings
):
    cache_directory = tmp_path / "cache"
    (cache_directory / ".cache-recovery-pending").mkdir(parents=True)
    provider = RecordingProvider({"600519": make_bars("600519")})
    service = MarketDataService(
        {
            "fixture": provider,
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=cache_directory,
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    service._staged_cache_writes.append(
        ("fixture", "staged", None, date(2026, 9, 4), [{"close": 1.0}])
    )

    lock_entered = threading.Event()
    release_lock = threading.Event()

    def hold_cache_lock():
        with service._cache._write_lock():
            lock_entered.set()
            release_lock.wait(timeout=5)

    lock_holder = threading.Thread(target=hold_cache_lock)
    lock_holder.start()
    assert lock_entered.wait(timeout=5)
    try:
        with pytest.raises(CacheRollbackError, match="recovery lock"):
            run_daily_report(
                fixture_settings,
                output_root=tmp_path,
                watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
                service=service,
                report_date=date(2026, 9, 4),
            )
    finally:
        release_lock.set()
        lock_holder.join(timeout=5)

    assert provider.calls == []
    assert service._staged_cache_writes == []
    assert (cache_directory / ".cache-recovery-pending").exists()


def test_cache_lock_is_held_through_publication_acknowledgement_and_cleanup(
    tmp_path, fixture_settings, monkeypatch
):
    service = MarketDataService(
        {
            "fixture": RecordingProvider({"600519": make_bars("600519")}),
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=tmp_path / "cache",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    writer_finished = threading.Event()
    writer_thread: threading.Thread | None = None
    original_commit = service.commit_staged_cache_writes

    def writer():
        service._cache.store(
            "fixture",
            "concurrent",
            None,
            date(2026, 9, 4),
            [{"close": 2.0}],
        )
        writer_finished.set()

    def observe_commit():
        nonlocal writer_thread
        original_commit()
        writer_thread = threading.Thread(target=writer)
        writer_thread.start()
        assert not writer_finished.wait(timeout=0.2)

    monkeypatch.setattr(service, "commit_staged_cache_writes", observe_commit)
    try:
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            service=service,
            report_date=date(2026, 9, 4),
        )
    finally:
        if writer_thread is not None:
            writer_thread.join(timeout=5)

    assert writer_finished.is_set()
    assert service._cache.load(
        "fixture", "concurrent", None, date(2026, 9, 4)
    ) == [{"close": 2.0}]


def test_cache_snapshot_lock_blocks_recovery_until_report_is_published(
    tmp_path, fixture_settings, monkeypatch
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    provider = RecordingProvider({"600519": make_bars("600519")})
    service = MarketDataService(
        {
            "fixture": provider,
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=tmp_path / "cache",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    service._cache.store(
        "fixture",
        "600519",
        None,
        date(2026, 9, 4),
        [bar.model_dump(mode="json") for bar in make_bars("600519")],
    )
    load_returned = threading.Event()
    recovery_attempted = threading.Event()
    recovery_acquired: list[bool] = []
    original_load = service._cache.load

    def observe_load(*args, **kwargs):
        result = original_load(*args, **kwargs)
        load_returned.set()
        assert recovery_attempted.wait(timeout=5)
        return result

    def attempt_recovery():
        assert load_returned.wait(timeout=5)
        with service._cache.transaction_lock(nonblocking=True) as acquired:
            recovery_acquired.append(acquired)
        recovery_attempted.set()

    monkeypatch.setattr(service._cache, "load", observe_load)
    recovery_thread = threading.Thread(target=attempt_recovery)
    recovery_thread.start()
    outputs = run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        service=service,
        report_date=date(2026, 9, 4),
    )
    recovery_thread.join(timeout=5)

    assert not recovery_thread.is_alive()
    assert recovery_acquired == [False]
    assert outputs.report.metadata.report_date == date(2026, 9, 4)


def test_cache_recovery_rejects_cross_output_root_without_rollback(
    tmp_path, fixture_settings
):
    shared_cache = tmp_path / "shared-cache"
    root_a = tmp_path / "output-a"
    root_b = tmp_path / "output-b"
    root_a.mkdir()
    root_b.mkdir()
    publication_manifest = root_a / ".publication-owner" / "manifest.json"
    publication_manifest.parent.mkdir()
    publication_manifest.write_text(
        json.dumps({"state": "committed"}), encoding="utf-8"
    )
    service_a = MarketDataService(
        {
            "fixture": RecordingProvider({"600519": make_bars("600519")}),
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=shared_cache,
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(minimum_history_bars=60),
    )
    service_a.fetch(
        "600519",
        end=date(2026, 9, 4),
        as_of=date(2026, 9, 4),
        defer_cache=True,
    )
    service_a.set_publication_recovery_context(
        publication_manifest, publication_root=root_a
    )
    service_a.commit_staged_cache_writes()
    cache_path = service_a._cache._path_for(
        "fixture", "600519", None, date(2026, 9, 4)
    )
    recovery_paths = list(shared_cache.glob(".cache-recovery-*"))
    assert cache_path.exists()
    assert len(recovery_paths) == 1
    recovery_manifest = json.loads(
        (recovery_paths[0] / "manifest.json").read_text(encoding="utf-8")
    )
    assert recovery_manifest["publication_owner_token"]

    provider_b = RecordingProvider({"600519": make_bars("600519")})
    service_b = MarketDataService(
        {
            "fixture": provider_b,
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=shared_cache,
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(minimum_history_bars=60),
    )

    with pytest.raises(CacheRollbackError, match="another publication root"):
        run_daily_report(
            fixture_settings,
            output_root=root_b,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            service=service_b,
            report_date=date(2026, 9, 4),
        )

    assert provider_b.calls == []
    assert cache_path.exists()
    assert recovery_paths[0].exists()


def test_restart_recovery_uses_absolute_publication_paths_from_another_cwd(
    tmp_path, fixture_settings
):
    creator_cwd = tmp_path / "creator"
    creator_cwd.mkdir()
    repo_root = Path(__file__).parents[1]
    child = f"""
import os
from datetime import date
from pathlib import Path
from datetime import UTC, datetime, timedelta

from stock_daily_report.models import DailyBar, Settings, Watchlist
from stock_daily_report.pipeline import run_daily_report
from stock_daily_report.providers.service import MarketDataService

creator_cwd = Path({str(creator_cwd)!r})
os.chdir(creator_cwd)
settings = Settings(
    rule_version={{"name": "simplified", "version": "v1"}},
    notifications={{"enabled_channels": []}},
    market_data={{
        "primary_provider": "fixture",
        "fallback_provider": "akshare",
        "minimum_history_bars": 60,
        "max_completed_trading_day_lag": 1,
    }},
    risk_rules={{
        "rule_version": "risk-v1",
        "high_realized_volatility20": 0.45,
        "overextension_ma20_distance": 0.20,
        "large_drawdown60": -0.20,
        "adverse_volume_ratio20": 0.50,
        "minimum_history_bars": 61,
    }},
)

def fail_after_publication_commit(self):
    raise OSError("simulated restart")

MarketDataService.finalize_staged_cache_commit = fail_after_publication_commit

class FixtureProvider:
    name = "fixture"

    def get_daily_bars(self, code, *, start=None, end=None):
        first_day = date(2026, 6, 17)
        return [
            DailyBar(
                trade_date=first_day + timedelta(days=index),
                open=100.0 + index,
                high=102.0 + index,
                low=99.0 + index,
                close=101.0 + index,
                volume=1000.0 + index,
                amount=(101.0 + index) * 1000.0,
                turnover_rate=0.1,
                adjustment_mode="qfq",
                provider_name="fixture",
                source_timestamp=datetime(2026, 9, 4, 8, tzinfo=UTC),
            )
            for index in range(80)
        ]

class UnusedProvider:
    name = "akshare"

run_daily_report(
    settings,
    output_root=Path("../published"),
    watchlist=Watchlist(stocks=[{{"code": "600519", "name": "one"}}]),
    providers={{"fixture": FixtureProvider(), "akshare": UnusedProvider()}},
    report_date=date(2026, 9, 4),
)
"""
    result = subprocess.run(
        [sys.executable, "-c", child],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(repo_root / "src")},
        check=False,
    )
    assert result.returncode != 0

    published = tmp_path / "published"
    recovery_manifest = next(
        (published / ".cache/stock-daily-report").glob(".cache-recovery-*")
    ) / "manifest.json"
    document = json.loads(recovery_manifest.read_text(encoding="utf-8"))
    assert Path(document["publication_manifest"]).is_absolute()
    assert Path(document["publication_root"]) == published.resolve()

    restarted = MarketDataService(
        {
            "fixture": RecordingProvider({"600519": make_bars("600519")}),
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=published / ".cache/stock-daily-report",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    pipeline_module._recover_pending_publications_if_idle(
        published, date(2026, 9, 4), service=restarted
    )

    cache_path = restarted._cache._path_for(
        "fixture", "600519", None, date(2026, 9, 4)
    )
    assert cache_path.exists()
    assert not list(published.glob(".publication-*"))


def test_interrupted_cleanup_retries_from_cleanup_journal(
    tmp_path, fixture_settings, monkeypatch
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    original_rmtree = pipeline_module.shutil.rmtree
    interrupted = False

    def interrupt_after_recovery_cleanup(path, *args, **kwargs):
        nonlocal interrupted
        path = Path(path)
        if path.name == "recovery" and not interrupted:
            original_rmtree(path, *args, **kwargs)
            transaction_root = path.parent
            journal = transaction_root / "cleanup.json"
            assert json.loads(journal.read_text(encoding="utf-8"))["state"] == "cleaning"
            (transaction_root / "manifest.json").unlink()
            interrupted = True
            raise KeyboardInterrupt
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(
        pipeline_module.shutil, "rmtree", interrupt_after_recovery_cleanup
    )
    with pytest.raises(KeyboardInterrupt):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            provider=RecordingProvider({"600519": make_bars("600519")}),
            report_date=date(2026, 9, 4),
        )

    assert interrupted
    assert list(tmp_path.glob(".publication-*/cleanup.json"))
    monkeypatch.undo()

    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=date(2026, 9, 4),
    )

    assert not list(tmp_path.glob(".publication-*"))


def test_failed_rerun_preserves_existing_report_and_site_bytes(
    tmp_path, fixture_settings
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=date(2026, 9, 4),
    )
    report_paths = [
        tmp_path / "reports/2026-09-04/report.json",
        tmp_path / "reports/2026-09-04/report.md",
        tmp_path / "reports/2026-09-04/index.html",
        tmp_path / "site/index.html",
    ]
    before = {path: path.read_bytes() for path in report_paths}

    with pytest.raises(PipelineError):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            provider=RecordingProvider(
                {"600519": make_bars("600519")}, fail_code="600519"
            ),
            report_date=date(2026, 9, 4),
        )

    assert {path: path.read_bytes() for path in report_paths} == before


def test_snapshot_conflict_is_a_typed_pipeline_failure_and_preserves_outputs(
    tmp_path, fixture_settings
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    original_bars = make_bars("600519")
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=RecordingProvider({"600519": original_bars}),
        report_date=date(2026, 9, 4),
    )
    report_paths = [
        tmp_path / "reports/2026-09-04/report.json",
        tmp_path / "reports/2026-09-04/report.md",
        tmp_path / "reports/2026-09-04/index.html",
        tmp_path / "site/index.html",
    ]
    before = {path: path.read_bytes() for path in report_paths}
    changed_bars = list(original_bars)
    changed_bars[-1] = changed_bars[-1].model_copy(update={"close": 1.0, "low": 1.0})

    with pytest.raises(PipelineError, match="snapshot_conflict"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            provider=RecordingProvider({"600519": changed_bars}),
            report_date=date(2026, 9, 4),
        )

    assert {path: path.read_bytes() for path in report_paths} == before


def test_concurrent_conflicting_runs_publish_only_one_immutable_result(
    tmp_path, fixture_settings, monkeypatch
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    original_bars = make_bars("600519")
    changed_bars = list(original_bars)
    changed_bars[-1] = changed_bars[-1].model_copy(
        update={"close": 1.0, "low": 1.0}
    )
    first_resolved = threading.Event()
    second_resolved = threading.Event()
    release_first = threading.Event()
    resolution_lock = threading.Lock()
    resolution_count = 0
    original_resolve = pipeline_module._resolve_snapshot_for_publication

    def pause_after_first_resolution(snapshot_target, staged_snapshot):
        nonlocal resolution_count
        resolved = original_resolve(snapshot_target, staged_snapshot)
        with resolution_lock:
            resolution_count += 1
            is_first = resolution_count == 1
        if is_first:
            first_resolved.set()
            assert release_first.wait(timeout=5)
        else:
            second_resolved.set()
        return resolved

    monkeypatch.setattr(
        pipeline_module,
        "_resolve_snapshot_for_publication",
        pause_after_first_resolution,
    )
    outcomes: list[object] = []
    outcomes_lock = threading.Lock()

    def run(bars):
        try:
            outcome = run_daily_report(
                fixture_settings,
                output_root=tmp_path,
                watchlist=watchlist,
                provider=RecordingProvider({"600519": bars}),
                report_date=date(2026, 9, 4),
                now=lambda: datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
            )
        except PipelineError as error:
            outcome = error
        with outcomes_lock:
            outcomes.append(outcome)

    first = threading.Thread(target=run, args=(original_bars,))
    second = threading.Thread(target=run, args=(changed_bars,))
    first.start()
    assert first_resolved.wait(timeout=5)
    second.start()
    second_resolved.wait(timeout=1)
    time.sleep(0.05)
    release_first.set()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    successes = [outcome for outcome in outcomes if not isinstance(outcome, Exception)]
    failures = [outcome for outcome in outcomes if isinstance(outcome, PipelineError)]
    assert len(successes) == 1
    assert len(failures) == 1
    assert failures[0].failures[0].code == "snapshot_conflict"
    snapshot = load_snapshot(tmp_path / "snapshots/2026-09-04/input.json")
    report_document = json.loads(
        (tmp_path / "reports/2026-09-04/report.json").read_text(encoding="utf-8")
    )
    assert report_document["metadata"]["snapshot_hash"] == snapshot.content_hash


def test_existing_stylesheet_is_published_without_touching_unrelated_site_files(
    tmp_path, fixture_settings
):
    site_directory = tmp_path / "site"
    site_directory.mkdir()
    stylesheet = site_directory / "styles.css"
    stylesheet.write_bytes(b"old stylesheet")
    unrelated = site_directory / "favicon.ico"
    unrelated.write_bytes(b"unrelated")

    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=date(2026, 9, 4),
    )

    assert stylesheet.read_bytes() == (
        pipeline_module._project_root() / "site" / "styles.css"
    ).read_bytes()
    assert unrelated.read_bytes() == b"unrelated"


def test_stylesheet_is_restored_when_publication_finalize_fails(
    tmp_path, fixture_settings, monkeypatch
):
    site_directory = tmp_path / "site"
    site_directory.mkdir()
    stylesheet = site_directory / "styles.css"
    stylesheet.write_bytes(b"existing stylesheet")

    def fail_finalize(transaction):
        raise OSError("injected finalize failure")

    monkeypatch.setattr(
        pipeline_module._PublicationTransaction, "finalize", fail_finalize
    )
    with pytest.raises(OSError, match="injected finalize failure"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            provider=RecordingProvider({"600519": make_bars("600519")}),
            report_date=date(2026, 9, 4),
        )

    assert stylesheet.read_bytes() == b"existing stylesheet"
    monkeypatch.undo()
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=date(2026, 9, 4),
    )


def test_date_lock_file_remains_stable_after_failed_publication(
    tmp_path, fixture_settings, monkeypatch
):
    monkeypatch.setattr(
        pipeline_module,
        "render_html",
        lambda _report: (_ for _ in ()).throw(OSError("injected render failure")),
    )

    with pytest.raises(OSError, match="injected render failure"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            provider=RecordingProvider({"600519": make_bars("600519")}),
            report_date=date(2026, 9, 4),
        )

    lock_path = tmp_path / "snapshots/2026-09-04/.input.lock"
    assert lock_path.exists()
    lock_inode = lock_path.stat().st_ino

    monkeypatch.undo()
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=date(2026, 9, 4),
    )

    assert lock_path.stat().st_ino == lock_inode


def test_date_lock_file_remains_stable_after_prepublication_failure(
    tmp_path, fixture_settings
):
    failing_provider = RecordingProvider(
        {"600519": make_bars("600519")}, fail_code="600519"
    )
    service = MarketDataService(
        {
            "fixture": failing_provider,
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=tmp_path / "cache",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )

    with pytest.raises(PipelineError):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            service=service,
            report_date=date(2026, 9, 4),
        )

    lock_path = tmp_path / "snapshots/2026-09-04/.input.lock"
    assert lock_path.exists()
    lock_inode = lock_path.stat().st_ino

    failing_provider.fail_code = None
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
        service=service,
        report_date=date(2026, 9, 4),
    )

    assert lock_path.stat().st_ino == lock_inode


def test_snapshots_root_is_fsynced_after_snapshot_publish_before_cache_commit(
    tmp_path, fixture_settings, monkeypatch
):
    service = MarketDataService(
        {
            "fixture": RecordingProvider({"600519": make_bars("600519")}),
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=tmp_path / "cache",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    snapshots_root = tmp_path / "snapshots"
    snapshot_target = snapshots_root / "2026-09-04" / "input.json"
    events: list[str] = []
    original_fsync_directory = pipeline_module._fsync_directory
    original_replace = pipeline_module.os.replace
    original_commit = service.commit_staged_cache_writes

    def record_fsync(directory):
        if Path(directory) == snapshots_root:
            events.append("snapshots-fsync")
        return original_fsync_directory(directory)

    def record_replace(source, destination):
        result = original_replace(source, destination)
        if Path(destination) == snapshot_target:
            events.append("snapshot-rename")
        return result

    def record_commit():
        rename_index = events.index("snapshot-rename")
        assert "snapshots-fsync" in events[rename_index + 1 :]
        events.append("cache-commit")
        original_commit()

    monkeypatch.setattr(pipeline_module, "_fsync_directory", record_fsync)
    monkeypatch.setattr(pipeline_module.os, "replace", record_replace)
    monkeypatch.setattr(service, "commit_staged_cache_writes", record_commit)

    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
        service=service,
        report_date=date(2026, 9, 4),
    )

    assert events.index("snapshot-rename") < events.index("cache-commit")


def test_publication_transaction_parent_is_fsynced_before_commit(
    tmp_path, fixture_settings, monkeypatch
):
    events: list[tuple[str, Path | None]] = []
    original_fsync_directory = pipeline_module._fsync_directory
    original_commit = pipeline_module._PublicationTransaction.commit

    def record_fsync(directory):
        path = Path(directory)
        if path == tmp_path:
            events.append(("output-root-fsync", path))
        elif path.parent == tmp_path and path.name.startswith(".publication-"):
            events.append(("transaction-fsync", path))
        return original_fsync_directory(directory)

    def record_commit(transaction):
        assert any(
            event == "output-root-fsync" for event, _ in events
        ), "output root was not fsynced before commit"
        assert any(
            event == "transaction-fsync" for event, _ in events
        ), "transaction directory was not fsynced before commit"
        events.append(("commit", transaction.transaction_root))
        return original_commit(transaction)

    monkeypatch.setattr(pipeline_module, "_fsync_directory", record_fsync)
    monkeypatch.setattr(
        pipeline_module._PublicationTransaction, "commit", record_commit
    )

    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=date(2026, 9, 4),
    )

    assert any(event == "commit" for event, _ in events)


def test_publication_transaction_parent_fsync_failure_retains_journal(
    tmp_path, fixture_settings, monkeypatch
):
    original_fsync_directory = pipeline_module._fsync_directory

    def fail_manifest_parent_fsync(directory):
        path = Path(directory)
        if path == tmp_path and any(
            (candidate / "manifest.json").exists()
            for candidate in tmp_path.glob(".publication-*")
        ):
            raise OSError("injected transaction parent fsync failure")
        return original_fsync_directory(directory)

    monkeypatch.setattr(
        pipeline_module, "_fsync_directory", fail_manifest_parent_fsync
    )

    with pytest.raises(OSError, match="transaction parent fsync"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            provider=RecordingProvider({"600519": make_bars("600519")}),
            report_date=date(2026, 9, 4),
        )

    transaction_paths = list(tmp_path.glob(".publication-*"))
    assert len(transaction_paths) == 1
    assert (transaction_paths[0] / "manifest.json").exists()


def test_publication_transaction_creation_fsync_failure_retains_evidence(
    tmp_path, fixture_settings, monkeypatch
):
    original_fsync_directory = pipeline_module._fsync_directory

    def fail_transaction_directory_fsync(directory):
        path = Path(directory)
        if path.parent == tmp_path and path.name.startswith(".publication-"):
            raise OSError("injected transaction directory fsync failure")
        return original_fsync_directory(directory)

    monkeypatch.setattr(
        pipeline_module, "_fsync_directory", fail_transaction_directory_fsync
    )

    with pytest.raises(OSError, match="transaction directory fsync"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            provider=RecordingProvider({"600519": make_bars("600519")}),
            report_date=date(2026, 9, 4),
        )

    transaction_paths = list(tmp_path.glob(".publication-*"))
    assert len(transaction_paths) == 1


def test_concurrent_different_dates_preserve_all_site_index_entries(
    tmp_path, fixture_settings, monkeypatch
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    first_publish_entered = threading.Event()
    release_first_publish = threading.Event()
    second_publish_finished = threading.Event()
    original_publish = pipeline_module._PublicationTransaction.publish

    def pause_first_publish(transaction):
        if transaction.report_date == date(2026, 9, 4):
            first_publish_entered.set()
            assert release_first_publish.wait(timeout=5)
        result = original_publish(transaction)
        if transaction.report_date == date(2026, 9, 5):
            second_publish_finished.set()
        return result

    monkeypatch.setattr(
        pipeline_module._PublicationTransaction, "publish", pause_first_publish
    )
    second_fetched = threading.Event()
    outcomes: list[object] = []
    outcomes_lock = threading.Lock()

    def run(report_date, *, fetched_event=None):
        provider = RecordingProvider(
            {"600519": make_bars("600519")},
            on_call=lambda _code: fetched_event.set() if fetched_event else None,
        )
        try:
            outcome = run_daily_report(
                fixture_settings,
                output_root=tmp_path,
                watchlist=watchlist,
                provider=provider,
                report_date=report_date,
            )
        except (AssertionError, OSError, PipelineError) as error:
            outcome = error
        with outcomes_lock:
            outcomes.append(outcome)

    first = threading.Thread(target=run, args=(date(2026, 9, 4),))
    first.start()
    assert first_publish_entered.wait(timeout=5)

    second = threading.Thread(
        target=run,
        args=(date(2026, 9, 5),),
        kwargs={"fetched_event": second_fetched},
    )
    second.start()
    assert second_fetched.wait(timeout=5)
    second_publish_finished.wait(timeout=1)
    release_first_publish.set()

    first.join(timeout=5)
    second.join(timeout=5)
    assert not first.is_alive()
    assert not second.is_alive()
    assert not [outcome for outcome in outcomes if isinstance(outcome, Exception)]

    site_index = (tmp_path / "site/index.html").read_text(encoding="utf-8")
    assert "../reports/2026-09-04/index.html" in site_index
    assert "../reports/2026-09-05/index.html" in site_index


def test_opportunistic_recovery_waits_for_each_transaction_date_lock(
    tmp_path, monkeypatch
):
    transaction_root = tmp_path / ".publication-date-lock-orphan"
    staged_report = transaction_root / "reports/2026-09-04"
    staged_report.mkdir(parents=True)
    transaction = pipeline_module._PublicationTransaction(
        root=tmp_path,
        report_date=date(2026, 9, 4),
        staged_report_dir=staged_report,
        staged_snapshot_path=None,
        staged_site_index=transaction_root / "site/index.html",
        staged_styles_path=None,
    )
    assert transaction.transaction_root == transaction_root

    marker = tmp_path / "date-lock-held"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"""
import fcntl
import time
from pathlib import Path

lock_path = Path({str(tmp_path / "snapshots/2026-09-04/.input.lock")!r})
lock_path.parent.mkdir(parents=True, exist_ok=True)
with lock_path.open("a", encoding="utf-8") as lock_file:
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    Path({str(marker)!r}).write_text("held", encoding="utf-8")
    time.sleep(5)
""",
        ],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    try:
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.05)
        assert marker.exists()
        recovered = []
        monkeypatch.setattr(
            pipeline_module,
            "_recover_pending_publications",
            lambda *_args, **_kwargs: recovered.append(True),
        )

        pipeline_module._recover_pending_publications_if_idle(
            tmp_path,
            date(2026, 9, 5),
        )

        assert recovered == []
        assert transaction_root.exists()
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_manifestless_recovery_does_not_probe_requested_date_lock(tmp_path):
    transaction_root = tmp_path / ".publication-pre-manifest"
    (transaction_root / "reports").mkdir(parents=True)
    marker = tmp_path / "requested-date-lock-held"
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            f"""
import fcntl
import time
from pathlib import Path

lock_path = Path({str(tmp_path / "snapshots/2026-09-05/.input.lock")!r})
lock_path.parent.mkdir(parents=True, exist_ok=True)
with lock_path.open("a", encoding="utf-8") as lock_file:
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
    Path({str(marker)!r}).write_text("held", encoding="utf-8")
    time.sleep(5)
""",
        ],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
    )
    try:
        for _ in range(100):
            if marker.exists():
                break
            time.sleep(0.05)
        assert marker.exists()

        pipeline_module._recover_pending_publications_if_idle(
            tmp_path,
            date(2026, 9, 5),
        )

        assert not transaction_root.exists()
    finally:
        holder.terminate()
        holder.wait(timeout=5)


def test_later_date_recovers_older_orphan_before_touching_shared_site(
    tmp_path, fixture_settings
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=date(2026, 9, 4),
    )
    orphan = tmp_path / ".publication-multi-date-orphan"
    staged_report = orphan / "reports/2026-09-04"
    staged_report.mkdir(parents=True)
    for name in ("report.json", "report.md", "index.html"):
        (staged_report / name).write_text("new", encoding="utf-8")
    staged_site = orphan / "site"
    staged_site.mkdir()
    (staged_site / "index.html").write_text("new", encoding="utf-8")
    repo_root = Path(__file__).parents[1]
    child = f"""
import os
from datetime import date
from pathlib import Path
import stock_daily_report.pipeline as pipeline

root = Path({str(tmp_path)!r})
transaction_root = root / ".publication-multi-date-orphan"
transaction = pipeline._PublicationTransaction(
    root=root,
    report_date=date(2026, 9, 4),
    staged_report_dir=transaction_root / "reports/2026-09-04",
    staged_snapshot_path=None,
    staged_site_index=transaction_root / "site/index.html",
    staged_styles_path=None,
)
original_replace = pipeline.os.replace

def hard_exit_after_site_backup(source, destination):
    original_replace(source, destination)
    if Path(destination) == transaction.backup_site_index:
        os._exit(75)

pipeline.os.replace = hard_exit_after_site_backup
transaction.publish()
"""
    result = subprocess.run(
        [sys.executable, "-c", child],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(repo_root / "src")},
        check=False,
    )
    assert result.returncode == 75
    old_date_lock_directory = tmp_path / "snapshots/2026-09-04"
    with pipeline_module._snapshot_write_lock(old_date_lock_directory):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            provider=RecordingProvider({"600519": make_bars("600519")}),
            report_date=date(2026, 9, 5),
        )

    assert not list(tmp_path.glob(".publication-*"))
    site_index = (tmp_path / "site/index.html").read_text(encoding="utf-8")
    assert "../reports/2026-09-05/index.html" in site_index


def test_publication_fsync_failure_retains_recovery_artifacts(
    tmp_path, fixture_settings, monkeypatch
):
    def fail_site_directory_fsync(directory):
        if Path(directory) == tmp_path / "site":
            raise OSError("injected directory fsync failure")
        return original_fsync_directory(directory)

    original_fsync_directory = pipeline_module._fsync_directory
    monkeypatch.setattr(
        pipeline_module, "_fsync_directory", fail_site_directory_fsync
    )

    with pytest.raises(PublicationRollbackError, match="rollback"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            provider=RecordingProvider({"600519": make_bars("600519")}),
            report_date=date(2026, 9, 4),
        )

    recovery_roots = list(tmp_path.glob(".publication-*/backups"))
    assert recovery_roots


def test_failed_date_publication_cannot_rollback_another_date(
    tmp_path, fixture_settings, monkeypatch
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    first_finalize_entered = threading.Event()
    second_started = threading.Event()
    second_published = threading.Event()
    original_finalize = pipeline_module._PublicationTransaction.finalize
    original_publish = pipeline_module._PublicationTransaction.publish

    def track_publish(transaction):
        result = original_publish(transaction)
        if transaction.report_date == date(2026, 9, 5):
            second_published.set()
        return result

    def fail_first_finalize(transaction):
        if transaction.report_date == date(2026, 9, 4):
            first_finalize_entered.set()
            assert second_started.wait(timeout=5)
            second_published.wait(timeout=1)
            raise OSError("injected first-date finalize failure")
        return original_finalize(transaction)

    monkeypatch.setattr(
        pipeline_module._PublicationTransaction, "publish", track_publish
    )
    monkeypatch.setattr(
        pipeline_module._PublicationTransaction, "finalize", fail_first_finalize
    )
    outcomes: list[object] = []
    outcomes_lock = threading.Lock()

    def run(report_date):
        provider = RecordingProvider(
            {"600519": make_bars("600519")},
            on_call=lambda _code: (
                second_started.set() if report_date == date(2026, 9, 5) else None
            ),
        )
        try:
            outcome = run_daily_report(
                fixture_settings,
                output_root=tmp_path,
                watchlist=watchlist,
                provider=provider,
                report_date=report_date,
            )
        except (AssertionError, OSError, PipelineError) as error:
            outcome = error
        with outcomes_lock:
            outcomes.append(outcome)

    first = threading.Thread(target=run, args=(date(2026, 9, 4),))
    first.start()
    assert first_finalize_entered.wait(timeout=5)
    second = threading.Thread(target=run, args=(date(2026, 9, 5),))
    second.start()
    first.join(timeout=5)
    second.join(timeout=5)

    assert not first.is_alive()
    assert not second.is_alive()
    failures = [outcome for outcome in outcomes if isinstance(outcome, OSError)]
    assert len(failures) == 1
    assert str(failures[0]) == "injected first-date finalize failure"
    assert (tmp_path / "reports/2026-09-05/index.html").exists()
    site_index = (tmp_path / "site/index.html").read_text(encoding="utf-8")
    assert "../reports/2026-09-05/index.html" in site_index
    assert not (tmp_path / "reports/2026-09-04").exists()


@pytest.mark.parametrize("failure_kind", ["render", "site_index"])
def test_publication_failure_restores_old_state_and_discards_staged_cache(
    tmp_path, fixture_settings, monkeypatch, failure_kind
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=date(2026, 9, 4),
    )
    report_paths = [
        tmp_path / "reports/2026-09-04/report.json",
        tmp_path / "reports/2026-09-04/report.md",
        tmp_path / "reports/2026-09-04/index.html",
        tmp_path / "site/index.html",
    ]
    before = {path: path.read_bytes() for path in report_paths}
    cache_directory = tmp_path / "cache"
    service = MarketDataService(
        {
            "fixture": RecordingProvider({"600519": make_bars("600519")}),
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=cache_directory,
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    original_atomic_write = pipeline_module._atomic_write

    def fail_site_index(path, content):
        if path.parent.name == "site" and path.name == "index.html":
            raise OSError("injected site index failure")
        original_atomic_write(path, content)

    if failure_kind == "render":
        monkeypatch.setattr(
            pipeline_module,
            "render_html",
            lambda _report: (_ for _ in ()).throw(
                OSError("injected render failure")
            ),
        )
    else:
        monkeypatch.setattr(pipeline_module, "_atomic_write", fail_site_index)

    expected_message = f"injected {failure_kind.replace('_', ' ')} failure"
    with pytest.raises(OSError, match=expected_message):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            service=service,
            report_date=date(2026, 9, 4),
        )

    assert {path: path.read_bytes() for path in report_paths} == before
    assert not list(cache_directory.glob("*.json"))


def test_finalize_failure_does_not_commit_new_cache_entries(
    tmp_path, fixture_settings, monkeypatch
):
    service = MarketDataService(
        {
            "fixture": RecordingProvider({"600519": make_bars("600519")}),
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=tmp_path / "cache",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )

    monkeypatch.setattr(
        pipeline_module._PublicationTransaction,
        "finalize",
        lambda _transaction: (_ for _ in ()).throw(
            OSError("injected finalize failure")
        ),
    )
    with pytest.raises(OSError, match="injected finalize failure"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            service=service,
            report_date=date(2026, 9, 4),
        )

    assert not list((tmp_path / "cache").glob("*.json"))


def test_backup_cleanup_failure_restores_old_publication(
    tmp_path, fixture_settings, monkeypatch
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    report_date = date(2026, 9, 4)
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=report_date,
        now=lambda: datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
    )
    report_paths = [
        tmp_path / "reports/2026-09-04/report.json",
        tmp_path / "reports/2026-09-04/report.md",
        tmp_path / "reports/2026-09-04/index.html",
        tmp_path / "site/index.html",
    ]
    before = {path: path.read_bytes() for path in report_paths}
    original_rmtree = pipeline_module.shutil.rmtree
    cleanup_attempted = False

    def partially_remove_backups(path, *args, **kwargs):
        nonlocal cleanup_attempted
        path = Path(path)
        if path.name == "backups" and not cleanup_attempted:
            cleanup_attempted = True
            original_rmtree(path / "report")
            raise OSError("injected partial backup cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(pipeline_module.shutil, "rmtree", partially_remove_backups)

    with pytest.raises(OSError, match="injected partial backup cleanup failure"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            provider=RecordingProvider({"600519": make_bars("600519")}),
            report_date=report_date,
            now=lambda: datetime(2026, 9, 4, 10, 30, tzinfo=UTC),
        )

    assert cleanup_attempted
    assert {path: path.read_bytes() for path in report_paths} == before


def test_partial_backup_cleanup_restores_complete_report_from_recovery_copy(
    tmp_path, fixture_settings, monkeypatch
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    report_date = date(2026, 9, 4)
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=report_date,
    )
    report_paths = [
        tmp_path / "reports/2026-09-04/report.json",
        tmp_path / "reports/2026-09-04/report.md",
        tmp_path / "reports/2026-09-04/index.html",
        tmp_path / "site/index.html",
    ]
    before = {path: path.read_bytes() for path in report_paths}
    original_rmtree = pipeline_module.shutil.rmtree
    cleanup_attempted = False

    def partially_remove_report(path, *args, **kwargs):
        nonlocal cleanup_attempted
        path = Path(path)
        if path.name == "backups" and not cleanup_attempted:
            cleanup_attempted = True
            (path / "report" / "report.json").unlink()
            raise OSError("injected partial backup cleanup failure")
        return original_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(pipeline_module.shutil, "rmtree", partially_remove_report)

    with pytest.raises(OSError, match="injected partial backup cleanup failure"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            provider=RecordingProvider({"600519": make_bars("600519")}),
            report_date=report_date,
            now=lambda: datetime(2026, 9, 4, 10, 30, tzinfo=UTC),
        )

    assert cleanup_attempted
    assert {path: path.read_bytes() for path in report_paths} == before


def test_cache_commit_failure_restores_preimage_and_publication(
    tmp_path, fixture_settings, monkeypatch
):
    watchlist = Watchlist(
        stocks=[
            {"code": "000001", "name": "one"},
            {"code": "600519", "name": "two"},
        ]
    )
    cache_directory = tmp_path / "cache"
    service = MarketDataService(
        {
            "fixture": RecordingProvider(
                {"000001": make_bars("000001"), "600519": make_bars("600519")}
            ),
            "unused": RecordingProvider(
                {"000001": make_bars("000001"), "600519": make_bars("600519")}
            ),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=cache_directory,
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    unrelated_dir = tmp_path / "reports/2026-09-03"
    unrelated_dir.mkdir(parents=True)
    unrelated_files = {
        unrelated_dir / "report.json": b"unrelated json\n",
        unrelated_dir / "report.md": b"unrelated markdown\n",
        unrelated_dir / "index.html": b"unrelated html\n",
    }
    for path, content in unrelated_files.items():
        path.write_bytes(content)

    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        service=service,
        report_date=date(2026, 9, 4),
    )
    report_paths = [
        tmp_path / "reports/2026-09-04/report.json",
        tmp_path / "reports/2026-09-04/report.md",
        tmp_path / "reports/2026-09-04/index.html",
        tmp_path / "snapshots/2026-09-04/input.json",
        tmp_path / "site/index.html",
        tmp_path / "site/styles.css",
    ]
    before_reports = {path: path.read_bytes() for path in report_paths}
    before_unrelated = {path: path.read_bytes() for path in unrelated_files}
    existing_cache_path = service._cache._path_for(
        "fixture", "000001", None, date(2026, 9, 4)
    )
    new_cache_path = service._cache._path_for(
        "fixture", "600519", None, date(2026, 9, 4)
    )
    existing_cache_bytes = existing_cache_path.read_bytes()
    new_cache_path.unlink()

    monkeypatch.setattr(service._cache, "load", lambda *_args, **_kwargs: None)
    original_store = service._cache._store_unlocked
    store_calls = 0

    def fail_during_second_cache_write(
        provider_name, code, start, end, document
    ):
        nonlocal store_calls
        store_calls += 1
        if store_calls == 2:
            raise OSError("injected cache commit failure after one entry")
        original_store(provider_name, code, start, end, document)

    monkeypatch.setattr(
        service._cache, "_store_unlocked", fail_during_second_cache_write
    )

    with pytest.raises(PipelineError, match="cache commit failure"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            service=service,
            report_date=date(2026, 9, 4),
        )

    assert store_calls == 2
    assert existing_cache_path.read_bytes() == existing_cache_bytes
    assert not new_cache_path.exists()
    assert not list(
        cache_directory.glob(f".{new_cache_path.name}.*.tmp")
    )
    assert {path: path.read_bytes() for path in report_paths} == before_reports
    assert {path: path.read_bytes() for path in unrelated_files} == before_unrelated


def test_failed_rerun_never_deletes_unrelated_dated_reports(tmp_path, fixture_settings):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    bars = make_bars("600519")[:-1]
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=RecordingProvider({"600519": bars}),
        report_date=date(2026, 9, 3),
    )
    unrelated = tmp_path / "reports/2026-09-03"
    before = {path.name: path.read_bytes() for path in unrelated.iterdir()}

    with pytest.raises(PipelineError):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            provider=RecordingProvider({"600519": bars}, fail_code="600519"),
            report_date=date(2026, 9, 4),
        )

    assert {path.name: path.read_bytes() for path in unrelated.iterdir()} == before


def test_cli_handles_pipeline_failure_without_traceback(
    tmp_path, fixture_settings, monkeypatch, capsys
):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "rule_version:\n  name: simplified\n  version: v1\n"
        "notifications:\n  enabled_channels: []\n"
        "market_data:\n  primary_provider: fixture\n"
        "  fallback_provider: unused\n"
        "  minimum_history_bars: 60\n"
        "  max_completed_trading_day_lag: 1\n"
        "risk_rules:\n  rule_version: risk-v1\n"
        "  high_realized_volatility20: 0.45\n"
        "  overextension_ma20_distance: 0.20\n"
        "  large_drawdown60: -0.20\n"
        "  adverse_volume_ratio20: 0.50\n"
        "  minimum_history_bars: 61\n",
        encoding="utf-8",
    )
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text(
        "stocks:\n  - code: '600519'\n    name: one\n", encoding="utf-8"
    )
    monkeypatch.setattr(
        cli_module,
        "run_daily_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            PipelineError(
                [PipelineFailure("snapshot_conflict", "immutable snapshot conflict")]
            )
        ),
    )

    result = cli_module.main(
        [
            "daily",
            "--date",
            "2026-09-04",
            "--settings",
            str(settings_path),
            "--watchlist",
            str(watchlist_path),
            "--output-root",
            str(tmp_path / "published"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "snapshot_conflict" in captured.err
    assert "Traceback" not in captured.err


def test_cli_notifies_only_after_report_pipeline_returns(
    fixture_settings, monkeypatch, capsys
):
    settings_data = fixture_settings.model_dump()
    settings_data["notifications"] = {"enabled_channels": ["wecom"]}
    settings = Settings.model_validate(settings_data)
    report = object()
    calls = []

    class FakeNotificationService:
        def __init__(self, configured_settings):
            assert configured_settings is settings.notifications

        def send_report(self, published_report, *, report_url):
            calls.append((published_report, report_url))

    monkeypatch.setattr(cli_module, "load_settings", lambda _path: settings)
    monkeypatch.setattr(
        cli_module,
        "load_watchlist",
        lambda _path: Watchlist(stocks=[{"code": "600519", "name": "one"}]),
    )
    monkeypatch.setattr(
        cli_module,
        "run_daily_report",
        lambda *args, **kwargs: SimpleNamespace(
            report=report, html_path="/tmp/reports/2026-09-04/index.html"
        ),
    )
    monkeypatch.setattr(cli_module, "NotificationService", FakeNotificationService)
    monkeypatch.setenv("REPORT_BASE_URL", "https://reports.example")

    result = cli_module.main(["daily", "--date", "2026-09-04"])

    assert result == 0
    assert calls == [(report, "https://reports.example/reports/2026-09-04/")]
    assert capsys.readouterr().out.endswith("\n")


def test_cli_can_skip_notifications_for_deferred_pages_delivery(
    fixture_settings, monkeypatch
):
    settings_data = fixture_settings.model_dump()
    settings_data["notifications"] = {"enabled_channels": ["wecom"]}
    settings = Settings.model_validate(settings_data)
    calls = []

    class FakeNotificationService:
        def __init__(self, _configured_settings):
            calls.append("created")

    monkeypatch.setattr(cli_module, "load_settings", lambda _path: settings)
    monkeypatch.setattr(
        cli_module,
        "load_watchlist",
        lambda _path: Watchlist(stocks=[{"code": "600519", "name": "one"}]),
    )
    monkeypatch.setattr(
        cli_module,
        "run_daily_report",
        lambda *args, **kwargs: SimpleNamespace(
            report=object(), html_path="/tmp/reports/2026-09-04/index.html"
        ),
    )
    monkeypatch.setattr(cli_module, "NotificationService", FakeNotificationService)

    assert cli_module.main(["daily", "--date", "2026-09-04", "--skip-notifications"]) == 0
    assert calls == []


def test_cli_treats_explicit_report_url_as_final_url(
    fixture_settings, monkeypatch
):
    settings_data = fixture_settings.model_dump()
    settings_data["notifications"] = {"enabled_channels": ["wecom"]}
    settings = Settings.model_validate(settings_data)
    calls = []

    class FakeNotificationService:
        def __init__(self, _configured_settings):
            pass

        def send_report(self, _report, *, report_url):
            calls.append(report_url)

    monkeypatch.setattr(cli_module, "load_settings", lambda _path: settings)
    monkeypatch.setattr(
        cli_module,
        "load_watchlist",
        lambda _path: Watchlist(stocks=[{"code": "600519", "name": "one"}]),
    )
    monkeypatch.setattr(
        cli_module,
        "run_daily_report",
        lambda *args, **kwargs: SimpleNamespace(
            report=object(), html_path="/tmp/reports/2026-09-04/index.html"
        ),
    )
    monkeypatch.setattr(cli_module, "NotificationService", FakeNotificationService)

    cli_module.main(
        [
            "daily",
            "--date",
            "2026-09-04",
            "--report-url",
            "https://reports.example/reports/2026-09-04/",
        ]
    )

    assert calls == ["https://reports.example/reports/2026-09-04/"]


def test_cli_notify_delivers_an_existing_published_report(
    fixture_settings, monkeypatch, tmp_path, capsys
):
    settings_data = fixture_settings.model_dump()
    settings_data["notifications"] = {"enabled_channels": ["wecom"]}
    settings = Settings.model_validate(settings_data)
    report_path = tmp_path / "reports/2026-09-04/report.json"
    report_path.parent.mkdir(parents=True)
    report_path.write_text(
        __import__("test_report_render", fromlist=["_document"])
        ._document("visible")
        .model_dump_json(),
        encoding="utf-8",
    )
    calls = []

    class FakeNotificationService:
        def __init__(self, _configured_settings):
            pass

        def send_report(self, report, *, report_url):
            calls.append((report, report_url))

    monkeypatch.setattr(cli_module, "load_settings", lambda _path: settings)
    monkeypatch.setattr(cli_module, "NotificationService", FakeNotificationService)

    result = cli_module.main(
        [
            "notify",
            "--report",
            str(report_path),
            "--settings",
            str(tmp_path / "settings.yaml"),
            "--report-url",
            "https://reports.example/reports/2026-09-04/",
        ]
    )

    assert result == 0
    assert calls[0][0].metadata.report_date.isoformat() == "2026-09-04"
    assert calls[0][1] == "https://reports.example/reports/2026-09-04/"
    assert "Notifications delivered" in capsys.readouterr().out


def test_cli_reports_notification_failure_without_traceback(
    fixture_settings, monkeypatch, capsys
):
    settings_data = fixture_settings.model_dump()
    settings_data["notifications"] = {"enabled_channels": ["wecom"]}
    settings = Settings.model_validate(settings_data)

    class FailingNotificationService:
        def __init__(self, _configured_settings):
            pass

        def send_report(self, _published_report, *, report_url):
            raise NotificationDeliveryError(
                (
                    NotificationOutcome(
                        channel="wecom",
                        status="failed",
                        error="WECOM_WEBHOOK_URL is required",
                    ),
                )
            )

    monkeypatch.setattr(cli_module, "load_settings", lambda _path: settings)
    monkeypatch.setattr(
        cli_module,
        "load_watchlist",
        lambda _path: Watchlist(stocks=[{"code": "600519", "name": "one"}]),
    )
    monkeypatch.setattr(
        cli_module,
        "run_daily_report",
        lambda *args, **kwargs: SimpleNamespace(
            report=object(), html_path="/tmp/reports/2026-09-04/index.html"
        ),
    )
    monkeypatch.setattr(
        cli_module, "NotificationService", FailingNotificationService
    )

    result = cli_module.main(["daily", "--date", "2026-09-04"])

    captured = capsys.readouterr()
    assert result == 1
    assert "wecom" in captured.err
    assert "Traceback" not in captured.err


def test_cli_handles_publication_rollback_failure_without_traceback(
    fixture_settings, monkeypatch, capsys
):
    monkeypatch.setattr(cli_module, "load_settings", lambda _path: fixture_settings)
    monkeypatch.setattr(
        cli_module,
        "load_watchlist",
        lambda _path: Watchlist(stocks=[{"code": "600519", "name": "one"}]),
    )
    monkeypatch.setattr(
        cli_module,
        "run_daily_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            PublicationRollbackError("recovery artifacts retained")
        ),
    )

    result = cli_module.main(["daily", "--date", "2026-09-04"])

    captured = capsys.readouterr()
    assert result == 1
    assert "recovery artifacts retained" in captured.err
    assert "Traceback" not in captured.err


def test_cli_handles_snapshot_lock_failure_without_traceback(
    fixture_settings, monkeypatch, capsys
):
    monkeypatch.setattr(cli_module, "load_settings", lambda _path: fixture_settings)
    monkeypatch.setattr(
        cli_module,
        "load_watchlist",
        lambda _path: Watchlist(stocks=[{"code": "600519", "name": "one"}]),
    )
    monkeypatch.setattr(
        cli_module,
        "run_daily_report",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            SnapshotError("Could not lock snapshot directory")
        ),
    )

    result = cli_module.main(["daily", "--date", "2026-09-04"])

    captured = capsys.readouterr()
    assert result == 1
    assert "Could not lock snapshot directory" in captured.err
    assert "Traceback" not in captured.err


@pytest.mark.parametrize(
    ("settings_contents", "expected_message"),
    [
        (None, "Configuration file not found"),
        ("rule_version: []\n", "Invalid settings configuration"),
    ],
)
def test_cli_handles_configuration_failure_without_traceback(
    tmp_path, capsys, settings_contents, expected_message
):
    settings_path = tmp_path / "settings.yaml"
    if settings_contents is not None:
        settings_path.write_text(settings_contents, encoding="utf-8")
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text(
        "stocks:\n  - code: '600519'\n    name: one\n", encoding="utf-8"
    )

    result = cli_module.main(
        [
            "daily",
            "--date",
            "2026-09-04",
            "--settings",
            str(settings_path),
            "--watchlist",
            str(watchlist_path),
            "--output-root",
            str(tmp_path / "published"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert expected_message in captured.err
    assert "Traceback" not in captured.err


def test_cli_handles_unsupported_provider_without_traceback(
    tmp_path, capsys
):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        """rule_version:
  name: simplified
  version: v1
notifications:
  enabled_channels: []
market_data:
  primary_provider: unsupported
  fallback_provider: fixture
risk_rules:
  rule_version: risk-v1
  high_realized_volatility20: 0.45
  overextension_ma20_distance: 0.20
  large_drawdown60: -0.20
  adverse_volume_ratio20: 0.50
  minimum_history_bars: 1
""",
        encoding="utf-8",
    )
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text(
        "stocks:\n  - code: '600519'\n    name: one\n", encoding="utf-8"
    )

    result = cli_module.main(
        [
            "daily",
            "--date",
            "2026-09-04",
            "--settings",
            str(settings_path),
            "--watchlist",
            str(watchlist_path),
            "--output-root",
            str(tmp_path / "published"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "Configured provider is unavailable: unsupported" in captured.err
    assert "Traceback" not in captured.err


def test_cli_handles_missing_risk_rules_without_traceback(
    tmp_path, capsys
):
    fixture_directory = tmp_path / "fixtures"
    fixture_directory.mkdir()
    fixture_file = fixture_directory / "600519.csv"
    fixture_file.write_text(
        "trade_date,open,high,low,close,volume,amount,turnover_rate,"
        "adjustment_mode,provider_name,source_timestamp\n"
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
            for bar in make_bars("600519")
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
  fallback_provider: akshare
  minimum_history_bars: 60
  max_completed_trading_day_lag: 1
""",
        encoding="utf-8",
    )
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text(
        "stocks:\n  - code: '600519'\n    name: one\n", encoding="utf-8"
    )

    result = cli_module.main(
        [
            "daily",
            "--date",
            "2026-09-04",
            "--settings",
            str(settings_path),
            "--watchlist",
            str(watchlist_path),
            "--fixture-directory",
            str(fixture_directory),
            "--output-root",
            str(tmp_path / "published"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "risk_rules configuration is required for decisions" in captured.err
    assert "Traceback" not in captured.err


def test_cli_handles_missing_watchlist_without_traceback(tmp_path, capsys):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "rule_version:\n  name: simplified\n  version: v1\n"
        "notifications:\n  enabled_channels: []\n",
        encoding="utf-8",
    )

    result = cli_module.main(
        [
            "daily",
            "--date",
            "2026-09-04",
            "--settings",
            str(settings_path),
            "--watchlist",
            str(tmp_path / "watchlist.yaml"),
            "--output-root",
            str(tmp_path / "published"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "Configuration file not found" in captured.err
    assert "Traceback" not in captured.err


def test_cli_handles_invalid_watchlist_without_traceback(tmp_path, capsys):
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        "rule_version:\n  name: simplified\n  version: v1\n"
        "notifications:\n  enabled_channels: []\n",
        encoding="utf-8",
    )
    watchlist_path = tmp_path / "watchlist.yaml"
    watchlist_path.write_text("stocks: []\n", encoding="utf-8")

    result = cli_module.main(
        [
            "daily",
            "--date",
            "2026-09-04",
            "--settings",
            str(settings_path),
            "--watchlist",
            str(watchlist_path),
            "--output-root",
            str(tmp_path / "published"),
        ]
    )

    captured = capsys.readouterr()
    assert result == 1
    assert "Invalid watchlist configuration" in captured.err
    assert "Traceback" not in captured.err


def test_market_summary_is_derived_from_validated_watchlist_bars(
    tmp_path, fixture_settings
):
    watchlist = Watchlist(
        stocks=[
            {"code": "600519", "name": "one"},
            {"code": "000001", "name": "two"},
        ]
    )
    bars_one = make_bars("600519")
    bars_one[-1] = bars_one[-1].model_copy(update={"close": 180.79})
    bars_two = make_bars("000001")
    bars_two[-1] = bars_two[-1].model_copy(
        update={"close": 177.21, "low": 177.0}
    )
    provider = RecordingProvider({"600519": bars_one, "000001": bars_two})

    outputs = run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=provider,
        report_date=date(2026, 9, 4),
        now=lambda: datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
    )

    assert outputs.report.market_summary.status == "validated_watchlist"
    assert outputs.report.market_summary.text == (
        "Broad-market data unavailable; validated watchlist only: "
        "count=2, average_latest_return=0.00%, up=1, down=1, unchanged=0, "
        "latest_source=2026-09-04T08:00:00+00:00."
    )


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


def test_pipeline_does_not_commit_cache_entries_when_required_symbol_fails(
    tmp_path, fixture_settings
):
    watchlist = Watchlist(
        stocks=[
            {"code": "600519", "name": "one"},
            {"code": "000001", "name": "two"},
        ]
    )
    provider = RecordingProvider(
        {"600519": make_bars("600519"), "000001": make_bars("000001")},
        fail_code="000001",
    )
    fallback = RecordingProvider(
        {"600519": make_bars("600519"), "000001": make_bars("000001")}
    )
    service = MarketDataService(
        {"fixture": provider, "unused": fallback},
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=tmp_path / "cache",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )

    with pytest.raises(PipelineError):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            service=service,
            report_date=date(2026, 9, 4),
        )

    assert not list((tmp_path / "cache").glob("*.json"))
    assert not list((tmp_path / "reports").glob("**/*"))
    assert not list((tmp_path / "snapshots").glob("**/input.json"))
    assert (
        tmp_path / "snapshots/2026-09-04/.input.lock"
    ).exists()


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


def test_pipeline_rejects_provider_ignoring_end_with_future_dated_bar(
    tmp_path, fixture_settings
):
    bars = make_bars("600519")
    future = bars[-1].model_copy(update={"trade_date": date(2026, 9, 5)})
    provider = RecordingProvider({"600519": [*bars, future]})

    with pytest.raises(PipelineError) as error:
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            provider=provider,
            report_date=date(2026, 9, 4),
        )

    assert "future_trade_date" in error.value.failures[0].issue_codes
    report_dir = tmp_path / "reports/2026-09-04"
    assert not any(
        (report_dir / name).exists() for name in ("report.json", "report.md", "index.html")
    )
    assert not (tmp_path / "snapshots/2026-09-04/input.json").exists()


def test_interrupt_after_report_backup_rename_restores_old_report(
    tmp_path, fixture_settings, monkeypatch
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=date(2026, 9, 4),
    )
    report_dir = tmp_path / "reports/2026-09-04"
    before = {path: path.read_bytes() for path in report_dir.iterdir()}
    original_replace = pipeline_module.os.replace

    def interrupt_after_report_backup(source, destination):
        original_replace(source, destination)
        if Path(source) == report_dir:
            raise KeyboardInterrupt

    monkeypatch.setattr(pipeline_module.os, "replace", interrupt_after_report_backup)

    with pytest.raises(KeyboardInterrupt):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            provider=RecordingProvider({"600519": make_bars("600519")}),
            report_date=date(2026, 9, 4),
        )

    assert {path: path.read_bytes() for path in report_dir.iterdir()} == before
    assert not list(tmp_path.glob(".publication-*"))


def test_startup_recovers_orphan_publication_after_hard_exit(
    tmp_path, fixture_settings
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=date(2026, 9, 4),
    )
    report_dir = tmp_path / "reports/2026-09-04"
    before = {path.name: path.read_bytes() for path in report_dir.iterdir()}
    orphan = tmp_path / ".publication-hard-exit"
    staged_report = orphan / "reports/2026-09-04"
    staged_report.mkdir(parents=True)
    (staged_report / "report.json").write_text("new", encoding="utf-8")
    (staged_report / "report.md").write_text("new", encoding="utf-8")
    (staged_report / "index.html").write_text("new", encoding="utf-8")
    staged_site = orphan / "site"
    staged_site.mkdir()
    (staged_site / "index.html").write_text("new", encoding="utf-8")
    repo_root = Path(__file__).parents[1]
    child = f"""
import os
from pathlib import Path
from datetime import date
import stock_daily_report.pipeline as pipeline

root = Path({str(tmp_path)!r})
transaction_root = root / ".publication-hard-exit"
transaction = pipeline._PublicationTransaction(
    root=root,
    report_date=date(2026, 9, 4),
    staged_report_dir=transaction_root / "reports/2026-09-04",
    staged_snapshot_path=None,
    staged_site_index=transaction_root / "site/index.html",
    staged_styles_path=None,
)
original_replace = pipeline.os.replace

def hard_exit_after_report_backup(source, destination):
    original_replace(source, destination)
    if Path(destination) == transaction.backup_report_dir:
        os._exit(73)

pipeline.os.replace = hard_exit_after_report_backup
transaction.publish()
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
    assert result.returncode == 73
    assert not report_dir.exists()

    with pytest.raises(PipelineError):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            provider=RecordingProvider(
                {"600519": make_bars("600519")}, fail_code="600519"
            ),
            report_date=date(2026, 9, 4),
        )

    assert {path.name: path.read_bytes() for path in report_dir.iterdir()} == before
    assert not list(tmp_path.glob(".publication-*"))


def test_startup_cleans_payload_only_transaction_after_hard_exit_before_manifest(
    tmp_path, fixture_settings
):
    repo_root = Path(__file__).parents[1]
    child = f"""
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import stock_daily_report.pipeline as pipeline
from stock_daily_report.models import DailyBar, Settings, Watchlist
from stock_daily_report.pipeline import run_daily_report

root = Path({str(tmp_path)!r})
original_transaction = pipeline._PublicationTransaction


class HardExitBeforeManifest(original_transaction):
    def __init__(self, *args, **kwargs):
        os._exit(76)


pipeline._PublicationTransaction = HardExitBeforeManifest
bars = [
    DailyBar(
        trade_date=date(2026, 6, 17) + timedelta(days=index),
        open=100.0 + index,
        high=102.0 + index,
        low=99.0 + index,
        close=101.0 + index,
        volume=1000.0 + index,
        amount=(101.0 + index) * 1000.0,
        turnover_rate=0.1,
        adjustment_mode="qfq",
        provider_name="fixture",
        source_timestamp=datetime(2026, 9, 4, 8, tzinfo=UTC),
    )
    for index in range(80)
]
run_daily_report(
    Settings.model_validate({fixture_settings.model_dump()!r}),
    output_root=root,
    watchlist=Watchlist(stocks=[{{"code": "600519", "name": "one"}}]),
    provider=type(
        "Provider",
        (),
        {{
            "name": "fixture",
            "get_daily_bars": lambda self, code, *, start=None, end=None: bars,
        }},
    )(),
    report_date=date(2026, 9, 4),
)
"""
    result = subprocess.run(
        [sys.executable, "-c", child],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(repo_root / "src")},
        check=False,
    )
    assert result.returncode == 76
    transaction_paths = list(tmp_path.glob(".publication-*"))
    assert len(transaction_paths) == 1
    assert not (transaction_paths[0] / "manifest.json").exists()
    assert (transaction_paths[0] / "reports/2026-09-04").is_dir()
    assert (transaction_paths[0] / "snapshots/2026-09-04/input.json").exists()

    with pytest.raises(PipelineError):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            provider=RecordingProvider(
                {"600519": make_bars(600519)}, fail_code="600519"
            ),
            report_date=date(2026, 9, 4),
        )

    assert not list(tmp_path.glob(".publication-*"))


def test_manifestless_transaction_with_mutation_evidence_is_quarantined(
    tmp_path,
):
    transaction_root = tmp_path / ".publication-ambiguous"
    backup_root = transaction_root / "backups"
    backup_root.mkdir(parents=True)
    (backup_root / "report").write_text("possible published preimage", encoding="utf-8")

    with pytest.raises(
        PublicationRollbackError, match="ambiguous|quarantined"
    ):
        pipeline_module._recover_pending_publications_if_idle(
            tmp_path,
            date(2026, 9, 4),
        )

    quarantine = transaction_root / "quarantine.json"
    assert quarantine.exists()
    assert json.loads(quarantine.read_text(encoding="utf-8"))["state"] == "quarantined"
    assert (backup_root / "report").read_text(encoding="utf-8") == (
        "possible published preimage"
    )


def test_restart_recovers_publication_and_cache_as_one_commit(
    tmp_path, fixture_settings
):
    child = f"""
import os
from datetime import date
from pathlib import Path

from datetime import UTC, date, datetime, timedelta

from stock_daily_report.models import DailyBar, Settings, Watchlist
from stock_daily_report.pipeline import run_daily_report
from stock_daily_report.providers.service import MarketDataService
from stock_daily_report.quality.checks import DataQualitySettings


class FixtureProvider:
    name = "fixture"

    def get_daily_bars(self, code, *, start=None, end=None):
        first_day = date(2026, 6, 17)
        return [
            DailyBar(
                trade_date=first_day + timedelta(days=index),
                open=100.0 + index,
                high=102.0 + index,
                low=99.0 + index,
                close=101.0 + index,
                volume=1000.0 + index,
                amount=(101.0 + index) * 1000.0,
                turnover_rate=0.1,
                adjustment_mode="qfq",
                provider_name="fixture",
                source_timestamp=datetime(2026, 9, 4, 8, tzinfo=UTC),
            )
            for index in range(80)
        ]


class UnusedProvider:
    name = "unused"


root = Path({str(tmp_path)!r})
settings = Settings(
    rule_version={{"name": "simplified", "version": "v1"}},
    notifications={{"enabled_channels": []}},
    market_data={{
        "primary_provider": "fixture",
        "fallback_provider": "unused",
        "cache_directory": str(root / "cache"),
        "cache_ttl_seconds": 3600,
        "minimum_history_bars": 60,
        "max_completed_trading_day_lag": 1,
    }},
    risk_rules={{
        "rule_version": "risk-v1",
        "high_realized_volatility20": 0.45,
        "overextension_ma20_distance": 0.20,
        "large_drawdown60": -0.20,
        "adverse_volume_ratio20": 0.50,
        "minimum_history_bars": 61,
    }},
)
service = MarketDataService(
    {{
        "fixture": FixtureProvider(),
        "unused": UnusedProvider(),
    }},
    primary_provider="fixture",
    fallback_provider="unused",
    cache_directory=root / "cache",
    cache_ttl_seconds=3600,
    quality_settings=DataQualitySettings(
        minimum_history_bars=60,
        max_completed_trading_day_lag=1,
    ),
)


def fail_before_cache_recovery_ack():
    raise OSError("simulated restart before cache recovery acknowledgement")


service.finalize_staged_cache_commit = fail_before_cache_recovery_ack
run_daily_report(
    settings,
    output_root=root,
    watchlist=Watchlist(stocks=[{{"code": "600519", "name": "one"}}]),
    service=service,
    report_date=date(2026, 9, 4),
)
"""
    result = subprocess.run(
        [sys.executable, "-c", child],
        cwd=Path(__file__).parents[1],
        env={**os.environ, "PYTHONPATH": str(Path(__file__).parents[1] / "src")},
        check=False,
    )
    assert result.returncode != 0

    restarted = MarketDataService(
        {
            "fixture": RecordingProvider({"600519": make_bars("600519")}),
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=tmp_path / "cache",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    cache_path = restarted._cache._path_for(
        "fixture", "600519", None, date(2026, 9, 4)
    )
    pipeline_module._recover_pending_publications_if_idle(
        tmp_path, date(2026, 9, 4), service=restarted
    )

    assert cache_path.exists()
    assert not list(tmp_path.glob(".publication-*"))


def test_startup_removes_newly_created_target_after_hard_exit_at_publish_rename(
    tmp_path, fixture_settings
):
    orphan = tmp_path / ".publication-hard-exit-absent"
    staged_report = orphan / "reports/2026-09-04"
    staged_report.mkdir(parents=True)
    for name in ("report.json", "report.md", "index.html"):
        (staged_report / name).write_text("new", encoding="utf-8")
    staged_site = orphan / "site"
    staged_site.mkdir()
    (staged_site / "index.html").write_text("new", encoding="utf-8")
    repo_root = Path(__file__).parents[1]
    child = f"""
import os
from datetime import date
from pathlib import Path
import stock_daily_report.pipeline as pipeline

root = Path({str(tmp_path)!r})
transaction_root = root / ".publication-hard-exit-absent"
transaction = pipeline._PublicationTransaction(
    root=root,
    report_date=date(2026, 9, 4),
    staged_report_dir=transaction_root / "reports/2026-09-04",
    staged_snapshot_path=None,
    staged_site_index=transaction_root / "site/index.html",
    staged_styles_path=None,
)
original_replace = pipeline.os.replace


def hard_exit_after_report_publish(source, destination):
    original_replace(source, destination)
    if Path(destination) == transaction.report_dir:
        os._exit(74)


pipeline.os.replace = hard_exit_after_report_publish
transaction.publish()
"""
    result = subprocess.run(
        [sys.executable, "-c", child],
        cwd=repo_root,
        env={**os.environ, "PYTHONPATH": str(repo_root / "src")},
        check=False,
    )
    assert result.returncode == 74
    report_dir = tmp_path / "reports/2026-09-04"
    assert report_dir.exists()

    with pytest.raises(PipelineError):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            provider=RecordingProvider(
                {"600519": make_bars("600519")}, fail_code="600519"
            ),
            report_date=date(2026, 9, 4),
        )

    assert not report_dir.exists()
    assert not list(tmp_path.glob(".publication-*"))


def test_cache_preparation_failure_preserves_original_target_and_recovery_manifest(
    tmp_path, fixture_settings, monkeypatch
):
    service = MarketDataService(
        {
            "fixture": RecordingProvider({"600519": make_bars("600519")}),
            "unused": RecordingProvider({"600519": make_bars("600519")}),
        },
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=tmp_path / "cache",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    cache_path = service._cache._path_for(
        "fixture", "600519", None, date(2026, 9, 4)
    )
    original_cache_bytes = b"original cache preimage"
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_bytes(original_cache_bytes)
    monkeypatch.setattr(service._cache, "load", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        service,
        "_copy_cache_preimage",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError("injected preimage preparation failure")
        ),
    )

    with pytest.raises(CacheRollbackError, match="preimage"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            service=service,
            report_date=date(2026, 9, 4),
        )

    assert cache_path.read_bytes() == original_cache_bytes
    assert list((tmp_path / "cache").glob(".cache-recovery-*"))


def test_failed_rollback_retains_recovery_artifacts(
    tmp_path, fixture_settings, monkeypatch
):
    watchlist = Watchlist(stocks=[{"code": "600519", "name": "one"}])
    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=watchlist,
        provider=RecordingProvider({"600519": make_bars("600519")}),
        report_date=date(2026, 9, 4),
    )
    report_dir = tmp_path / "reports/2026-09-04"
    original_replace = pipeline_module.os.replace

    def fail_restore(source, destination):
        if Path(source) == report_dir:
            original_replace(source, destination)
            raise KeyboardInterrupt
        if Path(source).name == "report" and Path(source).parent.name == "backups":
            raise OSError("injected rollback failure")
        return original_replace(source, destination)

    monkeypatch.setattr(pipeline_module.os, "replace", fail_restore)

    with pytest.raises(
        pipeline_module.PublicationRollbackError,
        match="recovery artifacts retained",
    ):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=watchlist,
            provider=RecordingProvider({"600519": make_bars("600519")}),
            report_date=date(2026, 9, 4),
        )

    recovery_backups = list(tmp_path.glob(".publication-*/backups"))
    assert len(recovery_backups) == 1
    assert (recovery_backups[0] / "report").exists()


def test_prepublication_failure_discards_staged_cache_before_service_reuse(
    tmp_path, fixture_settings, monkeypatch
):
    provider = RecordingProvider({"600519": make_bars("600519")})
    service = MarketDataService(
        {"fixture": provider, "unused": RecordingProvider({"600519": make_bars("600519")})},
        primary_provider="fixture",
        fallback_provider="unused",
        cache_directory=tmp_path / "cache",
        cache_ttl_seconds=3600,
        quality_settings=DataQualitySettings(
            minimum_history_bars=fixture_settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=fixture_settings.market_data.max_completed_trading_day_lag,
        ),
    )
    original_mkdir = Path.mkdir

    def fail_output_root(path, *args, **kwargs):
        if path == tmp_path:
            raise OSError("injected output root failure")
        return original_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", fail_output_root)

    with pytest.raises(OSError, match="injected output root failure"):
        run_daily_report(
            fixture_settings,
            output_root=tmp_path,
            watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
            service=service,
            report_date=date(2026, 9, 4),
        )

    assert service._staged_cache_writes == []
    monkeypatch.undo()
    provider.bars_by_code["600519"] = [
        bar.model_copy(update={"open": 200.0, "high": 202.0, "low": 199.0, "close": 201.0})
        for bar in make_bars("600519")
    ]

    run_daily_report(
        fixture_settings,
        output_root=tmp_path,
        watchlist=Watchlist(stocks=[{"code": "600519", "name": "one"}]),
        service=service,
        report_date=date(2026, 9, 4),
    )

    cached = service._cache.load("fixture", "600519", None, date(2026, 9, 4))
    assert cached[-1]["close"] == 201.0


def test_report_json_is_deterministic_and_contains_auditing_metadata(
    tmp_path, fixture_settings
):
    first_watchlist = Watchlist(
        stocks=[
            {"code": "600519", "name": "贵州茅台"},
            {"code": "000001", "name": "平安银行"},
        ]
    )
    second_watchlist = Watchlist(
        stocks=[
            {"code": "000001", "name": "平安银行"},
            {"code": "600519", "name": "贵州茅台"},
        ]
    )
    first_outputs = run_daily_report(
        fixture_settings,
        output_root=tmp_path / "first",
        watchlist=first_watchlist,
        provider=RecordingProvider(
            {"600519": make_bars("600519"), "000001": make_bars("000001")}
        ),
        report_date=date(2026, 9, 4),
        now=lambda: datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
    )
    second_outputs = run_daily_report(
        fixture_settings,
        output_root=tmp_path / "second",
        watchlist=second_watchlist,
        provider=RecordingProvider(
            {"000001": make_bars("000001"), "600519": make_bars("600519")}
        ),
        report_date=date(2026, 9, 4),
        now=lambda: datetime(2026, 9, 4, 9, 30, tzinfo=UTC),
    )
    first = first_outputs.json_path.read_bytes()
    second = second_outputs.json_path.read_bytes()
    document = json.loads(first)

    assert first == second
    assert hashlib.sha256(first).hexdigest() == hashlib.sha256(second).hexdigest()
    assert document["schema_version"] == 1
    assert document["metadata"]["snapshot_path"] == "snapshots/2026-09-04/input.json"
    assert document["metadata"]["config_hash"] == first_outputs.report.metadata.config_hash
    assert document["metadata"]["analyzer_versions"]["structural"] == "simplified-v1"
    assert document["metadata"]["stock_count"] == 2
    assert "webhook" not in first_outputs.json_path.read_text(encoding="utf-8")


def test_report_artifacts_do_not_leak_notification_webhooks(tmp_path, fixture_settings):
    settings = fixture_settings.model_copy(
        update={
            "notifications": {"enabled_channels": ["wecom"]},
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
