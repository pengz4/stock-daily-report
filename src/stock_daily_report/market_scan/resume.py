"""Durable checkpoint state for resumable full-market scans.

The checkpoint stores the serialized ``_CandidateResult`` objects produced by
``runner._process_candidate`` together with the normalized universe snapshot, so
a resumed scan can skip already-completed candidates and finish by rebuilding
the immutable artifact through the existing ``scan_market`` path.

Schema v2 adds ``execution_date``: the Asia/Shanghai calendar date captured
when the scan starts. Forward-adjusted history must never be reused across
calendar days, so a v1 checkpoint (without ``execution_date``) is never
resumed and must be archived manually.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from stock_daily_report.market_scan.models import MarketState
from stock_daily_report.providers.universe import UniverseQuote

CHECKPOINT_SCHEMA_VERSION = 2


class CheckpointError(ValueError):
    """Raised when a checkpoint cannot be loaded, validated, or persisted."""


class CheckpointIntegrityError(CheckpointError):
    """Raised for corrupted, unparsable, or unsupported-schema checkpoints.

    These must never be auto-archived or silently resumed; a human must decide.
    """


class CheckpointResumeError(CheckpointError):
    """Raised when a checkpoint is stale (date/hash mismatch) and must be
    archived in favour of a fresh scan."""


class ScanCheckpoint(BaseModel):
    """Durable intermediate state for one report-date scan."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[2] = CHECKPOINT_SCHEMA_VERSION
    report_date: date
    execution_date: date
    rule_version: Literal["market-scan-v1"]
    scoring_config_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    manifest_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_revision: str = ""
    universe_quotes: tuple[dict[str, object], ...]
    completed: tuple[dict[str, object], ...]
    market_state: MarketState | None = None
    state: Literal["running", "complete"]
    updated_at: datetime
    last_batch: int = Field(ge=0)

    @field_validator("updated_at")
    @classmethod
    def require_aware_updated_at(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("updated_at must be timezone-aware")
        return value.astimezone(UTC)


def load_checkpoint(
    root: str | Path,
    report_date: date,
    *,
    directory: str | Path = "market-scans",
) -> ScanCheckpoint | None:
    """Load a persisted checkpoint, or return ``None`` if none exists yet.

    Raises ``CheckpointIntegrityError`` when the file exists but is unparsable,
    has an unsupported schema, or fails model validation.
    """

    path = _checkpoint_path(root, report_date, directory)
    if not path.exists():
        return None
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CheckpointIntegrityError(
            f"Could not read checkpoint: {path}"
        ) from error
    try:
        checkpoint = ScanCheckpoint.model_validate(document)
    except Exception as error:
        raise CheckpointIntegrityError(
            f"Invalid checkpoint: {path}: {error}"
        ) from error
    if checkpoint.manifest_hash != manifest_hash_for_documents(
        checkpoint.universe_quotes
    ):
        raise CheckpointIntegrityError(
            f"Checkpoint manifest hash mismatch: {path}"
        )
    return checkpoint


def save_checkpoint(
    root: str | Path,
    checkpoint: ScanCheckpoint,
    *,
    directory: str | Path = "market-scans",
) -> Path:
    """Atomically persist a checkpoint."""

    path = _checkpoint_path(root, checkpoint.report_date, directory)
    content = _canonical_json(checkpoint)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, content)
    except OSError as error:
        raise CheckpointError(f"Could not persist checkpoint: {path}") from error
    return path


def archive_checkpoint(
    root: str | Path,
    checkpoint: ScanCheckpoint,
    *,
    directory: str | Path = "market-scans",
) -> Path:
    """Atomically move a stale checkpoint into the per-date archive directory.

    The archive filename encodes the checkpoint's ``execution_date`` and its
    ``updated_at`` (as ``YYYYMMDDTHHMMSSZ``) so later runs never collide.
    """

    source = _checkpoint_path(root, checkpoint.report_date, directory)
    directory_path = source.parent / "progress-archive"
    directory_path.mkdir(parents=True, exist_ok=True)
    updated = checkpoint.updated_at.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
    execution = checkpoint.execution_date.isoformat()
    destination = directory_path / f"{execution}-{updated}.json"
    try:
        os.replace(source, destination)
    except OSError as error:
        raise CheckpointError(
            f"Could not archive checkpoint {source} to {destination}"
        ) from error
    return destination


