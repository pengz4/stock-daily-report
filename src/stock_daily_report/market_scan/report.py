"""Immutable JSON persistence for date-partitioned market-scan artifacts."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

from pydantic import ValidationError

from stock_daily_report.market_scan.identity import (
    MarketStateIdentityError,
    resolve_market_state_identity,
)
from stock_daily_report.market_scan.models import MarketScanArtifact
from stock_daily_report.models import MarketStateSettings


class MarketScanArtifactError(ValueError):
    """Raised when a market-scan artifact cannot be loaded or persisted."""


class MarketScanConflictError(MarketScanArtifactError):
    """Raised when different scan content already exists for the same date."""


def write_scan_artifact(
    root_directory: str | Path,
    artifact: MarketScanArtifact,
    *,
    directory: str | Path = "market-scans",
) -> Path:
    """Persist one immutable artifact, reusing a semantically identical rerun."""

    path = (
        Path(root_directory)
        / directory
        / artifact.report_date.isoformat()
        / "scan.json"
    )
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with _scan_write_lock(path.parent):
            if path.exists():
                existing = load_scan_artifact(path)
                if _identity_payload(existing) == _identity_payload(artifact):
                    if (
                        existing.market_state_identity is None
                        or existing.market_state_identity
                        == artifact.market_state_identity
                    ):
                        return path
                    raise MarketScanConflictError(
                        "Refusing to reuse market scan with mismatched "
                        f"market-state identity: {path}"
                    )
                raise MarketScanConflictError(
                    "Refusing to overwrite immutable market scan with different "
                    f"content: {path}"
                )
            _atomic_write(path, _canonical_json(artifact))
    except OSError as error:
        raise MarketScanArtifactError(
            f"Could not persist market scan artifact: {path}"
        ) from error
    return path


def load_scan_artifact(
    path: str | Path,
    *,
    market_state_settings: MarketStateSettings | None = None,
) -> MarketScanArtifact:
    """Load and validate a market-scan artifact.

    When market-state settings are supplied, legacy artifacts without an
    identity are migrated in memory and stored identities are checked against
    their canonical payload.
    """

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
        artifact = MarketScanArtifact.model_validate(document)
    except ValidationError as error:
        raise MarketScanArtifactError(
            f"Invalid market scan artifact: {artifact_path}: {error}"
        ) from error
    if market_state_settings is None:
        return artifact
    try:
        identity = resolve_market_state_identity(
            market_state_settings,
            artifact.market_state,
            artifact.market_state_identity,
        )
    except MarketStateIdentityError as error:
        raise MarketScanArtifactError(
            f"Invalid market-state identity: {artifact_path}: {error}"
        ) from error
    if identity == artifact.market_state_identity:
        return artifact
    return artifact.model_copy(update={"market_state_identity": identity})


def _identity_payload(artifact: MarketScanArtifact) -> dict[str, object]:
    payload = artifact.model_dump(mode="json")
    payload.pop("generated_at")
    payload.pop("market_state_identity", None)
    market_state = payload.get("market_state")
    if isinstance(market_state, dict):
        market_state.pop("generated_at", None)
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
