"""Failure-isolated orchestration for deterministic full-market scans."""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Protocol

from pydantic import ValidationError

from stock_daily_report.market_scan.filters import (
    filter_history,
    filter_universe_quote,
)
from stock_daily_report.market_scan.models import (
    ConsensusRecord,
    MarketScanArtifact,
    ProfileRankings,
    RankingRecord,
    ScanStatus,
)
from stock_daily_report.market_scan.report import write_scan_artifact
from stock_daily_report.market_scan.scoring import (
    CandidateScores,
    ProfileScore,
    rank_scores,
    score_candidate,
)
from stock_daily_report.models import DailyBar, MarketDataSettings, MarketScanSettings
from stock_daily_report.providers.base import ProviderError
from stock_daily_report.providers.service import (
    AllProvidersFailedError,
    DataQualityError,
)
from stock_daily_report.providers.universe import UniverseQuote


class UniverseProvider(Protocol):
    """Minimal universe provider contract used by the scanner."""

    name: str

    def get_quotes(self) -> list[UniverseQuote]:
        """Return one current bulk quote snapshot."""


class HistoryProvider(Protocol):
    """Minimal direct history provider contract used by the scanner."""

    name: str

    def get_daily_bars(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[DailyBar | Mapping[str, object]]:
        """Return history no later than ``end``."""


@dataclass(frozen=True)
class _CandidateResult:
    quote: UniverseQuote
    status: ScanStatus
    scores: CandidateScores | None
    latest_trade_date: date | None
    provider_name: str | None
    history_hash: str | None
    provider_names: tuple[str, ...]


def scan_market(
    settings: MarketScanSettings,
    universe_provider: UniverseProvider,
    history_provider: object,
    *,
    report_date: date,
    generated_at: datetime | None = None,
    configuration_hash: str | None = None,
) -> MarketScanArtifact:
    """Scan, filter, and rank a universe without allowing one symbol to abort."""

    _require_date(report_date)
    timestamp = _normalize_timestamp(generated_at or datetime.now(UTC))
    quotes = tuple(sorted(universe_provider.get_quotes(), key=lambda quote: quote.code))
    _require_unique_quotes(quotes)
    exclusion_counts: Counter[str] = Counter()
    failure_counts: Counter[str] = Counter()
    statuses: dict[str, ScanStatus] = {}
    candidates: list[UniverseQuote] = []

    for quote in quotes:
        eligibility = filter_universe_quote(
            quote,
            report_date=report_date,
            settings=settings,
        )
        if eligibility.eligible:
            candidates.append(quote)
            continue
        exclusion_counts.update(eligibility.reason_codes)
        statuses[quote.code] = ScanStatus(
            code=quote.code,
            name=quote.name,
            status="universe_excluded",
            reason_codes=eligibility.reason_codes,
        )

    selected = candidates[: settings.max_candidates]
    for quote in candidates[settings.max_candidates :]:
        reason = "candidate_limit_exceeded"
        failure_counts[reason] += 1
        statuses[quote.code] = ScanStatus(
            code=quote.code,
            name=quote.name,
            status="not_processed",
            reason_codes=(reason,),
        )

    results: dict[str, _CandidateResult] = {}
    with ThreadPoolExecutor(max_workers=settings.max_workers) as executor:
        futures = {
            executor.submit(
                _process_candidate,
                quote,
                history_provider,
                report_date,
                settings,
            ): quote.code
            for quote in selected
        }
        for future in as_completed(futures):
            code = futures[future]
            results[code] = future.result()

    trend_scores: list[ProfileScore] = []
    balanced_scores: list[ProfileScore] = []
    valid_results: dict[str, _CandidateResult] = {}
    for code in sorted(results):
        result = results[code]
        statuses[code] = result.status
        if result.status.status == "history_excluded":
            exclusion_counts.update(result.status.reason_codes)
        elif result.status.status == "history_failed":
            failure_counts.update(result.status.reason_codes)
        elif result.scores is not None:
            valid_results[code] = result
            trend_scores.append(result.scores.trend)
            balanced_scores.append(result.scores.balanced)

    eligible_count = len(candidates)
    valid_count = len(valid_results)
    coverage = valid_count / eligible_count if eligible_count else 0.0
    rankings = ProfileRankings()
    consensus: tuple[ConsensusRecord, ...] = ()
    scan_complete = len(selected) == len(candidates)
    if scan_complete and coverage >= settings.minimum_coverage_ratio:
        trend = _ranking_records(
            rank_scores(trend_scores),
            valid_results,
            limit=min(settings.trend_limit, 30),
        )
        balanced = _ranking_records(
            rank_scores(balanced_scores),
            valid_results,
            limit=min(settings.balanced_limit, 30),
        )
        rankings = ProfileRankings(trend=trend, balanced=balanced)
        consensus = _consensus(rankings)

    provider_names = {
        _provider_name(universe_provider),
        _provider_name(history_provider),
    }
    for result in results.values():
        provider_names.update(result.provider_names)
    normalized_statuses = tuple(statuses[code] for code in sorted(statuses))
    return MarketScanArtifact(
        rule_version=settings.rule_version,
        report_date=report_date,
        generated_at=timestamp,
        universe_count=len(quotes),
        eligible_count=eligible_count,
        valid_count=valid_count,
        coverage=coverage,
        exclusion_counts=dict(sorted(exclusion_counts.items())),
        failure_counts=dict(sorted(failure_counts.items())),
        rankings=rankings,
        consensus=consensus,
        statuses=normalized_statuses,
        config_hash=configuration_hash or market_scan_config_hash(settings),
        input_hash=_input_hash(quotes, results, report_date),
        provider_names=tuple(sorted(name for name in provider_names if name)),
    )


def run_market_scan(
    settings: MarketScanSettings,
    universe_provider: UniverseProvider,
    history_provider: object,
    *,
    report_date: date,
    output_root: str | Path,
    generated_at: datetime | None = None,
    configuration_hash: str | None = None,
) -> Path:
    """Run one scan and persist its immutable date-partitioned artifact."""

    artifact = scan_market(
        settings,
        universe_provider,
        history_provider,
        report_date=report_date,
        generated_at=generated_at,
        configuration_hash=configuration_hash,
    )
    return write_scan_artifact(output_root, artifact)


def _process_candidate(
    quote: UniverseQuote,
    history_provider: object,
    report_date: date,
    settings: MarketScanSettings,
) -> _CandidateResult:
    try:
        raw_bars, provider_name = _fetch_history(
            history_provider,
            quote.code,
            report_date,
        )
        bars = tuple(_normalize_bar(bar) for bar in raw_bars)
    except DataQualityError as error:
        reasons = tuple(dict.fromkeys((*error.quality.issue_codes, error.code)))
        return _excluded_result(
            quote,
            reasons=reasons,
            provider_name=error.provider,
        )
    except AllProvidersFailedError as error:
        return _failed_result(
            quote,
            reason=error.code,
            provider_name=error.provider,
            provider_names=tuple(
                sorted(
                    {
                        failure.provider
                        for failure in error.failures
                        if failure.provider
                    }
                )
            ),
        )
    except ProviderError as error:
        return _failed_result(
            quote,
            reason=error.code,
            provider_name=error.provider,
        )
    except (KeyError, TypeError, ValueError, ValidationError):
        return _failed_result(
            quote,
            reason="history_data_invalid",
            provider_name=_provider_name(history_provider),
        )
    except Exception:  # noqa: BLE001
        return _failed_result(
            quote,
            reason="history_fetch_failed",
            provider_name=_provider_name(history_provider),
        )

    history_hash = _hash_json([_stable_bar_payload(bar) for bar in bars])
    providers = tuple(sorted({bar.provider_name for bar in bars} | {provider_name}))
    eligibility = filter_history(
        quote.code,
        bars,
        report_date=report_date,
        settings=settings,
    )
    if not eligibility.eligible:
        return _CandidateResult(
            quote=quote,
            status=ScanStatus(
                code=quote.code,
                name=quote.name,
                status="history_excluded",
                reason_codes=eligibility.reason_codes,
                provider_name=provider_name,
            ),
            scores=None,
            latest_trade_date=None,
            provider_name=provider_name,
            history_hash=history_hash,
            provider_names=providers,
        )
    try:
        scores = score_candidate(quote.code, bars, as_of=report_date)
    except Exception:  # noqa: BLE001
        return _CandidateResult(
            quote=quote,
            status=ScanStatus(
                code=quote.code,
                name=quote.name,
                status="history_failed",
                reason_codes=("scoring_failed",),
                provider_name=provider_name,
            ),
            scores=None,
            latest_trade_date=None,
            provider_name=provider_name,
            history_hash=history_hash,
            provider_names=providers,
        )
    return _CandidateResult(
        quote=quote,
        status=ScanStatus(
            code=quote.code,
            name=quote.name,
            status="valid",
            reason_codes=(),
            provider_name=provider_name,
        ),
        scores=scores,
        latest_trade_date=max(bar.trade_date for bar in bars),
        provider_name=provider_name,
        history_hash=history_hash,
        provider_names=providers,
    )


def _fetch_history(
    history_provider: object,
    code: str,
    report_date: date,
) -> tuple[Sequence[DailyBar | Mapping[str, object]], str]:
    fetch = getattr(history_provider, "fetch", None)
    if callable(fetch):
        fetched = fetch(code, end=report_date, as_of=report_date)
        return fetched.bars, fetched.provider_name
    get_daily_bars = getattr(history_provider, "get_daily_bars", None)
    if not callable(get_daily_bars):
        raise TypeError("history provider must expose fetch or get_daily_bars")
    return get_daily_bars(code, end=report_date), _provider_name(history_provider)


def _normalize_bar(value: DailyBar | Mapping[str, object]) -> DailyBar:
    if isinstance(value, DailyBar):
        return value
    if not isinstance(value, Mapping):
        raise TypeError("history rows must be DailyBar instances or mappings")
    return DailyBar.model_validate(value)


def _failed_result(
    quote: UniverseQuote,
    *,
    reason: str,
    provider_name: str,
    provider_names: tuple[str, ...] = (),
) -> _CandidateResult:
    providers = tuple(
        sorted({name for name in (*provider_names, provider_name) if name})
    )
    return _CandidateResult(
        quote=quote,
        status=ScanStatus(
            code=quote.code,
            name=quote.name,
            status="history_failed",
            reason_codes=(reason,),
            provider_name=provider_name,
            provider_names=provider_names,
        ),
        scores=None,
        latest_trade_date=None,
        provider_name=provider_name,
        history_hash=None,
        provider_names=providers,
    )


def _excluded_result(
    quote: UniverseQuote,
    *,
    reasons: tuple[str, ...],
    provider_name: str,
) -> _CandidateResult:
    return _CandidateResult(
        quote=quote,
        status=ScanStatus(
            code=quote.code,
            name=quote.name,
            status="history_excluded",
            reason_codes=reasons,
            provider_name=provider_name,
        ),
        scores=None,
        latest_trade_date=None,
        provider_name=provider_name,
        history_hash=None,
        provider_names=(provider_name,) if provider_name else (),
    )


def _ranking_records(
    scores: Sequence[ProfileScore],
    results: Mapping[str, _CandidateResult],
    *,
    limit: int,
) -> tuple[RankingRecord, ...]:
    records: list[RankingRecord] = []
    for rank, score in enumerate(scores[:limit], start=1):
        result = results[score.code]
        records.append(
            RankingRecord(
                code=score.code,
                name=result.quote.name,
                profile=score.profile,
                rank=rank,
                score=score.total,
                components={
                    "trend": score.components.trend,
                    "momentum": score.components.momentum,
                    "volume": score.components.volume,
                    "structure": score.components.structure,
                    "risk": score.components.risk,
                },
                evidence_codes=score.evidence_codes,
                risk_codes=score.risk_codes,
                latest_trade_date=result.latest_trade_date,
                provider_name=result.provider_name,
            )
        )
    return tuple(records)


def _consensus(rankings: ProfileRankings) -> tuple[ConsensusRecord, ...]:
    balanced = {record.code: record for record in rankings.balanced}
    records = []
    for trend in rankings.trend:
        if trend.code not in balanced:
            continue
        other = balanced[trend.code]
        records.append(
            ConsensusRecord(
                code=trend.code,
                name=trend.name,
                trend_rank=trend.rank,
                balanced_rank=other.rank,
                trend_score=trend.score,
                balanced_score=other.score,
                latest_trade_date=trend.latest_trade_date,
                provider_name=trend.provider_name,
            )
        )
    return tuple(records)


def _input_hash(
    quotes: Sequence[UniverseQuote],
    results: Mapping[str, _CandidateResult],
    report_date: date,
) -> str:
    return _hash_json(
        {
            "report_date": report_date.isoformat(),
            "quotes": [
                {
                    "code": quote.code,
                    "name": quote.name,
                    "market": quote.market,
                    "latest_price": quote.latest_price,
                    "volume": quote.volume,
                    "amount": quote.amount,
                    "quote_date": quote.quote_date.isoformat(),
                }
                for quote in quotes
            ],
            "histories": {
                code: {
                    "history_hash": result.history_hash,
                    "status": result.status.status,
                    "reason_codes": result.status.reason_codes,
                    "provider_name": result.provider_name,
                }
                for code, result in sorted(results.items())
            },
        }
    )


def _stable_bar_payload(bar: DailyBar) -> dict[str, object]:
    return {
        "trade_date": bar.trade_date.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
        "amount": bar.amount,
        "turnover_rate": bar.turnover_rate,
        "adjustment_mode": bar.adjustment_mode,
        "provider_name": bar.provider_name,
    }


def _hash_json(value: object) -> str:
    encoded = json.dumps(
        value,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def market_scan_config_hash(
    settings: MarketScanSettings,
    market_data_settings: MarketDataSettings | None = None,
) -> str:
    """Return the canonical identity hash for market-scan configuration."""

    scan_configuration = settings.model_dump(mode="json")
    if market_data_settings is None:
        return _hash_json(scan_configuration)
    return _hash_json(
        {
            "market_data": market_data_settings.model_dump(mode="json"),
            "market_scan": scan_configuration,
        }
    )


def _provider_name(provider: object) -> str:
    name = getattr(provider, "name", provider.__class__.__name__)
    return str(name).strip() or provider.__class__.__name__


def _require_unique_quotes(quotes: Sequence[UniverseQuote]) -> None:
    codes = [quote.code for quote in quotes]
    if len(codes) != len(set(codes)):
        raise ValueError("universe provider returned duplicate stock codes")


def _require_date(value: date) -> None:
    if isinstance(value, datetime) or not isinstance(value, date):
        raise TypeError("report_date must be a date, not a datetime")


def _normalize_timestamp(value: datetime) -> datetime:
    if not isinstance(value, datetime):
        raise TypeError("generated_at must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("generated_at must be timezone-aware")
    return value.astimezone(UTC)


__all__ = [
    "HistoryProvider",
    "UniverseProvider",
    "market_scan_config_hash",
    "run_market_scan",
    "scan_market",
]
