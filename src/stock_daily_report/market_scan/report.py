"""Immutable JSON persistence for date-partitioned market-scan artifacts."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from stock_daily_report.market_scan.models import MarketScanArtifact


class MarketScanArtifactError(ValueError):
    """Raised when a market-scan artifact cannot be loaded or persisted."""


class MarketScanConflictError(MarketScanArtifactError):
    """Raised when different scan content already exists for the same date."""


def write_scan_artifact(
    root_directory: str | Path,
    artifact: MarketScanArtifact,
) -> Path:
    """Persist one immutable artifact, reusing a semantically identical rerun."""

    path = (
        Path(root_directory)
        / "market-scans"
        / artifact.report_date.isoformat()
        / "scan.json"
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with _scan_write_lock(path.parent):
        if path.exists():
            existing = load_scan_artifact(path)
            if _identity_payload(existing) == _identity_payload(artifact):
                return path
            raise MarketScanConflictError(
                "Refusing to overwrite immutable market scan with different "
                f"content: {path}"
            )
        _atomic_write(path, _canonical_json(artifact))
    return path


def load_scan_artifact(path: str | Path) -> MarketScanArtifact:
    """Load and validate a market-scan artifact."""

    artifact_path = Path(path)
    try:
        document = json.loads(artifact_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise MarketScanArtifactError(
            f"Market scan artifact not found: {artifact_path}"
        ) from error
    except UnicodeDecodeError as error:
        raise MarketScanArtifactError(
            f"Market scan artifact must be valid UTF-8: {artifact_path}"
        ) from error
    except json.JSONDecodeError as error:
        raise MarketScanArtifactError(
            f"Market scan artifact must be valid JSON: {artifact_path}"
        ) from error
    except OSError as error:
        raise MarketScanArtifactError(
            f"Could not read market scan artifact: {artifact_path}"
        ) from error
    try:
        return MarketScanArtifact.model_validate(document)
    except ValidationError as error:
        raise MarketScanArtifactError(
            f"Invalid market scan artifact: {artifact_path}: {error}"
        ) from error


def _identity_payload(artifact: MarketScanArtifact) -> dict[str, object]:
    payload = artifact.model_dump(mode="json")
    payload.pop("generated_at")
    return payload


def _canonical_json(artifact: MarketScanArtifact) -> str:
    return (
        json.dumps(
            artifact.model_dump(mode="json"),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    )


@contextmanager
def _scan_write_lock(directory: Path):
    lock_path = directory / ".scan.lock"
    try:
        lock_file = lock_path.open("a", encoding="utf-8")
    except OSError as error:
        raise MarketScanArtifactError(
            f"Could not lock market scan directory: {directory}"
        ) from error
    try:
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        except OSError as error:
            raise MarketScanArtifactError(
                f"Could not lock market scan directory: {directory}"
            ) from error
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
    finally:
        lock_file.close()


def _atomic_write(path: Path, content: str) -> None:
    temporary_path = path.parent / f".scan.json.{os.getpid()}.{uuid4().hex}.tmp"
    try:
        with temporary_path.open("x", encoding="utf-8") as temporary_file:
            temporary_file.write(content)
            temporary_file.flush()
            os.fsync(temporary_file.fileno())
        os.replace(temporary_path, path)
    except OSError as error:
        raise MarketScanArtifactError(
            f"Could not write market scan artifact: {path}"
        ) from error
    finally:
        if temporary_path.exists():
            temporary_path.unlink()


__all__ = [
    "MarketScanArtifactError",
    "MarketScanConflictError",
    "load_scan_artifact",
    "write_scan_artifact",
]
