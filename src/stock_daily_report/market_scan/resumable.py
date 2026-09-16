"""Resumable orchestration for full-market scans.

Implements the same-day checkpoint/resume lifecycle: eligible candidates are
processed in batches, each batch is persisted to ``progress.json``, and a later
run for the *same* Asia/Shanghai day resumes instead of restarting. Forward
adjusted history is never reused across calendar days.

The final immutable ``scan.json`` artifact is only written once every eligible
candidate has been attempted and coverage reaches the configured threshold,
matching the existing gate in ``runner.scan_market``.
"""

from __future__ import annotations

from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from stock_daily_report.market_scan.models import MarketState
from stock_daily_report.market_scan.resume import (
    ScanCheckpoint,
    _checkpoint_lock,
    archive_checkpoint,
    build_checkpoint,
    checkpoint_completed,
    checkpoint_quotes,
    load_checkpoint,
    manifest_hash_for,
    save_checkpoint,
)
from stock_daily_report.market_scan.runner import (
    _build_market_state,
    _CandidateResult,
    _collect_with_timeout,
    _normalize_timestamp,
    _process_candidate,
    _provider_name,
    scan_market,
    scoring_config_hash,
)
from stock_daily_report.models import MarketScanSettings
from stock_daily_report.providers.universe import UniverseQuote

_SHANGHAI = ZoneInfo("Asia/Shanghai")

__all__ = ["run_resumable_scan"]


def run_resumable_scan(
    settings: MarketScanSettings,
    universe_provider: object,
    history_provider: object,
    *,
    report_date: date,
    output_root: str | Path,
    generated_at: datetime | None = None,
    configuration_hash: str | None = None,
    index_provider: object | None = None,
    source_revision: str = "",
    max_batches: int | None = None,
    batch_size: int | None = None,
    directory: str | Path = "market-scans",
) -> Path | None:
    """Run one resumable scan, returning the artifact path once complete.

    Returns ``None`` when the scan is not yet complete (some eligible candidate
    remains unprocessed) so the caller can persist the checkpoint and retry on
    a later run. The checkpoint is persisted after every batch.
    """

    if max_batches is not None and max_batches <= 0:
        raise ValueError("max_batches must be a positive integer")
    with _checkpoint_lock(output_root, report_date, directory=directory):
        return _run_resumable_scan(
            settings,
            universe_provider,
            history_provider,
            report_date=report_date,
            output_root=output_root,
            generated_at=generated_at,
            configuration_hash=configuration_hash,
            index_provider=index_provider,
            source_revision=source_revision,
            max_batches=max_batches,
            batch_size=batch_size,
            directory=directory,
        )


def _run_resumable_scan(
    settings: MarketScanSettings,
    universe_provider: object,
    history_provider: object,
    *,
    report_date: date,
    output_root: str | Path,
    generated_at: datetime | None = None,
    configuration_hash: str | None = None,
    index_provider: object | None = None,
    source_revision: str = "",
    max_batches: int | None = None,
    batch_size: int | None = None,
    directory: str | Path = "market-scans",
) -> Path | None:
    semantic_hash = scoring_config_hash(settings)
    per_batch = batch_size or settings.max_candidates
    execution_date = _current_shanghai_date()

    checkpoint = load_checkpoint(output_root, report_date, directory=directory)
    if checkpoint is not None:
        manifest_hash = manifest_hash_for(checkpoint_quotes(checkpoint))
        if _is_stale(checkpoint, report_date, execution_date, semantic_hash):
            archive_checkpoint(output_root, checkpoint, directory=directory)
            checkpoint = None

    if checkpoint is not None:
        quotes = checkpoint_quotes(checkpoint)
        market_state = checkpoint.market_state
        manifest_hash = manifest_hash_for(quotes)
        completed = checkpoint_completed(checkpoint)
        last_batch = checkpoint.last_batch
        if checkpoint.state == "complete":
            return _finalize(
                settings,
                universe_provider,
                history_provider,
                quotes,
                completed,
                market_state,
                report_date,
                output_root,
                generated_at,
                configuration_hash,
                index_provider,
                directory=directory,
            )
    else:
        quotes = tuple(
            sorted(universe_provider.get_quotes(), key=lambda quote: quote.code)
        )
        market_state = None
        manifest_hash = manifest_hash_for(quotes)
        completed = {}
        last_batch = 0

    if market_state is None and index_provider is not None:
        market_state = _build_market_state(
            settings,
            index_provider,
            quotes,
            report_date=report_date,
            generated_at=_normalize_timestamp(
                generated_at or datetime.now(UTC)
            ),
            breadth_provider=_provider_name(universe_provider),
        )

    eligible = _eligible_quotes(quotes, report_date, settings)
    eligible_codes = {quote.code for quote in eligible}
    completed = {
        code: result for code, result in completed.items() if code in eligible_codes
    }

    pending = [quote for quote in eligible if quote.code not in completed]
    batch_number = last_batch

    while pending:
        if max_batches is not None and (batch_number - last_batch) >= max_batches:
            break
        if _current_shanghai_date() != execution_date:
            save_checkpoint(
                output_root,
                _make_checkpoint(
                    settings,
                    report_date,
                    execution_date,
                    semantic_hash,
                    manifest_hash,
                    quotes,
                    completed,
                    market_state,
                    "running",
                    source_revision,
                    batch_number,
                ),
                directory=directory,
            )
            return None

        batch = pending[:per_batch]
        pending = pending[per_batch:]
        batch_number += 1

        processed = _process_batch(batch, history_provider, report_date, settings)
        completed.update(processed)

        save_checkpoint(
            output_root,
            _make_checkpoint(
                settings,
                report_date,
                execution_date,
                semantic_hash,
                manifest_hash,
                quotes,
                completed,
                market_state,
                "complete" if not pending else "running",
                source_revision,
                batch_number,
            ),
            directory=directory,
        )

    if pending or _current_shanghai_date() != execution_date:
        return None

    # Persist a running checkpoint before writing the artifact; only mark
    # complete after the immutable artifact writes successfully.
    save_checkpoint(
        output_root,
        _make_checkpoint(
            settings,
            report_date,
            execution_date,
            semantic_hash,
            manifest_hash,
            quotes,
            completed,
            market_state,
            "running",
            source_revision,
            batch_number,
        ),
        directory=directory,
    )

    artifact_path = _finalize(
        settings,
        universe_provider,
        history_provider,
        quotes,
        completed,
        market_state,
        report_date,
        output_root,
        generated_at,
        configuration_hash,
        index_provider,
        directory=directory,
    )

    save_checkpoint(
        output_root,
        _make_checkpoint(
            settings,
            report_date,
            execution_date,
            semantic_hash,
            manifest_hash,
            quotes,
            completed,
            market_state,
            "complete",
            source_revision,
            batch_number,
        ),
        directory=directory,
    )
    return artifact_path


