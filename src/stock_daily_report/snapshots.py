"""Deterministic, immutable JSON input snapshots for daily analysis."""

import fcntl
import hashlib
import hmac
import json
import os
import re
import tempfile
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType

from pydantic import ValidationError

from stock_daily_report.models import DailyBar

SNAPSHOT_SCHEMA_VERSION = 1
_CONTENT_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CODE_PATTERN = re.compile(r"^\d{6}$")
_DOCUMENT_KEYS = {
    "bars_by_code",
    "codes",
    "content_hash",
    "generated_at",
    "provider_metadata",
    "report_date",
    "schema_version",
}


class SnapshotError(ValueError):
    """Raised when a snapshot is invalid, corrupt, or cannot be persisted."""


class SnapshotConflictError(SnapshotError):
    """Raised when immutable snapshot content already exists for a date."""


@dataclass(frozen=True)
class InputSnapshot:
    """Validated immutable representation of a persisted input snapshot."""

    schema_version: int
    report_date: date
    generated_at: datetime
    provider_metadata: Mapping[str, tuple[str, ...]]
    codes: tuple[str, ...]
    bars_by_code: Mapping[str, tuple[DailyBar, ...]]
    content_hash: str


def write_snapshot(
    root_directory: str | Path,
    *,
    report_date: date,
    bars_by_code: Mapping[str, Sequence[DailyBar | Mapping[str, object]]],
    generated_at: datetime | None = None,
) -> Path:
    """Persist a canonical snapshot without overwriting other content.

    ``generated_at`` must be timezone-aware when supplied; it is normalized to
    UTC. The default is the current UTC time. When a snapshot already exists,
    equivalent normalized bar content retains its original generation time and
    is returned unchanged.
    """

    normalized_report_date = _require_date(report_date, "report_date")
    normalized_generated_at = _normalize_timestamp(
        generated_at or datetime.now(UTC), "generated_at"
    )
    normalized_bars = _normalize_bars_by_code(bars_by_code)
    path = (
        Path(root_directory)
        / "snapshots"
        / normalized_report_date.isoformat()
        / "input.json"
    )
    payload = _build_payload(
        normalized_report_date, normalized_generated_at, normalized_bars
    )
    document = {**payload, "content_hash": _content_hash(payload)}
    path.parent.mkdir(parents=True, exist_ok=True)
    with _snapshot_write_lock(path.parent):
        if path.exists():
            existing = load_snapshot(path)
            existing_time_payload = _build_payload(
                normalized_report_date, existing.generated_at, normalized_bars
            )
            if hmac.compare_digest(
                existing.content_hash, _content_hash(existing_time_payload)
            ):
                return path
            raise SnapshotConflictError(
                "Refusing to overwrite immutable snapshot with different content: "
                f"{path}"
            )
        _atomic_write(path, _canonical_json(document))
    return path


def load_snapshot(path: str | Path) -> InputSnapshot:
    """Load, hash-verify, validate, and freeze a snapshot JSON document."""

    snapshot_path = Path(path)
    try:
        raw_document = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise SnapshotError(f"Snapshot file not found: {snapshot_path}") from error
    except UnicodeDecodeError as error:
        raise SnapshotError(f"Snapshot must be valid UTF-8: {snapshot_path}") from error
    except json.JSONDecodeError as error:
        raise SnapshotError(f"Snapshot must be valid JSON: {snapshot_path}") from error
    except OSError as error:
        raise SnapshotError(f"Could not read snapshot: {snapshot_path}") from error

    if not isinstance(raw_document, dict) or set(raw_document) != _DOCUMENT_KEYS:
        raise SnapshotError("Snapshot has unsupported or missing top-level fields")
    content_hash = raw_document["content_hash"]
    if not isinstance(content_hash, str) or not _CONTENT_HASH_PATTERN.fullmatch(
        content_hash
    ):
        raise SnapshotError("Snapshot content_hash must be a lowercase SHA-256 digest")

    payload = {key: value for key, value in raw_document.items() if key != "content_hash"}
    actual_hash = _content_hash(payload)
    if not hmac.compare_digest(content_hash, actual_hash):
        raise SnapshotError("Snapshot content hash mismatch")

    schema_version = raw_document["schema_version"]
    if schema_version != SNAPSHOT_SCHEMA_VERSION:
        raise SnapshotError(f"Unsupported snapshot schema version: {schema_version!r}")
    normalized_report_date = _parse_date(raw_document["report_date"], "report_date")
    normalized_generated_at = _parse_timestamp(
        raw_document["generated_at"], "generated_at"
    )
    codes = _validate_codes(raw_document["codes"])
    normalized_bars = _normalize_bars_by_code(raw_document["bars_by_code"])
    if tuple(sorted(normalized_bars)) != codes:
        raise SnapshotError("Snapshot codes must exactly match bars_by_code keys")
    provider_metadata = _validate_provider_metadata(raw_document["provider_metadata"])

    canonical_payload = _build_payload(
        normalized_report_date, normalized_generated_at, normalized_bars
    )
    if canonical_payload != payload:
        raise SnapshotError("Snapshot payload is not normalized canonical content")
    if provider_metadata != canonical_payload["provider_metadata"]:
        raise SnapshotError("Snapshot provider metadata does not match bar providers")

    return InputSnapshot(
        schema_version=schema_version,
        report_date=normalized_report_date,
        generated_at=normalized_generated_at,
        provider_metadata=MappingProxyType(
            {"providers": tuple(provider_metadata["providers"])}
        ),
        codes=codes,
        bars_by_code=MappingProxyType(normalized_bars),
        content_hash=content_hash,
    )


