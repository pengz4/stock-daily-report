"""Configured provider selection, validated retrieval, and safe local caching."""

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import ValidationError

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


@dataclass(frozen=True)
class FetchedBars:
    """Validated bars plus the provider and cache provenance."""

    code: str
    provider_name: str
    bars: tuple[DailyBar, ...]
    quality: DataQualityResult
    from_cache: bool


class RawResponseCache:
    """A TTL cache of redacted provider responses below a configured local path."""

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

    def load(
        self, provider: str, code: str, start: date | None, end: date | None
    ) -> object | None:
        """Load only a well-formed, unexpired response for this exact request."""

        path = self._path_for(provider, code, start, end)
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            cached_at = datetime.fromisoformat(document["cached_at"])
            if cached_at.tzinfo is None or document["key"] != self._key(
                provider, code, start, end
            ):
                raise ValueError("invalid cache metadata")
            age = (self._now() - cached_at).total_seconds()
            if age < 0 or age > self._ttl_seconds:
                path.unlink(missing_ok=True)
                return None
            return document["response"]
        except (FileNotFoundError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
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

        self._directory.mkdir(parents=True, exist_ok=True)
        document = {
            "cached_at": self._now().astimezone(UTC).isoformat(),
            "key": self._key(provider, code, start, end),
            "response": _redact(response, self._secrets),
        }
        self._path_for(provider, code, start, end).write_text(
            json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
            encoding="utf-8",
        )

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
            raise ValueError("primary and fallback providers must differ")
        self._providers = dict(providers)
        for provider_name in (primary_provider, fallback_provider):
            if provider_name not in self._providers:
                raise ValueError(f"Configured provider is unavailable: {provider_name}")
        self._selection = (primary_provider, fallback_provider)
        self._quality_settings = quality_settings or DataQualitySettings()
        self._cache = RawResponseCache(
            cache_directory,
            ttl_seconds=cache_ttl_seconds,
            secrets=secrets,
            now=now,
        )

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
    ) -> FetchedBars:
        """Return validated data from exactly the configured primary/fallback order."""

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
                self._cache.store(provider_name, code, start, end, response)
            return FetchedBars(
                code=code,
                provider_name=provider_name,
                bars=normalized_bars,
                quality=quality,
                from_cache=from_cache,
            )
        raise AllProvidersFailedError(failures)


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
