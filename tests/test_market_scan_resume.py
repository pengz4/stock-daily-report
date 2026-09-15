"""Tests for resumable full-market scan orchestration and checkpointing."""

import json
import time
from datetime import UTC, date, datetime, timedelta

import pytest

from stock_daily_report.market_scan.resumable import run_resumable_scan
from stock_daily_report.market_scan.resume import (
    CheckpointIntegrityError,
    ScanCheckpoint,
    archive_checkpoint,
    build_checkpoint,
    checkpoint_completed,
    checkpoint_quotes,
    load_checkpoint,
    manifest_hash_for,
    save_checkpoint,
)
from stock_daily_report.market_scan.runner import (
    _candidate_result_from_json,
    _candidate_result_to_json,
    scoring_config_hash,
)
from stock_daily_report.models import DailyBar, MarketScanSettings
from stock_daily_report.providers.universe import UniverseQuote

REPORT_DATE = date(2026, 9, 11)


def _settings(**changes: object) -> MarketScanSettings:
    values = {
        "rule_version": "market-scan-v1",
        "trend_limit": 30,
        "balanced_limit": 30,
        "minimum_history_bars": 120,
        "minimum_latest_amount": 50_000_000,
        "minimum_coverage_ratio": 0.8,
        "max_workers": 3,
        "max_candidates": 1_200,
    }
    values.update(changes)
    return MarketScanSettings(**values)


def _quote(code: str, change_pct: float | None = None) -> UniverseQuote:
    return UniverseQuote(
        code=code,
        name=f"Company {code}",
        market="SH",
        latest_price=100.0,
        volume=1_000_000.0,
        amount=100_000_000.0,
        quote_date=REPORT_DATE,
        change_pct=change_pct,
    )


def _bars(code: str) -> list[DailyBar]:
    start = REPORT_DATE - timedelta(days=129)
    offset = int(code[-2:]) / 100_000
    return [
        DailyBar(
            trade_date=start + timedelta(days=index),
            open=100.0 * (1.002 + offset) ** index,
            high=100.0 * (1.002 + offset) ** index + 1.0,
            low=100.0 * (1.002 + offset) ** index - 1.0,
            close=100.0 * (1.002 + offset) ** index,
            volume=1_000_000.0 + index,
            amount=(1_000_000.0 + index) * (100.0 * (1.002 + offset) ** index),
            turnover_rate=0.5,
            adjustment_mode="qfq",
            provider_name="fake-history",
            source_timestamp=datetime(2026, 9, 11, tzinfo=UTC),
        )
        for index in range(130)
    ]


class FakeUniverseProvider:
    name = "fake-universe"

    def __init__(self, quotes: list[UniverseQuote]) -> None:
        self._quotes = quotes

    def get_quotes(self) -> list[UniverseQuote]:
        return list(self._quotes)


