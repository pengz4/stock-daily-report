"""Configured provider selection, validated retrieval, and safe local caching."""

import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import ValidationError

from stock_daily_report.config import ConfigurationError
from stock_daily_report.models import DailyBar, MarketDataSettings
from stock_daily_report.providers.base import (
    MarketDataProvider,
    ProviderAvailabilityError,
    ProviderDataError,
    ProviderError,
)
from stock_daily_report.quality.checks import (
    BarInput,
    DataQualityIssue,
    DataQualityResult,
    DataQualitySettings,
    validate_bars,
)

_DAILY_BAR_FIELDS = frozenset(DailyBar.model_fields)
_SENSITIVE_KEY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "token",
        "secret",
        "client_secret",
        "password",
        "passwd",
        "credential",
        "cookie",
    }
)
_MAX_RECOVERY_MANIFESTS = 32
_MAX_RECOVERY_ENTRIES = 256
_MAX_RECOVERY_ENTRY_BYTES = 64 * 1024 * 1024
_MAX_RECOVERY_TOTAL_BYTES = 256 * 1024 * 1024
_MAX_RECOVERY_MANIFEST_BYTES = 1024 * 1024
_RECOVERY_CHUNK_BYTES = 1024 * 1024


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


class DataQualityError(ProviderDataError):
    """Raised when a provider returned data that may not enter analysis."""

    def __init__(self, provider: str, quality: DataQualityResult) -> None:
        self.quality = quality
        super().__init__(
            provider,
            "data_quality_rejected",
            f"Data quality rejected {quality.code}: {', '.join(quality.issue_codes)}",
        )


class AllProvidersFailedError(ProviderError):
    """Raised after both configured providers had expected operational failures."""

    def __init__(self, failures: Sequence[ProviderError]) -> None:
        self.failures = tuple(failures)
        detail = "; ".join(str(failure) for failure in self.failures)
        super().__init__("selection", "all_providers_failed", detail)


class CacheRollbackError(RuntimeError):
    """Raised when a cache commit cannot restore its preimages."""

    def __init__(self, recovery_path: Path | None, detail: str) -> None:
        self.recovery_path = recovery_path
        retained_at = str(recovery_path) if recovery_path is not None else "unavailable"
        super().__init__(
            "Cache rollback failed; recovery artifacts retained at "
            f"{retained_at}: {detail}"
        )


@dataclass(frozen=True)
class FetchedBars:
    """Validated bars plus the provider and cache provenance."""

    code: str
    provider_name: str
    bars: tuple[DailyBar, ...]
    quality: DataQualityResult
    from_cache: bool