def _normalize_bars_by_code(
    bars_by_code: Mapping[str, Sequence[DailyBar | Mapping[str, object]]],
) -> dict[str, tuple[DailyBar, ...]]:
    if not isinstance(bars_by_code, Mapping) or not bars_by_code:
        raise SnapshotError("bars_by_code must be a non-empty mapping")

    normalized: dict[str, tuple[DailyBar, ...]] = {}
    for code in sorted(bars_by_code):
        if not isinstance(code, str) or not _CODE_PATTERN.fullmatch(code):
            raise SnapshotError("Snapshot stock codes must be exactly six digits")
        raw_bars = bars_by_code[code]
        if isinstance(raw_bars, (str, bytes)) or not isinstance(raw_bars, Sequence):
            raise SnapshotError(f"Bars for stock code {code} must be a sequence")
        if not raw_bars:
            raise SnapshotError(f"Bars for stock code {code} must not be empty")
        bars = tuple(_normalize_bar(code, raw_bar) for raw_bar in raw_bars)
        normalized[code] = tuple(
            sorted(
                bars,
                key=lambda bar: (bar.trade_date, _canonical_json(bar.model_dump(mode="json"))),
            )
        )
    return normalized


def _normalize_bar(code: str, raw_bar: DailyBar | Mapping[str, object]) -> DailyBar:
    try:
        return (
            raw_bar
            if isinstance(raw_bar, DailyBar)
            else DailyBar.model_validate(raw_bar)
        )
    except ValidationError as error:
        raise SnapshotError(f"Invalid bar for stock code {code}: {error}") from error
    except (TypeError, ValueError) as error:
        raise SnapshotError(f"Invalid bar for stock code {code}: {error}") from error


def _build_payload(
    report_date: date,
    generated_at: datetime,
    bars_by_code: Mapping[str, tuple[DailyBar, ...]],
) -> dict[str, object]:
    codes = sorted(bars_by_code)
    providers = sorted(
        {bar.provider_name for bars in bars_by_code.values() for bar in bars}
    )
    return {
        "schema_version": SNAPSHOT_SCHEMA_VERSION,
        "report_date": report_date.isoformat(),
        "generated_at": _timestamp_to_json(generated_at),
        "provider_metadata": {"providers": providers},
        "codes": codes,
        "bars_by_code": {
            code: [bar.model_dump(mode="json") for bar in bars_by_code[code]]
            for code in codes
        },
    }


def _validate_codes(raw_codes: object) -> tuple[str, ...]:
    if not isinstance(raw_codes, list) or not all(
        isinstance(code, str) and _CODE_PATTERN.fullmatch(code) for code in raw_codes
    ):
        raise SnapshotError("Snapshot codes must be a list of six-digit strings")
    if raw_codes != sorted(set(raw_codes)):
        raise SnapshotError("Snapshot codes must be sorted and unique")
    return tuple(raw_codes)


def _validate_provider_metadata(raw_metadata: object) -> dict[str, list[str]]:
    if not isinstance(raw_metadata, dict) or set(raw_metadata) != {"providers"}:
        raise SnapshotError("Snapshot provider_metadata must contain only providers")
    providers = raw_metadata["providers"]
    if not isinstance(providers, list) or not all(
        isinstance(provider, str) and provider.strip() for provider in providers
    ):
        raise SnapshotError("Snapshot provider metadata must list nonblank providers")
    if providers != sorted(set(providers)):
        raise SnapshotError("Snapshot provider metadata must be sorted and unique")
    return {"providers": providers}


def _require_date(value: object, name: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise SnapshotError(f"{name} must be a date")
    return value


def _parse_date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise SnapshotError(f"Snapshot {name} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise SnapshotError(f"Snapshot {name} must be an ISO date string") from error


def _normalize_timestamp(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime):
        raise SnapshotError(f"{name} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise SnapshotError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _parse_timestamp(value: object, name: str) -> datetime:
    if not isinstance(value, str):
        raise SnapshotError(f"Snapshot {name} must be an ISO timestamp string")
    try:
        return _normalize_timestamp(datetime.fromisoformat(value), name)
    except ValueError as error:
        raise SnapshotError(f"Snapshot {name} must be an ISO timestamp string") from error


def _timestamp_to_json(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _content_hash(payload: Mapping[str, object]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


def _canonical_json(document: Mapping[str, object]) -> str:
    return json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@contextmanager
def _snapshot_write_lock(directory: Path, *, lock_name: str = ".input.lock"):
    lock_path = directory / lock_name
    try:
        lock_file = lock_path.open("a", encoding="utf-8")
    except OSError as error:
        raise SnapshotError(f"Could not lock snapshot directory: {directory}") from error
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            raise SnapshotError(
                f"Could not lock snapshot directory: {directory}"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def _atomic_write(path: Path, content: str) -> None:
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            prefix=".input.json.",
            suffix=".tmp",
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except OSError as error:
        raise SnapshotError(f"Could not write snapshot: {path}") from error
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()