def _is_stale(
    checkpoint: ScanCheckpoint,
    report_date: date,
    execution_date: date,
    semantic_hash: str,
) -> bool:
    return (
        checkpoint.report_date != report_date
        or checkpoint.execution_date != execution_date
        or checkpoint.scoring_config_hash != semantic_hash
    )


def _make_checkpoint(
    settings: MarketScanSettings,
    report_date: date,
    execution_date: date,
    semantic_hash: str,
    manifest_hash: str,
    quotes: list[UniverseQuote],
    completed: Mapping[str, _CandidateResult],
    market_state: MarketState | None,
    state: str,
    source_revision: str,
    batch_number: int,
) -> ScanCheckpoint:
    return build_checkpoint(
        report_date=report_date,
        execution_date=execution_date,
        rule_version=settings.rule_version,
        scoring_config_hash=semantic_hash,
        manifest_hash=manifest_hash,
        universe_quotes=quotes,
        completed=completed.values(),
        market_state=market_state,
        state=state,  # type: ignore[arg-type]
        source_revision=source_revision,
        last_batch=batch_number,
    )


def _eligible_quotes(
    quotes: list[UniverseQuote],
    report_date: date,
    settings: MarketScanSettings,
) -> list[UniverseQuote]:
    from stock_daily_report.market_scan.filters import filter_universe_quote

    return [
        quote
        for quote in quotes
        if filter_universe_quote(quote, report_date=report_date, settings=settings).eligible
    ]


def _process_batch(
    batch: list[UniverseQuote],
    history_provider: object,
    report_date: date,
    settings: MarketScanSettings,
) -> dict[str, _CandidateResult]:
    results: dict[str, _CandidateResult] = {}
    executor = ThreadPoolExecutor(max_workers=settings.max_workers)
    try:
        futures = {
            executor.submit(
                _process_candidate,
                quote,
                history_provider,
                report_date,
                settings,
            ): quote.code
            for quote in batch
        }
        _collect_with_timeout(
            futures,
            results,
            quotes_by_code={quote.code: quote for quote in batch},
            provider_name=_provider_name(history_provider),
        )
    finally:
        executor.shutdown(wait=False, cancel_futures=True)
    return results


def _finalize(
    settings: MarketScanSettings,
    universe_provider: object,
    history_provider: object,
    quotes: list[UniverseQuote],
    completed: Mapping[str, _CandidateResult],
    market_state: MarketState | None,
    report_date: date,
    output_root: str | Path,
    generated_at: datetime | None,
    configuration_hash: str | None,
    index_provider: object | None,
    directory: str | Path = "market-scans",
) -> Path:
    """Rebuild the immutable artifact from completed results via ``scan_market``."""

    from stock_daily_report.market_scan.report import write_scan_artifact

    artifact = scan_market(
        settings,
        universe_provider,
        history_provider,
        report_date=report_date,
        generated_at=generated_at,
        configuration_hash=configuration_hash,
        index_provider=index_provider,
        market_state=market_state,
        universe_quotes=quotes,
        resume_from=completed,
    )
    return write_scan_artifact(output_root, artifact, directory=directory)


def _current_shanghai_date() -> date:
    return datetime.now(_SHANGHAI).date()
