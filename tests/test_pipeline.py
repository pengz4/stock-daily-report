import hashlib
import json
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

import stock_daily_report.cli as cli_module
import stock_daily_report.pipeline as pipeline_module
from stock_daily_report.models import DailyBar, Settings, Watchlist
from stock_daily_report.pipeline import (
    PipelineError,
    PipelineFailure,
    run_daily_report,
)
from stock_daily_report.providers.service import MarketDataService
from stock_daily_report.quality.checks import DataQualitySettings
from stock_daily_report.snapshots import load_snapshot


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
    assert not list((tmp_path / "snapshots").glob("**/*"))


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