class RawResponseCache:
    """A TTL cache of redacted provider responses below a configured local path.

    Atomic writes and staged batch commits use one stable cache lock so a
    rollback cannot remove another writer's entry.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        ttl_seconds: int,
        secrets: Sequence[str] = (),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if ttl_seconds < 0:
            raise ValueError("cache TTL must not be negative")
        self._directory = Path(directory)
        self._ttl_seconds = ttl_seconds
        self._secrets = tuple(secret for secret in secrets if secret)
        self._now = now or (lambda: datetime.now(UTC))
        self._recover_pending_manifests()

    def load(
        self, provider: str, code: str, start: date | None, end: date | None
    ) -> object | None:
        """Load only a well-formed, unexpired response for this exact request."""

        if not self._directory.exists():
            return None
        path = self._path_for(provider, code, start, end)
        with self._write_lock():
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
                cached_at = datetime.fromisoformat(document["cached_at"])
                if cached_at.tzinfo is None or document["key"] != self._key(
                    provider, code, start, end
                ):
                    raise ValueError("invalid cache metadata")
                age = (self._now() - cached_at).total_seconds()
                if age < 0 or age > self._ttl_seconds:
                    self._unlink_and_fsync(path)
                    return None
                return document["response"]
            except (
                FileNotFoundError,
                OSError,
                ValueError,
                KeyError,
                TypeError,
                json.JSONDecodeError,
            ):
                self._unlink_and_fsync(path)
                return None

    def store(
        self,
        provider: str,
        code: str,
        start: date | None,
        end: date | None,
        response: object,
    ) -> None:
        """Persist canonical redacted JSON for a response already quality-gated."""

        document = {
            "cached_at": self._now().astimezone(UTC).isoformat(),
            "key": self._key(provider, code, start, end),
            "response": _redact(response, self._secrets),
        }
        with self._write_lock():
            self._store_unlocked(provider, code, start, end, document)

    @contextmanager
    def _write_lock(self):
        self._directory.mkdir(parents=True, exist_ok=True)
        lock_path = self._directory / ".cache.lock"
        lock_file = lock_path.open("a", encoding="utf-8")
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        finally:
            lock_file.close()

    def _store_unlocked(
        self,
        provider: str,
        code: str,
        start: date | None,
        end: date | None,
        document: Mapping[str, object],
    ) -> None:
        self._directory.mkdir(parents=True, exist_ok=True)
        path = self._path_for(provider, code, start, end)
        content = json.dumps(
            document, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self._directory,
                delete=False,
                prefix=f".{path.name}.",
                suffix=".tmp",
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
            _fsync_directory(self._directory)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
                _fsync_directory(self._directory)

    def _write_bytes_atomic(self, path: Path, content: bytes) -> None:
        """Write bytes with an atomic replacement and content verification."""

        path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=path.parent,
                delete=False,
                prefix=f".{path.name}.",
                suffix=".restore.tmp",
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, path)
            _fsync_directory(path.parent)
            restored = path.read_bytes()
            if restored != content:
                raise OSError(f"Cache restoration verification failed: {path}")
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
                _fsync_directory(path.parent)

    def _unlink_and_fsync(self, path: Path) -> None:
        try:
            path.unlink()
        except FileNotFoundError:
            return
        _fsync_directory(path.parent)

    def _recover_pending_manifests(self) -> None:
        if not self._directory.exists():
            return
        with self._write_lock():
            recovery_paths = sorted(self._directory.glob(".cache-recovery-*"))
            if len(recovery_paths) > _MAX_RECOVERY_MANIFESTS:
                raise CacheRollbackError(
                    recovery_paths[0],
                    f"too many recovery manifests (limit {_MAX_RECOVERY_MANIFESTS})",
                )
            for recovery_path in recovery_paths:
                if not recovery_path.is_dir() or recovery_path.is_symlink():
                    raise CacheRollbackError(
                        recovery_path,
                        "recovery manifest path is not a directory",
                    )
                try:
                    self._replay_recovery_manifest(recovery_path)
                except CacheRollbackError:
                    raise
                except Exception as error:
                    raise CacheRollbackError(recovery_path, str(error)) from error

    def _replay_recovery_manifest(self, recovery_path: Path) -> None:
        manifest_path = recovery_path / "manifest.json"
        try:
            if manifest_path.is_symlink() or (
                manifest_path.stat().st_size > _MAX_RECOVERY_MANIFEST_BYTES
            ):
                raise OSError("cache recovery manifest exceeds the size limit")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OSError(
                f"Could not read cache recovery manifest: {manifest_path}"
            ) from error
        if (
            not isinstance(manifest, dict)
            or not {"schema_version", "state", "entries"} <= set(manifest)
            or manifest["schema_version"] != 2
            or not isinstance(manifest["entries"], list)
        ):
            raise OSError(f"Invalid cache recovery manifest: {manifest_path}")
        state = manifest["state"]
        if state not in {
            "copying",
            "preparing",
            "prepared",
            "committing",
            "ready",
            "restoring",
            "committed",
            "rolled_back",
        }:
            raise OSError(f"Invalid cache recovery state: {state!r}")
        if len(manifest["entries"]) > _MAX_RECOVERY_ENTRIES:
            raise OSError(
                f"Too many cache recovery entries (limit {_MAX_RECOVERY_ENTRIES})"
            )

        entries: list[tuple[Path, bool, Path | None, int | None, str | None, str]] = []
        recovery_names = {"manifest.json"}
        entry_names: set[str] = set()
        total_bytes = 0
        for raw_entry in manifest["entries"]:
            if (
                not isinstance(raw_entry, dict)
                or not {"path", "present", "size", "sha256"} <= set(raw_entry)
            ):
                raise OSError(f"Invalid cache recovery entry: {manifest_path}")
            name = raw_entry["path"]
            present = raw_entry["present"]
            size = raw_entry["size"]
            digest = raw_entry["sha256"]
            status = raw_entry.get("status", "pending")
            if (
                not isinstance(name, str)
                or not name
                or Path(name).name != name
                or name in {".", ".."}
                or name in entry_names
                or name == ".cache.lock"
                or Path(name).is_absolute()
                or not isinstance(present, bool)
                or status not in {"pending", "committed", "restored"}
            ):
                raise OSError(f"Invalid cache recovery path: {name!r}")
            entry_names.add(name)
            if present:
                if (
                    not isinstance(size, int)
                    or isinstance(size, bool)
                    or size < 0
                    or size > _MAX_RECOVERY_ENTRY_BYTES
                    or not isinstance(digest, str)
                    or len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                ):
                    raise OSError(f"Invalid cache recovery metadata: {name}")
                source = recovery_path / name
                total_bytes += size
                if total_bytes > _MAX_RECOVERY_TOTAL_BYTES:
                    raise OSError(
                        "Cache recovery preimages exceed the total size limit"
                    )
                if not source.is_file() or source.is_symlink():
                    raise OSError(f"Missing cache recovery preimage: {source}")
                if source.stat().st_size != size:
                    raise OSError(f"Cache recovery preimage size mismatch: {source}")
                if not self._verify_file(source, size, digest):
                    raise OSError(
                        f"Cache recovery preimage verification failed: {source}"
                    )
                recovery_names.add(name)
            else:
                if size is not None or digest is not None:
                    raise OSError(f"Invalid absent cache recovery entry: {name}")
                source = None
            entries.append((self._directory / name, present, source, size, digest, name))

        actual_names = {path.name for path in recovery_path.iterdir()}
        if actual_names != recovery_names:
            raise OSError(
                f"Unexpected files in cache recovery directory: {recovery_path}"
            )

        if state in {"copying", "preparing"}:
            shutil.rmtree(recovery_path)
            _fsync_directory(self._directory)
            return
        if state == "committed" and self._publication_commit_is_durable(manifest):
            shutil.rmtree(recovery_path)
            _fsync_directory(self._directory)
            return
        if state == "rolled_back":
            shutil.rmtree(recovery_path)
            _fsync_directory(self._directory)
            return

        manifest["state"] = "restoring"
        self._persist_recovery_manifest(recovery_path, manifest)
        for target, present, source, size, digest, name in entries:
            if present:
                assert source is not None and size is not None and digest is not None
                self._restore_file_from_source(
                    source, target, expected_size=size, expected_digest=digest
                )
                if not self._verify_file(target, size, digest):
                    raise OSError(f"Cache recovery verification failed: {target}")
            else:
                self._unlink_and_fsync(target)
                if target.exists():
                    raise OSError(f"Cache entry remains after recovery cleanup: {name}")
            for raw_entry in manifest["entries"]:
                if raw_entry["path"] == name:
                    raw_entry["status"] = "restored"
                    break
            self._persist_recovery_manifest(recovery_path, manifest)

        manifest["state"] = "rolled_back"
        self._persist_recovery_manifest(recovery_path, manifest)

        shutil.rmtree(recovery_path)
        _fsync_directory(self._directory)

    @staticmethod
    def _publication_commit_is_durable(manifest: Mapping[str, object]) -> bool:
        publication_manifest = manifest.get("publication_manifest")
        if publication_manifest is None:
            return True
        if not isinstance(publication_manifest, str):
            return False
        try:
            document = json.loads(
                Path(publication_manifest).read_text(encoding="utf-8")
            )
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return False
        return isinstance(document, dict) and document.get("state") == "committed"

    def _persist_recovery_manifest(
        self, recovery_path: Path, manifest: Mapping[str, object]
    ) -> None:
        self._write_bytes_atomic(
            recovery_path / "manifest.json",
            json.dumps(manifest, sort_keys=True).encode("utf-8"),
        )

    @staticmethod
    def _verify_file(path: Path, expected_size: int, expected_digest: str) -> bool:
        if path.is_symlink() or not path.is_file():
            return False
        try:
            if path.stat().st_size != expected_size:
                return False
            digest = hashlib.sha256()
            size = 0
            with path.open("rb") as source:
                while chunk := source.read(_RECOVERY_CHUNK_BYTES):
                    size += len(chunk)
                    digest.update(chunk)
            return size == expected_size and digest.hexdigest() == expected_digest
        except OSError:
            return False

    def _restore_file_from_source(
        self,
        source: Path,
        target: Path,
        *,
        expected_size: int,
        expected_digest: str,
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        digest = hashlib.sha256()
        copied = 0
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=target.parent,
                delete=False,
                prefix=f".{target.name}.",
                suffix=".restore.tmp",
            ) as temporary:
                temporary_path = Path(temporary.name)
                with source.open("rb") as source_file:
                    while chunk := source_file.read(_RECOVERY_CHUNK_BYTES):
                        copied += len(chunk)
                        if copied > expected_size:
                            raise OSError(f"Cache recovery preimage exceeds declared size: {source}")
                        digest.update(chunk)
                        temporary.write(chunk)
                if (
                    copied != expected_size
                    or digest.hexdigest() != expected_digest
                ):
                    raise OSError(
                        f"Cache recovery preimage verification failed: {source}"
                    )
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, target)
            _fsync_directory(target.parent)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
                _fsync_directory(target.parent)

    def _restore_bytes_atomic(self, path: Path, content: bytes) -> None:
        """Restore bytes with an atomic replacement and content verification."""

        self._write_bytes_atomic(path, content)

    def _path_for(
        self, provider: str, code: str, start: date | None, end: date | None
    ) -> Path:
        digest = hashlib.sha256(
            self._key(provider, code, start, end).encode("utf-8")
        ).hexdigest()
        return self._directory / f"{digest}.json"

    @staticmethod
    def _key(provider: str, code: str, start: date | None, end: date | None) -> str:
        return json.dumps(
            {
                "code": code,
                "end": end.isoformat() if end else None,
                "provider": provider,
                "start": start.isoformat() if start else None,
            },
            separators=(",", ":"),
            sort_keys=True,
        )


class MarketDataService:
    """Fetch configured primary then fallback data; invalid primary data stops here."""

    def __init__(
        self,
        providers: Mapping[str, MarketDataProvider],
        *,
        primary_provider: str,
        fallback_provider: str,
        cache_directory: str | Path,
        cache_ttl_seconds: int,
        quality_settings: DataQualitySettings | None = None,
        secrets: Sequence[str] = (),
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if primary_provider == fallback_provider:
            raise ConfigurationError("primary and fallback providers must differ")
        self._providers = dict(providers)
        for provider_name in (primary_provider, fallback_provider):
            if provider_name not in self._providers:
                raise ConfigurationError(
                    f"Configured provider is unavailable: {provider_name}"
                )
        self._selection = (primary_provider, fallback_provider)
        self._quality_settings = quality_settings or DataQualitySettings()
        self._cache = RawResponseCache(
            cache_directory,
            ttl_seconds=cache_ttl_seconds,
            secrets=secrets,
            now=now,
        )
        self._staged_cache_writes: list[tuple[str, str, date | None, date | None, object]] = []
        self._cache_recovery_path: Path | None = None
        self._publication_manifest_path: Path | None = None

    @classmethod
    def from_settings(
        cls,
        providers: Mapping[str, MarketDataProvider],
        settings: MarketDataSettings,
        *,
        secrets: Sequence[str] = (),
        now: Callable[[], datetime] | None = None,
    ) -> "MarketDataService":
        """Build the fixed provider selection directly from loaded settings."""

        return cls(
            providers,
            primary_provider=settings.primary_provider,
            fallback_provider=settings.fallback_provider,
            cache_directory=settings.cache_directory,
            cache_ttl_seconds=settings.cache_ttl_seconds,
            quality_settings=DataQualitySettings(
                minimum_history_bars=settings.minimum_history_bars,
                max_completed_trading_day_lag=settings.max_completed_trading_day_lag,
            ),
            secrets=secrets,
            now=now,
        )

    def fetch(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
        as_of: date,
        defer_cache: bool = False,
    ) -> FetchedBars:
        """Return validated data from exactly the configured primary/fallback order.

        By default, successful uncached responses are persisted immediately for
        standalone retrieval. Callers fetching a batch can defer those writes
        and commit them only after every required request succeeds.
        """

        failures: list[ProviderAvailabilityError] = []
        for provider_name in self._selection:
            cached_response = self._cache.load(provider_name, code, start, end)
            from_cache = cached_response is not None
            if from_cache:
                response = cached_response
            else:
                try:
                    response = self._providers[provider_name].get_daily_bars(
                        code, start=start, end=end
                    )
                except ProviderAvailabilityError as error:
                    failures.append(error)
                    continue

            raw_bars = _require_sequence(response, provider_name, code, as_of)
            quality = validate_bars(
                code, raw_bars, as_of=as_of, settings=self._quality_settings
            )
            if not quality.analysis_allowed:
                raise DataQualityError(provider_name, quality)
            try:
                normalized_bars = tuple(_normalize_bar(item) for item in raw_bars)
            except (TypeError, ValidationError) as error:
                raise DataQualityError(
                    provider_name,
                    quality.with_issue("invalid_bar", f"Could not normalize bar: {error}"),
                ) from error

            if not from_cache:
                if defer_cache:
                    self._staged_cache_writes.append(
                        (provider_name, code, start, end, response)
                    )
                else:
                    self._cache.store(provider_name, code, start, end, response)
            return FetchedBars(
                code=code,
                provider_name=provider_name,
                bars=normalized_bars,
                quality=quality,
                from_cache=from_cache,
            )
        raise AllProvidersFailedError(failures)

    def commit_staged_cache_writes(self) -> None:
        """Persist deferred responses after publication finalization succeeds.

        All entries are written under the cache lock with per-path preimages,
        so a partial commit restores existing bytes and removes new entries.
        The recovery manifest is durable before the first cache mutation.
        """

        staged_writes = tuple(self._staged_cache_writes)
        if not staged_writes:
            return
        with self._cache._write_lock():
            paths = [
                self._cache._path_for(provider_name, code, start, end)
                for provider_name, code, start, end, _ in staged_writes
            ]
            self._cache_recovery_path = self._create_cache_recovery(paths)
            recovery_path = self._cache_recovery_path
            try:
                manifest = self._load_recovery_manifest_for_update(recovery_path)
                manifest["state"] = "committing"
                self._cache._persist_recovery_manifest(recovery_path, manifest)
                for provider_name, code, start, end, response in staged_writes:
                    path = self._cache._path_for(provider_name, code, start, end)
                    document = {
                        "cached_at": self._cache._now().astimezone(UTC).isoformat(),
                        "key": self._cache._key(provider_name, code, start, end),
                        "response": _redact(response, self._cache._secrets),
                    }
                    self._cache._store_unlocked(
                        provider_name, code, start, end, document
                    )
                    for entry in manifest["entries"]:
                        if entry["path"] == path.name:
                            entry["status"] = "committed"
                            break
                    self._cache._persist_recovery_manifest(recovery_path, manifest)
                manifest["state"] = "committed"
                self._cache._persist_recovery_manifest(recovery_path, manifest)
            except BaseException as commit_error:
                try:
                    self._cache._replay_recovery_manifest(recovery_path)
                    self._cache_recovery_path = None
                except BaseException as restore_error:
                    raise CacheRollbackError(
                        self._cache_recovery_path,
                        f"{commit_error}; {restore_error}",
                    ) from restore_error
                raise
        self._staged_cache_writes = []

    def set_publication_recovery_context(self, manifest_path: Path) -> None:
        """Link cache recovery to the publication transaction journal."""

        self._publication_manifest_path = manifest_path

    def finalize_staged_cache_commit(self) -> None:
        """Discard a committed cache recovery manifest after publication cleanup."""

        recovery_path = self._cache_recovery_path
        if recovery_path is None:
            return
        with self._cache._write_lock():
            manifest = self._load_recovery_manifest_for_update(recovery_path)
            if manifest["state"] != "committed":
                raise CacheRollbackError(
                    recovery_path,
                    f"cannot finalize cache state {manifest['state']!r}",
                )
            manifest["publication_manifest"] = None
            self._cache._persist_recovery_manifest(recovery_path, manifest)
            shutil.rmtree(recovery_path)
            _fsync_directory(self._cache._directory)
            if recovery_path.exists():
                raise CacheRollbackError(
                    recovery_path,
                    "cache recovery artifacts could not be removed",
                )
        self._cache_recovery_path = None

    def rollback_staged_cache_commit(self) -> None:
        """Restore cache preimages after a later publication cleanup failure."""

        recovery_path = self._cache_recovery_path
        if recovery_path is None:
            return
        with self._cache._write_lock():
            manifest = self._load_recovery_manifest_for_update(recovery_path)
            manifest["state"] = "restoring"
            self._cache._persist_recovery_manifest(recovery_path, manifest)
            self._cache._replay_recovery_manifest(recovery_path)
        self._cache_recovery_path = None

    def discard_staged_cache_writes(self) -> None:
        """Drop deferred responses when a batch cannot be completed."""

        self._staged_cache_writes.clear()

    def _load_recovery_manifest_for_update(self, recovery_path: Path) -> dict[str, object]:
        manifest_path = recovery_path / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or not isinstance(
            manifest.get("entries"), list
        ):
            raise OSError(f"Invalid cache recovery manifest: {manifest_path}")
        return manifest

    def _create_cache_recovery(self, paths: Sequence[Path]) -> Path:
        recovery_path = Path(
            tempfile.mkdtemp(prefix=".cache-recovery-", dir=self._cache._directory)
        )
        _fsync_directory(self._cache._directory)
        self._cache_recovery_path = recovery_path
        manifest = {
            "schema_version": 2,
            "state": "preparing",
            "publication_manifest": (
                str(self._publication_manifest_path)
                if self._publication_manifest_path is not None
                else None
            ),
            "entries": [
                {
                    "path": path.name,
                    "present": False,
                    "size": None,
                    "sha256": None,
                    "status": "pending",
                }
                for path in paths
            ],
        }
        self._cache._persist_recovery_manifest(recovery_path, manifest)
        seen_names: set[str] = set()
        total_bytes = 0
        for entry, path in zip(manifest["entries"], paths, strict=True):
            if path.name in seen_names:
                raise OSError(f"Duplicate cache preimage path: {path}")
            seen_names.add(path.name)
            if path.is_symlink():
                raise OSError(f"Invalid cache preimage path: {path}")
            if path.exists():
                if path.is_symlink() or not path.is_file():
                    raise OSError(f"Invalid cache preimage path: {path}")
                size = path.stat().st_size
                if size > _MAX_RECOVERY_ENTRY_BYTES:
                    raise OSError(f"Cache preimage exceeds the size limit: {path}")
                total_bytes += size
                if total_bytes > _MAX_RECOVERY_TOTAL_BYTES:
                    raise OSError("Cache recovery preimages exceed the total size limit")
                size, digest = self._copy_cache_preimage(
                    path, recovery_path / path.name
                )
                entry["present"] = True
                entry["size"] = size
                entry["sha256"] = digest
            self._cache._persist_recovery_manifest(recovery_path, manifest)
        manifest["state"] = "ready"
        self._cache._persist_recovery_manifest(recovery_path, manifest)
        return recovery_path

    def _copy_cache_preimage(self, source: Path, destination: Path) -> tuple[int, str]:
        destination.parent.mkdir(parents=True, exist_ok=True)
        declared_size = source.stat().st_size
        if declared_size > _MAX_RECOVERY_ENTRY_BYTES:
            raise OSError(
                f"Cache preimage exceeds the size limit: {source}"
            )
        temporary_path: Path | None = None
        digest = hashlib.sha256()
        copied = 0
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                dir=destination.parent,
                delete=False,
                prefix=f".{destination.name}.",
                suffix=".preimage.tmp",
            ) as temporary:
                temporary_path = Path(temporary.name)
                with source.open("rb") as source_file:
                    while chunk := source_file.read(_RECOVERY_CHUNK_BYTES):
                        copied += len(chunk)
                        digest.update(chunk)
                        temporary.write(chunk)
                if copied != declared_size:
                    raise OSError(f"Cache preimage changed during copy: {source}")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, destination)
            _fsync_directory(destination.parent)
        finally:
            if temporary_path is not None and temporary_path.exists():
                temporary_path.unlink()
                _fsync_directory(destination.parent)
        return copied, digest.hexdigest()


def _require_sequence(
    response: object, provider_name: str, code: str, as_of: date
) -> Sequence[BarInput]:
    if isinstance(response, Sequence) and not isinstance(response, (str, bytes, bytearray)):
        return response
    raise DataQualityError(
        provider_name,
        DataQualityResult(
            code=code,
            as_of=as_of,
            issues=(
                DataQualityIssue(
                    "invalid_response",
                    f"{provider_name} returned a non-sequence response",
                ),
            ),
            bar_count=0,
            analysis_allowed=False,
        )
    )


def _normalize_bar(bar: BarInput) -> DailyBar:
    if isinstance(bar, DailyBar):
        return bar
    if not isinstance(bar, Mapping):
        raise TypeError("provider response item must be a DailyBar or mapping")
    return DailyBar.model_validate(
        {key: value for key, value in bar.items() if key in _DAILY_BAR_FIELDS}
    )


def _redact(value: object, secrets: Sequence[str]) -> object:
    if isinstance(value, DailyBar):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {
            str(key): _redact(item, secrets)
            for key, item in value.items()
            if not _is_sensitive_key(str(key))
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, secrets) for item in value]
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, str):
        redacted = value
        for secret in secrets:
            redacted = redacted.replace(secret, "***REDACTED***")
        return redacted
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise TypeError(f"Cannot safely serialize cached value of type {type(value).__name__}")


def _is_sensitive_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", key.lower())
    return normalized in {
        item.replace("_", "") for item in _SENSITIVE_KEY_NAMES
    } or normalized.endswith(("secret", "password"))