def build_checkpoint(
    *,
    report_date: date,
    execution_date: date,
    rule_version: str,
    scoring_config_hash: str,
    manifest_hash: str,
    universe_quotes: Iterable[UniverseQuote],
    completed: Iterable[object],
    state: Literal["running", "complete"],
    market_state: MarketState | None = None,
    source_revision: str = "",
    last_batch: int = 0,
    updated_at: datetime | None = None,
) -> ScanCheckpoint:
    """Build a checkpoint from universe quotes and candidate results."""

    from stock_daily_report.market_scan.runner import (
        _candidate_result_to_json,
        _quote_to_json,
    )

    return ScanCheckpoint(
        report_date=report_date,
        execution_date=execution_date,
        rule_version=rule_version,  # type: ignore[arg-type]
        scoring_config_hash=scoring_config_hash,
        manifest_hash=manifest_hash,
        source_revision=source_revision,
        universe_quotes=tuple(
            _quote_to_json(quote) for quote in universe_quotes
        ),
        completed=tuple(
            _candidate_result_to_json(result) for result in completed
        ),
        market_state=market_state,
        state=state,
        updated_at=updated_at or datetime.now(UTC),
        last_batch=last_batch,
    )


def manifest_hash_for(quotes: Iterable[UniverseQuote]) -> str:
    """Hash the full normalized universe snapshot for resume validation."""

    return manifest_hash_for_documents(
        [
            {
                "code": quote.code,
                "name": quote.name,
                "market": quote.market,
                "latest_price": quote.latest_price,
                "volume": quote.volume,
                "amount": quote.amount,
                "quote_date": quote.quote_date.isoformat(),
                "change_pct": quote.change_pct,
            }
            for quote in sorted(quotes, key=lambda quote: quote.code)
        ]
    )


def manifest_hash_for_documents(documents: Iterable[Mapping[str, object]]) -> str:
    """Hash already-normalized universe quote documents from a checkpoint."""

    normalized = sorted(
        (
            {
                key: document[key]
                for key in (
                    "code",
                    "name",
                    "market",
                    "latest_price",
                    "volume",
                    "amount",
                    "quote_date",
                    "change_pct",
                )
                if key in document
            }
            for document in documents
        ),
        key=lambda document: str(document.get("code", "")),
    )
    return _hash_json(normalized)


def checkpoint_quotes(checkpoint: ScanCheckpoint) -> list[UniverseQuote]:
    """Rebuild the saved universe snapshot from a checkpoint."""

    from stock_daily_report.market_scan.runner import _quote_from_json

    return [_quote_from_json(document) for document in checkpoint.universe_quotes]


def checkpoint_completed(
    checkpoint: ScanCheckpoint,
) -> dict[str, object]:
    """Rebuild completed candidate results keyed by code from a checkpoint."""

    from stock_daily_report.market_scan.runner import _candidate_result_from_json

    return {
        _result_code(document): _candidate_result_from_json(document)
        for document in checkpoint.completed
    }


def _result_code(document: Mapping[str, object]) -> str:
    status = document.get("status")
    if isinstance(status, Mapping):
        code = status.get("code")
        if isinstance(code, str):
            return code
    code = document.get("code")
    if isinstance(code, str):
        return code
    raise CheckpointIntegrityError("completed candidate is missing its code")


def _checkpoint_path(
    root: str | Path, report_date: date, directory: str | Path = "market-scans"
) -> Path:
    return Path(root) / directory / report_date.isoformat() / "progress.json"


@contextmanager
def _checkpoint_lock(
    root: str | Path,
    report_date: date,
    *,
    directory: str | Path = "market-scans",
):
    checkpoint_directory = _checkpoint_path(root, report_date, directory).parent
    try:
        checkpoint_directory.mkdir(parents=True, exist_ok=True)
        lock_file = (checkpoint_directory / ".progress.lock").open(
            "a",
            encoding="utf-8",
        )
    except OSError as error:
        raise CheckpointError(
            f"Could not lock checkpoint directory: {checkpoint_directory}"
        ) from error
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            raise CheckpointError(
                f"Could not lock checkpoint directory: {checkpoint_directory}"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def _canonical_json(checkpoint: ScanCheckpoint) -> str:
    return (
        json.dumps(
            checkpoint.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


def _atomic_write(path: Path, content: str) -> None:
    temporary_path = path.parent / f".progress.json.{os.getpid()}.{uuid4().hex}.tmp"
    try:
        with temporary_path.open("x", encoding="utf-8") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "CHECKPOINT_SCHEMA_VERSION",
    "CheckpointError",
    "CheckpointIntegrityError",
    "CheckpointResumeError",
    "ScanCheckpoint",
    "archive_checkpoint",
    "build_checkpoint",
    "checkpoint_completed",
    "checkpoint_quotes",
    "load_checkpoint",
    "manifest_hash_for",
    "save_checkpoint",
]