class FakeHistoryProvider:
    name = "fake-history"

    def __init__(self, bars_by_code: dict[str, list[DailyBar]]) -> None:
        self._bars_by_code = bars_by_code
        self.requested: list[str] = []

    def get_daily_bars(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[DailyBar]:
        del start, end
        self.requested.append(code)
        return list(self._bars_by_code[code])


class FakeIndexProvider:
    name = "fake-index"

    def __init__(self, bars_by_code: dict[str, list[DailyBar]]) -> None:
        self._bars_by_code = bars_by_code

    def get_daily_bars(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[DailyBar]:
        del start, end
        return list(self._bars_by_code[code])


def _codes(count: int) -> list[str]:
    return [f"600{i:03d}" for i in range(count)]


def _make_checkpoint_for(
    tmp_path,
    *,
    report_date: date,
    execution_date: date,
    settings: MarketScanSettings,
    quotes: list[UniverseQuote],
    completed: list[object] | None = None,
    state: str = "running",
) -> ScanCheckpoint:
    manifest_hash = manifest_hash_for(quotes)
    checkpoint = build_checkpoint(
        report_date=report_date,
        execution_date=execution_date,
        rule_version=settings.rule_version,
        scoring_config_hash=scoring_config_hash(settings),
        manifest_hash=manifest_hash,
        universe_quotes=quotes,
        completed=completed or [],
        state=state,  # type: ignore[arg-type]
        source_revision="test",
        last_batch=0,
    )
    save_checkpoint(tmp_path, checkpoint)
    return checkpoint


def test_scoring_config_hash_excludes_runtime_parameters():
    base = scoring_config_hash(_settings())
    assert base == scoring_config_hash(_settings(max_candidates=4000))
    assert base == scoring_config_hash(_settings(max_workers=16))
    assert base != scoring_config_hash(_settings(minimum_coverage_ratio=0.9))


def test_resumable_batch_fuses_hung_symbol(monkeypatch):
    from stock_daily_report.market_scan import runner
    from stock_daily_report.market_scan.resumable import _process_batch

    monkeypatch.setattr(runner, "_FETCH_TIMEOUT_SECONDS", 0.1)
    codes = _codes(1)

    class SlowHistoryProvider(FakeHistoryProvider):
        supports_hard_timeout = True

        def get_daily_bars(self, code, *, start=None, end=None):
            time.sleep(1.0)
            return super().get_daily_bars(code, start=start, end=end)

    provider = SlowHistoryProvider({codes[0]: _bars(codes[0])})
    started = time.monotonic()
    results = _process_batch(
        [_quote(codes[0])],
        provider,
        REPORT_DATE,
        _settings(),
    )

    assert time.monotonic() - started < 0.5
    assert results[codes[0]].status.status == "history_failed"
    assert results[codes[0]].status.reason_codes == ("fetch_timeout",)


def test_candidate_result_serialization_roundtrip():
    from stock_daily_report.market_scan.models import ScanStatus
    from stock_daily_report.market_scan.runner import _CandidateResult

    quote = _quote("600000")
    status = ScanStatus(
        code="600000",
        name="Company 600000",
        status="history_excluded",
        reason_codes=("insufficient_history",),
        provider_name="fake-history",
    )
    result = _CandidateResult(
        quote=quote,
        status=status,
        scores=None,
        latest_trade_date=None,
        provider_name="fake-history",
        history_hash="a" * 64,
        provider_names=("fake-history",),
    )

    document = _candidate_result_to_json(result)
    restored = _candidate_result_from_json(document)

    assert restored.quote.code == "600000"
    assert restored.status.status == "history_excluded"
    assert restored.status.reason_codes == ("insufficient_history",)
    assert restored.history_hash == "a" * 64


def test_checkpoint_roundtrip_preserves_execution_date(tmp_path):
    quotes = [_quote("600000"), _quote("600001")]
    _make_checkpoint_for(
        tmp_path,
        report_date=REPORT_DATE,
        execution_date=REPORT_DATE,
        settings=_settings(),
        quotes=quotes,
    )

    loaded = load_checkpoint(tmp_path, REPORT_DATE)
    assert loaded is not None
    assert loaded.schema_version == 2
    assert loaded.execution_date == REPORT_DATE
    assert [q.code for q in checkpoint_quotes(loaded)] == ["600000", "600001"]
    assert checkpoint_completed(loaded) == {}


def test_load_checkpoint_returns_none_when_missing(tmp_path):
    assert load_checkpoint(tmp_path, REPORT_DATE) is None


def test_load_checkpoint_rejects_manifest_hash_mismatch(tmp_path):
    quotes = [_quote("600000")]
    _make_checkpoint_for(
        tmp_path,
        report_date=REPORT_DATE,
        execution_date=REPORT_DATE,
        settings=_settings(),
        quotes=quotes,
    )
    path = tmp_path / "market-scans" / REPORT_DATE.isoformat() / "progress.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["manifest_hash"] = "0" * 64
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CheckpointIntegrityError, match="manifest hash mismatch"):
        load_checkpoint(tmp_path, REPORT_DATE)


def test_load_checkpoint_rejects_unsupported_schema(tmp_path):
    quotes = [_quote("600000")]
    _make_checkpoint_for(
        tmp_path,
        report_date=REPORT_DATE,
        execution_date=REPORT_DATE,
        settings=_settings(),
        quotes=quotes,
    )
    path = tmp_path / "market-scans" / REPORT_DATE.isoformat() / "progress.json"
    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = 1
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(CheckpointIntegrityError):
        load_checkpoint(tmp_path, REPORT_DATE)


def test_archive_checkpoint_moves_file(tmp_path):
    quotes = [_quote("600000")]
    checkpoint = _make_checkpoint_for(
        tmp_path,
        report_date=REPORT_DATE,
        execution_date=REPORT_DATE,
        settings=_settings(),
        quotes=quotes,
    )
    archive_checkpoint(tmp_path, checkpoint)

    assert load_checkpoint(tmp_path, REPORT_DATE) is None
    archive_dir = (
        tmp_path / "market-scans" / REPORT_DATE.isoformat() / "progress-archive"
    )
    assert archive_dir.exists()
    archived = list(archive_dir.glob("*.json"))
    assert len(archived) == 1
    assert archived[0].name.startswith("2026-09-11-")


def test_resumable_scan_completes_across_batches(tmp_path):
    codes = _codes(5)
    quotes = [_quote(code) for code in codes]
    provider = FakeUniverseProvider(quotes)
    history = FakeHistoryProvider({code: _bars(code) for code in codes})
    settings = _settings(max_candidates=3)

    path = run_resumable_scan(
        settings,
        provider,
        history,
        report_date=REPORT_DATE,
        output_root=tmp_path,
        max_batches=1,
        batch_size=3,
    )
    assert path is None

    checkpoint = load_checkpoint(tmp_path, REPORT_DATE)
    assert checkpoint is not None
    assert checkpoint.state == "running"
    assert len(checkpoint.completed) == 3

    path = run_resumable_scan(
        settings,
        provider,
        history,
        report_date=REPORT_DATE,
        output_root=tmp_path,
    )
    assert path is not None

    from stock_daily_report.market_scan.report import load_scan_artifact

    artifact = load_scan_artifact(path)
    assert artifact.report_date == REPORT_DATE
    assert artifact.eligible_count == 5


def test_resumable_scan_resumes_without_refetching(tmp_path):
    codes = _codes(4)
    quotes = [_quote(code) for code in codes]
    provider = FakeUniverseProvider(quotes)
    history = FakeHistoryProvider({code: _bars(code) for code in codes})
    settings = _settings(max_candidates=2)

    run_resumable_scan(
        settings,
        provider,
        history,
        report_date=REPORT_DATE,
        output_root=tmp_path,
        max_batches=1,
        batch_size=2,
    )

    history.requested.clear()
    run_resumable_scan(
        settings,
        provider,
        history,
        report_date=REPORT_DATE,
        output_root=tmp_path,
    )

    assert len(history.requested) == 2


def test_resumable_scan_preserves_market_state_from_checkpoint_snapshot(tmp_path):
    from stock_daily_report.market_scan.models import MarketScanArtifact
    from stock_daily_report.models import MARKET_STATE_INDEX_CODES

    codes = _codes(2)
    quotes = [_quote(codes[0], 1.0), _quote(codes[1], -1.0)]
    universe = FakeUniverseProvider(quotes)
    history = FakeHistoryProvider({code: _bars(code) for code in codes})
    index = FakeIndexProvider(
        {code: _bars("600000") for code in MARKET_STATE_INDEX_CODES}
    )
    settings = _settings(max_candidates=2)

    assert (
        run_resumable_scan(
            settings,
            universe,
            history,
            index_provider=index,
            report_date=REPORT_DATE,
            output_root=tmp_path,
            max_batches=1,
            batch_size=1,
        )
        is None
    )

    universe._quotes = [_quote(codes[0], -1.0), _quote(codes[1], 1.0)]

    path = run_resumable_scan(
        settings,
        universe,
        history,
        index_provider=index,
        report_date=REPORT_DATE,
        output_root=tmp_path,
    )

    assert path is not None
    artifact = MarketScanArtifact.model_validate_json(path.read_text(encoding="utf-8"))
    assert artifact.market_state is not None
    assert artifact.market_state.breadth.advancing_count == 1
    assert artifact.market_state.breadth.declining_count == 1


def test_resumable_scan_archives_checkpoint_when_market_state_configuration_changes(
    tmp_path,
):
    from stock_daily_report.models import MARKET_STATE_INDEX_CODES, MarketStateSettings

    codes = _codes(2)
    quotes = [_quote(code) for code in codes]
    universe = FakeUniverseProvider(quotes)
    history = FakeHistoryProvider({code: _bars(code) for code in codes})
    index = FakeIndexProvider(
        {code: _bars("600000") for code in MARKET_STATE_INDEX_CODES}
    )
    settings = _settings(max_candidates=2)

    run_resumable_scan(
        settings,
        universe,
        history,
        index_provider=index,
        report_date=REPORT_DATE,
        output_root=tmp_path,
        max_batches=1,
        batch_size=1,
    )

    changed = settings.model_copy(
        update={
            "market_state": MarketStateSettings(lookback_periods=(10, 30)),
        }
    )
    path = run_resumable_scan(
        changed,
        universe,
        history,
        index_provider=index,
        report_date=REPORT_DATE,
        output_root=tmp_path,
    )

    archive_dir = (
        tmp_path / "market-scans" / REPORT_DATE.isoformat() / "progress-archive"
    )
    assert path is not None
    assert len(list(archive_dir.glob("*.json"))) == 1


def test_resumable_scan_archives_stale_scoring_hash(tmp_path):
    codes = _codes(3)
    quotes = [_quote(code) for code in codes]
    provider = FakeUniverseProvider(quotes)
    history = FakeHistoryProvider({code: _bars(code) for code in codes})
    settings = _settings(max_candidates=2)

    run_resumable_scan(
        settings,
        provider,
        history,
        report_date=REPORT_DATE,
        output_root=tmp_path,
        max_batches=1,
        batch_size=2,
    )

    changed = _settings(max_candidates=2, minimum_coverage_ratio=0.9)
    path = run_resumable_scan(
        changed,
        provider,
        history,
        report_date=REPORT_DATE,
        output_root=tmp_path,
    )

    archive_dir = (
        tmp_path / "market-scans" / REPORT_DATE.isoformat() / "progress-archive"
    )
    assert archive_dir.exists()
    assert len(list(archive_dir.glob("*.json"))) == 1
    assert path is not None


def test_resumable_scan_archives_cross_day_checkpoint(tmp_path, monkeypatch):
    codes = _codes(3)
    quotes = [_quote(code) for code in codes]
    provider = FakeUniverseProvider(quotes)
    history = FakeHistoryProvider({code: _bars(code) for code in codes})
    settings = _settings(max_candidates=2)

    from stock_daily_report.market_scan import resumable as resumable_mod

    # Freeze the shanghai date for the first run.
    monkeypatch.setattr(
        resumable_mod,
        "_current_shanghai_date",
        lambda: REPORT_DATE,
    )
    run_resumable_scan(
        settings,
        provider,
        history,
        report_date=REPORT_DATE,
        output_root=tmp_path,
        max_batches=1,
        batch_size=2,
    )

    # The next run happens the following calendar day; the stale running
    # checkpoint must be archived and a fresh scan started.
    monkeypatch.setattr(
        resumable_mod,
        "_current_shanghai_date",
        lambda: REPORT_DATE + timedelta(days=1),
    )
    history.requested.clear()
    path = run_resumable_scan(
        settings,
        provider,
        history,
        report_date=REPORT_DATE,
        output_root=tmp_path,
    )

    archive_dir = (
        tmp_path / "market-scans" / REPORT_DATE.isoformat() / "progress-archive"
    )
    assert archive_dir.exists()
    assert len(list(archive_dir.glob("*.json"))) == 1
    assert path is not None


def test_resumable_scan_stops_when_midnight_crossed(tmp_path, monkeypatch):
    codes = _codes(5)
    quotes = [_quote(code) for code in codes]
    provider = FakeUniverseProvider(quotes)
    history = FakeHistoryProvider({code: _bars(code) for code in codes})
    settings = _settings(max_candidates=2)

    from stock_daily_report.market_scan import resumable as resumable_mod

    days = iter([REPORT_DATE, REPORT_DATE, REPORT_DATE + timedelta(days=1)])

    def fake_date():
        return next(days)

    monkeypatch.setattr(resumable_mod, "_current_shanghai_date", fake_date)

    # The third batch sees midnight crossed and stops without an artifact.
    path = run_resumable_scan(
        settings,
        provider,
        history,
        report_date=REPORT_DATE,
        output_root=tmp_path,
        batch_size=2,
    )
    assert path is None

    checkpoint = load_checkpoint(tmp_path, REPORT_DATE)
    assert checkpoint is not None
    assert checkpoint.state == "running"
    assert checkpoint.execution_date == REPORT_DATE


def test_complete_checkpoint_generates_missing_artifact(tmp_path):
    codes = _codes(2)
    quotes = [_quote(code) for code in codes]
    provider = FakeUniverseProvider(quotes)
    history = FakeHistoryProvider({code: _bars(code) for code in codes})
    settings = _settings()

    # Run to completion, then delete the artifact, leaving a complete checkpoint.
    path = run_resumable_scan(
        settings,
        provider,
        history,
        report_date=REPORT_DATE,
        output_root=tmp_path,
    )
    assert path is not None
    path.unlink()

    # A subsequent run finds state=complete and regenerates the artifact.
    path2 = run_resumable_scan(
        settings,
        provider,
        history,
        report_date=REPORT_DATE,
        output_root=tmp_path,
    )
    assert path2 is not None
    assert path2.exists()
