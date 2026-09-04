import hashlib
import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from stock_daily_report.providers.base import DailyBar, MarketDataProvider
from stock_daily_report.providers.fixture import (
    FixtureDataError,
    FixtureMarketDataProvider,
)
from stock_daily_report.snapshots import (
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotConflictError,
    SnapshotError,
    load_snapshot,
    write_snapshot,
)

FIXTURE_DIRECTORY = Path(__file__).parents[1] / "fixtures" / "bars"
GENERATED_AT = datetime(2026, 9, 4, 9, 30, tzinfo=UTC)


@pytest.fixture
def provider() -> FixtureMarketDataProvider:
    return FixtureMarketDataProvider(FIXTURE_DIRECTORY)


@pytest.fixture
def bars(provider: FixtureMarketDataProvider) -> list[DailyBar]:
    return provider.get_daily_bars("600519")


def test_snapshot_round_trip_preserves_content_hash(tmp_path, bars):
    path = write_snapshot(
        tmp_path,
        report_date=date(2026, 9, 4),
        bars_by_code={"600519": bars},
        generated_at=GENERATED_AT,
    )

    snapshot = load_snapshot(path)

    assert snapshot.content_hash
    assert snapshot.bars_by_code["600519"][-1].close == bars[-1].close


def test_fixture_provider_returns_chronological_normalized_bars(provider):
    bars = provider.get_daily_bars("600519")

    assert isinstance(provider, MarketDataProvider)
    assert [bar.trade_date for bar in bars] == sorted(
        bar.trade_date for bar in bars
    )
    assert all(bar.source_timestamp.tzinfo == UTC for bar in bars)


def test_fixture_provider_rejects_absent_or_unsafe_codes(provider):
    with pytest.raises(FixtureDataError, match="No fixture data"):
        provider.get_daily_bars("600000")
    with pytest.raises(FixtureDataError, match="six digits"):
        provider.get_daily_bars("../600519")


def test_snapshot_bytes_and_hash_are_independent_of_code_mapping_order(tmp_path, bars):
    first_path = write_snapshot(
        tmp_path / "first",
        report_date=date(2026, 9, 4),
        bars_by_code={"600519": bars, "000001": bars},
        generated_at=GENERATED_AT,
    )
    second_path = write_snapshot(
        tmp_path / "second",
        report_date=date(2026, 9, 4),
        bars_by_code={"000001": bars, "600519": bars},
        generated_at=GENERATED_AT,
    )

    assert first_path.read_bytes() == second_path.read_bytes()
    assert load_snapshot(first_path).content_hash == load_snapshot(second_path).content_hash


def test_snapshot_uses_date_path_and_sorted_code_metadata(tmp_path, bars):
    path = write_snapshot(
        tmp_path,
        report_date=date(2026, 9, 4),
        bars_by_code={"600519": bars, "000001": bars},
        generated_at=GENERATED_AT,
    )

    snapshot = load_snapshot(path)

    assert path == tmp_path / "snapshots" / "2026-09-04" / "input.json"
    assert snapshot.codes == ("000001", "600519")


def test_load_snapshot_rejects_tampered_content_hash(tmp_path, bars):
    path = write_snapshot(
        tmp_path,
        report_date=date(2026, 9, 4),
        bars_by_code={"600519": bars},
        generated_at=GENERATED_AT,
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    document["bars_by_code"]["600519"][-1]["close"] = 999.0
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(SnapshotError, match="content hash mismatch"):
        load_snapshot(path)


def test_load_snapshot_rejects_unsorted_or_duplicated_code_metadata(tmp_path, bars):
    path = write_snapshot(
        tmp_path,
        report_date=date(2026, 9, 4),
        bars_by_code={"600519": bars, "000001": bars},
        generated_at=GENERATED_AT,
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    document["codes"] = ["600519", "000001", "000001"]
    document["content_hash"] = _content_hash(document)
    path.write_text(_canonical_json(document), encoding="utf-8")

    with pytest.raises(SnapshotError, match="sorted and unique"):
        load_snapshot(path)


def test_load_snapshot_rejects_unsupported_schema_version(tmp_path, bars):
    path = write_snapshot(
        tmp_path,
        report_date=date(2026, 9, 4),
        bars_by_code={"600519": bars},
        generated_at=GENERATED_AT,
    )
    document = json.loads(path.read_text(encoding="utf-8"))
    document["schema_version"] = SNAPSHOT_SCHEMA_VERSION + 1
    document["content_hash"] = _content_hash(document)
    path.write_text(_canonical_json(document), encoding="utf-8")

    with pytest.raises(SnapshotError, match="Unsupported snapshot schema version"):
        load_snapshot(path)


def test_write_snapshot_rejects_invalid_bar_payload(tmp_path, bars):
    invalid_bar = bars[-1].model_dump(mode="json")
    invalid_bar["high"] = 0

    with pytest.raises(SnapshotError, match="Invalid bar"):
        write_snapshot(
            tmp_path,
            report_date=date(2026, 9, 4),
            bars_by_code={"600519": [invalid_bar]},
            generated_at=GENERATED_AT,
        )


def test_daily_bar_rejects_invalid_prices_and_naive_source_timestamp(bars):
    payload = bars[-1].model_dump()
    payload["low"] = payload["high"] + 1
    with pytest.raises(ValidationError, match="OHLC"):
        DailyBar.model_validate(payload)

    payload = bars[-1].model_dump()
    payload["source_timestamp"] = bars[-1].source_timestamp.replace(tzinfo=None)
    with pytest.raises(ValidationError, match="timezone-aware"):
        DailyBar.model_validate(payload)


def test_write_snapshot_refuses_to_replace_different_content(tmp_path, bars):
    write_snapshot(
        tmp_path,
        report_date=date(2026, 9, 4),
        bars_by_code={"600519": bars},
        generated_at=GENERATED_AT,
    )
    changed_bars = list(bars)
    changed_bars[-1] = changed_bars[-1].model_copy(update={"close": 1.0, "low": 1.0})

    with pytest.raises(SnapshotConflictError, match="Refusing to overwrite"):
        write_snapshot(
            tmp_path,
            report_date=date(2026, 9, 4),
            bars_by_code={"600519": changed_bars},
            generated_at=GENERATED_AT,
        )


def test_write_snapshot_reuses_existing_normalized_content(tmp_path, bars):
    first_path = write_snapshot(
        tmp_path,
        report_date=date(2026, 9, 4),
        bars_by_code={"600519": bars},
        generated_at=GENERATED_AT,
    )

    second_path = write_snapshot(
        tmp_path,
        report_date=date(2026, 9, 4),
        bars_by_code={"600519": bars},
        generated_at=datetime(2026, 9, 4, 9, 31, tzinfo=UTC),
    )

    assert second_path == first_path
    assert load_snapshot(second_path).generated_at == GENERATED_AT


def test_write_snapshot_rejects_non_string_code_keys(tmp_path, bars):
    with pytest.raises(SnapshotError, match="six digits"):
        write_snapshot(
            tmp_path,
            report_date=date(2026, 9, 4),
            bars_by_code={600519: bars},
            generated_at=GENERATED_AT,
        )


def _canonical_json(document: dict[str, object]) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _content_hash(document: dict[str, object]) -> str:
    payload = {key: value for key, value in document.items() if key != "content_hash"}
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
