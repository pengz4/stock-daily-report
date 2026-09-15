"""Composition layer for the deterministic daily report pipeline."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from math import fsum
from pathlib import Path

from pydantic import ValidationError

from stock_daily_report.chan.common import RULE_VERSION
from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer
from stock_daily_report.config import load_settings, load_watchlist
from stock_daily_report.decision import decide
from stock_daily_report.indicators.technical import (
    TechnicalMetrics,
    calculate_technical_metrics,
)
from stock_daily_report.indicators.trend import classify_trend
from stock_daily_report.market_scan.models import (
    ConsensusRecord,
    MarketScanArtifact,
    MarketState,
    RankingRecord,
)
from stock_daily_report.market_scan.report import (
    MarketScanArtifactError,
    load_scan_artifact,
)
from stock_daily_report.models import (
    DailyBar,
    MarketStateSettings,
    Settings,
    Watchlist,
    WatchlistStock,
)
from stock_daily_report.providers.akshare import AkShareMarketDataProvider
from stock_daily_report.providers.base import MarketDataProvider, ProviderError
from stock_daily_report.providers.fixture import FixtureMarketDataProvider
from stock_daily_report.providers.service import (
    CacheRollbackError,
    FetchedBars,
    MarketDataService,
    RawResponseCache,
)
from stock_daily_report.providers.sina import SinaMarketDataProvider
from stock_daily_report.providers.universe import AkShareUniverseProvider, UniverseQuote
from stock_daily_report.quality.checks import (
    DataQualityResult,
    DataQualitySettings,
    validate_bars,
)
from stock_daily_report.report.models import (
    REPORT_RENDER_VERSION,
    AnalyzerMetadata,
    MarketConsensusRanking,
    MarketRanking,
    MarketRankingComponents,
    MarketRankings,
    MarketRankingsUnavailableReason,
    MarketScanReasonCount,
    MarketSummary,
    PoolOverview,
    PoolOverviewRow,
    ReportDocument,
    ReportMetadata,
    ReportMetrics,
    StockReport,
    StructureLevel,
    StructureSummary,
)
from stock_daily_report.report.render import (
    render_html,
    render_markdown,
    render_site_index,
)
from stock_daily_report.risk.rules import configuration_hash, resolve_risk_rules
from stock_daily_report.snapshots import (
    InputSnapshot,
    SnapshotConflictError,
    SnapshotError,
    _snapshot_write_lock,
    load_snapshot,
    write_snapshot,
)

_MAX_PUBLICATION_TRANSACTIONS = 32
_MAX_PUBLICATION_MANIFEST_BYTES = 1024 * 1024
# Deep analysis covers the top-ranked watchlist stocks; the rest get the
# lightweight pool overview backed by one bulk quote snapshot.
_DEEP_ANALYSIS_LIMIT = 20
_WATCHLIST_SCAN_DIRECTORY = "watchlist-scans"
_PUBLICATION_QUARANTINE_NAME = "quarantine.json"
_MANIFESTLESS_PAYLOAD_ROOTS = frozenset({"reports", "snapshots", "site"})
_PUBLICATION_EVIDENCE_NAMES = frozenset(
    {"backups", "recovery", "cache", "manifest.json", "cleanup.json", "quarantine.json"}
)


def _fsync_directory(directory: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(directory, flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _fsync_file(path: Path) -> None:
    with path.open("rb") as source:
        os.fsync(source.fileno())


def _fsync_tree(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        children = sorted(path.rglob("*"), key=lambda child: len(child.parts))
        for child in children:
            if child.is_file() and not child.is_symlink():
                _fsync_file(child)
        for directory in sorted(
            (
                child
                for child in [path, *children]
                if child.is_dir() and not child.is_symlink()
            ),
            key=lambda child: len(child.parts),
            reverse=True,
        ):
            _fsync_directory(directory)
        return
    _fsync_file(path)
    _fsync_directory(path.parent)


def _replace_and_fsync(source: Path, destination: Path) -> None:
    os.replace(source, destination)
    _fsync_directory(source.parent)
    if destination.parent != source.parent:
        _fsync_directory(destination.parent)


@dataclass(frozen=True)
class PipelineFailure:
    """One watchlist retrieval or quality failure."""

    code: str
    message: str
    issue_codes: tuple[str, ...] = ()


class PipelineError(RuntimeError):
    """Raised when a complete, validated watchlist cannot be published."""

    def __init__(self, failures: Sequence[PipelineFailure]) -> None:
        self.failures = tuple(failures)
        detail = "; ".join(
            f"{failure.code}: {failure.message}" for failure in self.failures
        )
        super().__init__(f"Daily report pipeline failed: {detail}")


class PublicationRollbackError(RuntimeError):
    """Raised when publication recovery cannot be verified."""


@dataclass(frozen=True)
class ReportOutputs:
    """Paths and canonical model returned after a successful publication."""

    json_path: Path
    markdown_path: Path
    html_path: Path
    snapshot_path: Path
    report: ReportDocument


def _split_watchlist_by_priority(
    watchlist: Watchlist,
) -> tuple[list[WatchlistStock], list[WatchlistStock]]:
    """Split the watchlist into (deep-analysis, lightweight) stocks.

    Legacy lists without any explicit priority analyse every stock in depth.
    Once any priority is set, only ``core`` stocks are analysed in depth and
    the remaining ``extended`` stocks receive a lightweight pool-overview row
    sourced from one bulk quote snapshot.
    """
    if not any(stock.priority is not None for stock in watchlist.stocks):
        return list(watchlist.stocks), []
    core = [stock for stock in watchlist.stocks if stock.priority == "core"]
    extended = [stock for stock in watchlist.stocks if stock.priority != "core"]
    if not core:
        raise ValueError(
            "watchlist sets priorities but contains no 'core' stock; mark at "
            "least one stock with priority: core"
        )
    return core, extended


def _fetch_light_quotes(
    watchlist: Watchlist,
    extended_stocks: Sequence[WatchlistStock],
    universe_provider: AkShareUniverseProvider | None,
) -> tuple[dict[str, UniverseQuote], str | None]:
    """Fetch one bulk quote snapshot for the pool overview.

    Core stocks are quoted too, so every watchlist row carries a price. A
    provider failure degrades gracefully: the report still publishes with deep
    analysis for core stocks, and the pool overview records the reason.
    """
    if not extended_stocks:
        return {}, None
    provider = universe_provider or AkShareUniverseProvider()
    try:
        quotes = provider.get_quotes()
    except (ProviderError, ImportError, OSError) as error:
        return {}, str(error)
    wanted = {stock.code for stock in watchlist.stocks}
    return {quote.code: quote for quote in quotes if quote.code in wanted}, None


def _load_watchlist_scan_ranking(
    root: Path, report_date: date, limit: int = _DEEP_ANALYSIS_LIMIT
) -> WatchlistScanInfo | None:
    """Return the watchlist scan ranking and fingerprint, or None.

    Returns ``None`` when no complete, same-day watchlist scan artifact exists
    so the caller can fall back to manual priorities or legacy behaviour.
    """
    scan_path = root / _WATCHLIST_SCAN_DIRECTORY / report_date.isoformat() / "scan.json"
    if not scan_path.exists():
        return None
    try:
        artifact = load_scan_artifact(scan_path)
    except MarketScanArtifactError:
        return None
    if artifact.report_date != report_date:
        return None
    if any(status.status == "not_processed" for status in artifact.statuses):
        return None
    best_scores: dict[str, float] = {}
    for record in (*artifact.rankings.trend, *artifact.rankings.balanced):
        if record.score > best_scores.get(record.code, -1.0):
            best_scores[record.code] = record.score
    if not best_scores:
        return None
    ordered = sorted(best_scores.items(), key=lambda item: (-item[1], item[0]))
    return WatchlistScanInfo(
        ranking=ordered[:limit],
        scan_date=artifact.report_date,
        config_hash=artifact.config_hash,
        input_hash=artifact.input_hash,
    )


@dataclass(frozen=True)
class WatchlistScanInfo:
    """A same-day watchlist scan's ranking and provenance fingerprint."""

    ranking: tuple[tuple[str, float], ...]
    scan_date: date
    config_hash: str
    input_hash: str


def _select_deep_stocks(
    watchlist: Watchlist,
    scan_ranking: Sequence[tuple[str, float]] | None,
) -> tuple[list[WatchlistStock], list[WatchlistStock]]:
    """Split into (deep-analysis, lightweight) stocks.

    A watchlist scan, when available, decides the deep set: its top-ranked
    codes plus any stock manually marked ``priority: core``. Without a scan,
    the existing priority-only rules apply.
    """
    if scan_ranking is None:
        return _split_watchlist_by_priority(watchlist)
    rank_index = {code: index for index, (code, _score) in enumerate(scan_ranking)}
    selected = [
        stock for stock in watchlist.stocks if stock.code in rank_index
    ]
    selected.sort(key=lambda stock: (rank_index[stock.code], stock.code))
    for stock in watchlist.stocks:
        if stock.priority == "core" and stock.code not in rank_index:
            selected.append(stock)
    if not selected:
        return _split_watchlist_by_priority(watchlist)
    selected_codes = {stock.code for stock in selected}
    extended = [stock for stock in watchlist.stocks if stock.code not in selected_codes]
    return selected, extended


def run_daily_report(
    settings: Settings | str | Path,
    *,
    output_root: str | Path,
    watchlist: Watchlist | None = None,
    provider: MarketDataProvider | None = None,
    service: MarketDataService | None = None,
    providers: Mapping[str, MarketDataProvider] | None = None,
    universe_provider: AkShareUniverseProvider | None = None,
    fixture_directory: str | Path | None = None,
    report_date: date | None = None,
    now: Callable[[], datetime] | None = None,
    reuse_existing_snapshot: bool = False,
    overwrite_snapshot: bool = False,
    market_scan_settings: MarketStateSettings | None = None,
) -> ReportOutputs:
    """Fetch, validate, snapshot, analyze, and publish one daily report.

    Retrieval and quality validation complete for every watchlist code before
    the immutable snapshot or any success-shaped report artifact is written.
    Cache-backed retrieval acquires the date, site, and cache locks before
    fetching and holds them through publication and cache finalization.
    Deferred cache writes commit only after publication finalization succeeds.
    """

    active_settings = (
        load_settings(settings) if isinstance(settings, (str, Path)) else settings
    )
    active_watchlist = watchlist or _load_default_watchlist()
    active_market_scan_settings = market_scan_settings or MarketStateSettings()
    generated_at = _normalise_now(now)
    active_report_date = report_date or generated_at.date()
    if not isinstance(active_report_date, date) or isinstance(
        active_report_date, datetime
    ):
        raise TypeError("report_date must be a date, not a datetime")
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    watchlist_scan_info = _load_watchlist_scan_ranking(root, active_report_date)
    watchlist_scan_ranking = (
        watchlist_scan_info.ranking if watchlist_scan_info is not None else None
    )
    core_stocks, extended_stocks = _select_deep_stocks(
        active_watchlist, watchlist_scan_ranking
    )
    failures: list[PipelineFailure] = []
    fetched: dict[str, FetchedBars] = {}
    light_quotes: dict[str, UniverseQuote] = {}
    light_quote_error: str | None = None
    snapshot_for_reuse: InputSnapshot | None = None
    if reuse_existing_snapshot:
        snapshot_for_reuse = load_snapshot(
            root / "snapshots" / active_report_date.isoformat() / "input.json"
        )
        if snapshot_for_reuse.report_date != active_report_date:
            raise SnapshotError(
                "Existing snapshot report_date does not match requested date: "
                f"{active_report_date.isoformat()}"
            )

    active_service = service
    recovery_cache: RawResponseCache | None = None
    if active_service is None and provider is None and snapshot_for_reuse is None:
        active_service = build_market_data_service(
            active_settings,
            providers=providers,
            fixture_directory=fixture_directory,
            now=lambda: generated_at,
            output_root=root,
        )
    elif active_service is None and snapshot_for_reuse is None:
        recovery_cache = _build_recovery_cache(active_settings, root)

    transaction_root: Path | None = None
    try:
        cache_lock = _cache_lock_owner(
            service=active_service, cache=recovery_cache
        )
        _recover_pending_publications_if_idle(
            root,
            active_report_date,
            service=active_service,
            cache=recovery_cache,
            cache_lock=cache_lock,
        )

        def recover_locked() -> None:
            if active_service is not None:
                active_service.recover_pending_cache_manifests(
                    publication_root=root
                )
            elif recovery_cache is not None:
                recovery_cache.recover_pending_manifests(publication_root=root)
            _recover_pending_publications(root)

        def fetch_all() -> None:
            nonlocal light_quotes, light_quote_error
            for stock in sorted(core_stocks, key=lambda item: item.code):
                try:
                    if snapshot_for_reuse is not None:
                        bars = snapshot_for_reuse.bars_by_code.get(stock.code)
                        if bars is None:
                            # The snapshot for a date is frozen, so a code that
                            # was not part of the original publication cannot be
                            # analysed for that same date. It is picked up by the
                            # next report date instead of failing the run.
                            print(
                                f"Skipping {stock.code}: the frozen snapshot for "
                                f"{active_report_date.isoformat()} has no bars for it",
                                file=sys.stderr,
                            )
                            continue
                        bar_providers = tuple(
                            sorted({bar.provider_name for bar in bars})
                        )
                        snapshot_providers = snapshot_for_reuse.provider_metadata.get(
                            "providers", ()
                        )
                        if not bar_providers or not set(bar_providers).issubset(
                            snapshot_providers
                        ):
                            raise SnapshotError(
                                "Existing snapshot is missing provider metadata: "
                                f"{stock.code}"
                            )
                        quality = validate_bars(
                            stock.code,
                            bars,
                            as_of=active_report_date,
                            settings=DataQualitySettings(
                                minimum_history_bars=(
                                    active_settings.market_data.minimum_history_bars
                                ),
                                max_completed_trading_day_lag=(
                                    active_settings.market_data.max_completed_trading_day_lag
                                ),
                            ),
                        )
                        if not quality.analysis_allowed:
                            raise ValueError(
                                "Existing snapshot failed quality validation: "
                                f"{stock.code}: {quality.issue_codes}"
                            )
                        result = FetchedBars(
                            code=stock.code,
                            provider_name=bar_providers[0],
                            bars=bars,
                            quality=quality,
                            from_cache=True,
                        )
                    else:
                        result = (
                            active_service.fetch(
                                stock.code,
                                end=active_report_date,
                                as_of=active_report_date,
                                defer_cache=True,
                            )
                            if active_service is not None
                            else _fetch_direct(
                                provider,
                                stock.code,
                                report_date=active_report_date,
                                settings=active_settings,
                            )
                        )
                    fetched[stock.code] = result
                except (
                    ProviderError,
                    ValidationError,
                    SnapshotError,
                    TypeError,
                    ValueError,
                ) as error:
                    quality = getattr(error, "quality", None)
                    issue_codes = (
                        quality.issue_codes
                        if isinstance(quality, DataQualityResult)
                        else ()
                    )
                    failures.append(
                        PipelineFailure(stock.code, str(error), tuple(issue_codes))
                    )
            if not fetched:
                if failures:
                    raise PipelineError(failures)
                raise PipelineError(
                    [
                        PipelineFailure(
                            "snapshot_selection_unavailable",
                            "The frozen snapshot for "
                            f"{active_report_date.isoformat()} contains none of the "
                            "selected watchlist codes; the deep-analysis section "
                            "refreshes on the next report date",
                        )
                    ]
                )
            light_quotes, light_quote_error = _fetch_light_quotes(
                active_watchlist, extended_stocks, universe_provider
            )
            if failures:
                raise PipelineError(failures)

        def publish_locked() -> ReportOutputs:
            nonlocal transaction_root
            bars_by_code = {code: result.bars for code, result in fetched.items()}
            report_dir = root / "reports" / active_report_date.isoformat()
            json_path = report_dir / "report.json"
            markdown_path = report_dir / "report.md"
            html_path = report_dir / "index.html"
            transaction_root = Path(tempfile.mkdtemp(prefix=".publication-", dir=root))
            transaction_initialized = False
            try:
                _fsync_directory(transaction_root)
                _fsync_directory(root)
                transaction_initialized = True
                try:
                    return _publish_report_transaction(
                        root=root,
                        transaction_root=transaction_root,
                        report_date=active_report_date,
                        generated_at=generated_at,
                        settings=active_settings,
                        watchlist=active_watchlist,
                        fetched=fetched,
                        bars_by_code=bars_by_code,
                        light_quotes=light_quotes,
                        light_quote_error=light_quote_error,
                        scan_ranking=watchlist_scan_ranking,
                        watchlist_scan_info=watchlist_scan_info,
                        market_state_settings=active_market_scan_settings,
                        overwrite_snapshot=overwrite_snapshot,
                        service=active_service,
                        json_path=json_path,
                        markdown_path=markdown_path,
                        html_path=html_path,
                    )
                finally:
                    _cleanup_transaction_root(transaction_root, active_report_date)
                    transaction_root = None
            finally:
                if not transaction_initialized:
                    try:
                        _fsync_directory(root)
                    except OSError:
                        pass
                    transaction_root = None

        if active_service is not None:
            with _publication_lock(root, active_report_date, cache=cache_lock):
                recover_locked()
                fetch_all()
                return publish_locked()

        fetch_all()
        with _publication_lock(root, active_report_date, cache=cache_lock):
            recover_locked()
            return publish_locked()
    finally:
        if active_service is not None:
            active_service.discard_staged_cache_writes()
        if transaction_root is not None:
            _cleanup_transaction_root(transaction_root, active_report_date)


def _cleanup_transaction_root(transaction_root: Path, report_date: date) -> None:
    """Remove staging only when no unverified publication backup remains."""

    if not transaction_root.exists():
        return
    backup_root = transaction_root / "backups"
    recovery_root = transaction_root / "recovery"
    cleanup_journal = transaction_root / "cleanup.json"
    manifest = transaction_root / "manifest.json"
    if not manifest.exists() and not cleanup_journal.exists():
        _recover_manifestless_publication(transaction_root)
        return
    if (
        backup_root.exists()
        or recovery_root.exists()
    ):
        return
    if manifest.exists():
        try:
            manifest_state = _read_publication_manifest(transaction_root)["state"]
        except PublicationRollbackError:
            return
        if manifest_state != "rolled_back":
            return
    elif cleanup_journal.exists():
        return
    shutil.rmtree(transaction_root, ignore_errors=True)
    try:
        transaction_root.rmdir()
    except FileNotFoundError:
        pass
    except OSError:
        return
    _fsync_directory(transaction_root.parent)


def _recover_pending_publications(
    root: Path, requested_date: date | None = None
) -> None:
    """Recover bounded orphan publication transactions before new work starts."""

    root = Path(root).expanduser().resolve()
    transaction_paths = sorted(root.glob(".publication-*"))
    if len(transaction_paths) > _MAX_PUBLICATION_TRANSACTIONS:
        raise PublicationRollbackError(
            "Too many orphan publication transactions; recovery artifacts "
            f"retained under {root}"
        )
    dated_transactions: list[tuple[date, Path]] = []
    manifestless_transactions: list[Path] = []
    for transaction_path in transaction_paths:
        if not transaction_path.is_dir() or transaction_path.is_symlink():
            raise PublicationRollbackError(
                "Invalid orphan publication transaction retained at "
                f"{transaction_path}"
            )
        if not (transaction_path / "manifest.json").exists() and not (
            transaction_path / "cleanup.json"
        ).exists():
            manifestless_transactions.append(transaction_path)
            continue
        try:
            manifest = _read_publication_manifest(transaction_path)
        except PublicationRollbackError:
            cleanup = _read_cleanup_journal(transaction_path)
            if cleanup is not None:
                try:
                    transaction_date = date.fromisoformat(cleanup["report_date"])
                except (KeyError, TypeError, ValueError) as error:
                    raise PublicationRollbackError(
                        "Invalid orphan cleanup report date retained at "
                        f"{transaction_path}"
                    ) from error
                dated_transactions.append((transaction_date, transaction_path))
                continue
            if not any(transaction_path.iterdir()):
                _recover_publication_transaction(transaction_path)
                continue
            if requested_date is None:
                raise
            if not (
                transaction_path / "reports" / requested_date.isoformat()
            ).exists():
                report_directory = transaction_path / "reports"
                if report_directory.exists() and any(
                    candidate.is_dir() for candidate in report_directory.iterdir()
                ):
                    continue
            raise
        try:
            transaction_date = date.fromisoformat(manifest["report_date"])
        except (KeyError, TypeError, ValueError) as error:
            raise PublicationRollbackError(
                "Invalid orphan publication report date retained at "
                f"{transaction_path}"
            ) from error
        dated_transactions.append((transaction_date, transaction_path))

    for transaction_path in manifestless_transactions:
        _recover_manifestless_publication(transaction_path)
    for transaction_date, transaction_path in dated_transactions:
        if requested_date is not None and transaction_date != requested_date:
            continue
        _recover_publication_transaction(transaction_path)


def _recover_pending_publications_if_idle(
    root: Path,
    report_date: date,
    *,
    service: MarketDataService | None = None,
    cache: RawResponseCache | None = None,
    cache_lock: RawResponseCache | None = None,
) -> None:
    root = Path(root).expanduser().resolve()
    transaction_paths = sorted(root.glob(".publication-*"))
    pending_cache = (
        (service is not None and service.has_pending_cache_manifests())
        or (cache is not None and cache.has_pending_manifests())
    )
    if not transaction_paths and not pending_cache:
        return
    transaction_dates: set[date] = set()
    for transaction_path in transaction_paths:
        if not transaction_path.exists():
            continue
        if transaction_path.is_symlink() or not transaction_path.is_dir():
            raise PublicationRollbackError(
                "Invalid orphan publication transaction retained at "
                f"{transaction_path}"
            )
        if not (transaction_path / "manifest.json").exists() and not (
            transaction_path / "cleanup.json"
        ).exists():
            continue
        try:
            manifest = _read_publication_manifest(transaction_path)
            report_date_value = manifest["report_date"]
        except PublicationRollbackError as manifest_error:
            cleanup = _read_cleanup_journal(transaction_path)
            if cleanup is None:
                raise PublicationRollbackError(
                    "Could not derive orphan publication report date; "
                    f"recovery artifacts retained at {transaction_path}"
                ) from manifest_error
            report_date_value = cleanup.get("report_date")
        try:
            transaction_dates.add(date.fromisoformat(report_date_value))
        except (TypeError, ValueError) as error:
            raise PublicationRollbackError(
                "Invalid orphan publication report date retained at "
                f"{transaction_path}"
            ) from error
    if pending_cache:
        transaction_dates.add(report_date)
    with _try_publication_recovery_lock(
        root, report_dates=transaction_dates, cache=cache_lock
    ) as acquired:
        if acquired:
            if service is not None:
                service.recover_pending_cache_manifests(publication_root=root)
            elif cache is not None:
                cache.recover_pending_manifests(publication_root=root)
            _recover_pending_publications(root)
        elif pending_cache and cache_lock is not None:
            recovery_path = getattr(cache_lock, "_directory", None)
            raise CacheRollbackError(
                recovery_path,
                "Could not acquire recovery lock before fetching; "
                "pending cache recovery is a precondition",
            )


@contextmanager
def _try_publication_lock(
    root: Path, report_date: date, *, cache: RawResponseCache | None = None
):
    root = Path(root).expanduser().resolve()
    snapshot_directory = root / "snapshots" / report_date.isoformat()
    snapshot_directory.mkdir(parents=True, exist_ok=True)
    _fsync_directory(snapshot_directory.parent)
    site_directory = root / "site"
    site_directory.mkdir(parents=True, exist_ok=True)
    date_lock = (snapshot_directory / ".input.lock").open("a", encoding="utf-8")
    site_lock = site_directory.joinpath(".publication.lock").open(
        "a", encoding="utf-8"
    )
    date_acquired = False
    site_acquired = False
    try:
        try:
            fcntl.flock(date_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            date_acquired = True
            fcntl.flock(site_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            site_acquired = True
        except BlockingIOError:
            yield False
            return
        if cache is None:
            yield True
            return
        with cache.transaction_lock(nonblocking=True) as cache_acquired:
            if not cache_acquired:
                yield False
                return
            yield True
    finally:
        if site_acquired:
            fcntl.flock(site_lock.fileno(), fcntl.LOCK_UN)
        if date_acquired:
            fcntl.flock(date_lock.fileno(), fcntl.LOCK_UN)
        site_lock.close()
        date_lock.close()


def _read_publication_manifest(transaction_root: Path) -> dict[str, object]:
    manifest_path = transaction_root / "manifest.json"
    try:
        if manifest_path.is_symlink() or (
            manifest_path.stat().st_size > _MAX_PUBLICATION_MANIFEST_BYTES
        ):
            raise OSError("publication manifest exceeds the size limit")
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationRollbackError(
            "Could not read orphan publication manifest; recovery artifacts "
            f"retained at {transaction_root}"
        ) from error
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or not isinstance(manifest.get("report_date"), str)
        or not isinstance(manifest.get("state"), str)
        or not isinstance(manifest.get("artifacts"), dict)
        or not isinstance(manifest.get("restore_candidates"), dict)
        or not isinstance(manifest.get("progress"), dict)
    ):
        raise PublicationRollbackError(
            "Invalid orphan publication manifest; recovery artifacts retained at "
            f"{transaction_root}"
        )
    artifacts = manifest["artifacts"]
    if any(
        name not in artifacts or not _valid_artifact_spec(artifacts[name])
        for name in ("report", "snapshot", "site-index", "styles")
    ):
        raise PublicationRollbackError(
            "Invalid orphan publication artifact metadata; recovery artifacts "
            f"retained at {transaction_root}"
        )
    for field in (
        "report_backed_up",
        "report_published",
        "report_backup_intent",
        "report_publish_intent",
        "snapshot_published",
        "snapshot_publish_intent",
        "site_index_backed_up",
        "site_index_published",
        "site_index_backup_intent",
        "site_index_publish_intent",
        "styles_backed_up",
        "styles_published",
        "styles_backup_intent",
        "styles_publish_intent",
    ):
        if field in manifest["progress"] and not isinstance(
            manifest["progress"][field], bool
        ):
            raise PublicationRollbackError(
                "Invalid orphan publication progress metadata; recovery "
                f"artifacts retained at {transaction_root}"
            )
    return manifest


def _read_cleanup_journal(transaction_root: Path) -> dict[str, object] | None:
    journal_path = transaction_root / "cleanup.json"
    try:
        if journal_path.is_symlink():
            raise OSError("cleanup journal must not be a symlink")
        if journal_path.stat().st_size > _MAX_PUBLICATION_MANIFEST_BYTES:
            raise OSError("cleanup journal exceeds the size limit")
        journal = json.loads(journal_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PublicationRollbackError(
            "Could not read orphan cleanup journal; recovery artifacts "
            f"retained at {transaction_root}"
        ) from error
    if (
        not isinstance(journal, dict)
        or journal.get("schema_version") != 1
        or journal.get("state") != "cleaning"
        or not isinstance(journal.get("report_date"), str)
        or not isinstance(journal.get("artifacts"), dict)
        or not isinstance(journal.get("published"), dict)
    ):
        raise PublicationRollbackError(
            "Invalid orphan cleanup journal; recovery artifacts retained at "
            f"{transaction_root}"
        )
    artifacts = journal["artifacts"]
    published = journal["published"]
    if any(
        name not in artifacts
        or not _valid_artifact_spec(artifacts[name])
        or name not in published
        or not _valid_artifact_spec(published[name])
        for name in ("report", "snapshot", "site-index", "styles")
    ):
        raise PublicationRollbackError(
            "Invalid orphan cleanup artifact metadata; recovery artifacts "
            f"retained at {transaction_root}"
        )
    return journal


def _ensure_cleanup_journal(
    transaction_root: Path, manifest: Mapping[str, object]
) -> None:
    if _read_cleanup_journal(transaction_root) is not None:
        return
    artifacts = manifest.get("artifacts")
    published = manifest.get("published")
    if not isinstance(artifacts, dict) or not isinstance(published, dict):
        raise PublicationRollbackError(
            "Committed publication lacks cleanup metadata; recovery artifacts "
            f"retained at {transaction_root}"
        )
    journal = {
        "schema_version": 1,
        "state": "cleaning",
        "report_date": manifest.get("report_date"),
        "artifacts": artifacts,
        "published": published,
    }
    _write_cleanup_journal(transaction_root, journal)


def _write_cleanup_journal(
    transaction_root: Path, journal: Mapping[str, object]
) -> None:
    _atomic_write(
        transaction_root / "cleanup.json",
        json.dumps(journal, ensure_ascii=False, sort_keys=True) + "\n",
    )
    _fsync_directory(transaction_root)
    _fsync_directory(transaction_root.parent)


def _publication_targets(root: Path, report_date: date) -> dict[str, Path]:
    return {
        "report": root / "reports" / report_date.isoformat(),
        "snapshot": root / "snapshots" / report_date.isoformat() / "input.json",
        "site-index": root / "site" / "index.html",
        "styles": root / "site" / "styles.css",
    }


def _recover_publication_cleanup(
    transaction_root: Path, journal: Mapping[str, object]
) -> None:
    try:
        report_date = date.fromisoformat(journal["report_date"])
    except (KeyError, TypeError, ValueError) as error:
        raise PublicationRollbackError(
            "Invalid orphan cleanup report date; recovery artifacts retained at "
            f"{transaction_root}"
        ) from error
    published = journal.get("published")
    if not isinstance(published, dict):
        raise PublicationRollbackError(
            "Invalid orphan cleanup publication metadata; recovery artifacts "
            f"retained at {transaction_root}"
        )
    targets = _publication_targets(transaction_root.parent, report_date)
    if any(
        not _valid_artifact_spec(published.get(name))
        or not _artifact_matches(targets[name], published[name])
        for name in targets
    ):
        raise PublicationRollbackError(
            "Committed publication targets are not verified; recovery "
            f"artifacts retained at {transaction_root}"
        )
    _finish_publication_cleanup(transaction_root)


def _remove_recovery_payloads(recovery_root: Path) -> None:
    if not recovery_root.exists():
        return
    if recovery_root.is_symlink() or not recovery_root.is_dir():
        raise PublicationRollbackError(
            "Invalid publication recovery directory retained at "
            f"{recovery_root}"
        )
    recovery_manifest = recovery_root / "manifest.json"
    for child in sorted(recovery_root.iterdir()):
        if child == recovery_manifest:
            continue
        if child.is_symlink():
            raise PublicationRollbackError(
                "Invalid publication recovery payload retained at " f"{child}"
            )
        if child.is_dir():
            shutil.rmtree(child)
        elif child.is_file():
            child.unlink()
        else:
            raise PublicationRollbackError(
                "Invalid publication recovery payload retained at " f"{child}"
            )
        _fsync_directory(recovery_root)
    remaining = set(recovery_root.iterdir())
    if remaining - {recovery_manifest}:
        raise PublicationRollbackError(
            "Publication recovery payload cleanup was incomplete; recovery "
            f"artifacts retained at {recovery_root}"
        )
    if recovery_manifest.exists():
        shutil.rmtree(recovery_root)
    else:
        recovery_root.rmdir()
    _fsync_directory(recovery_root.parent)


def _finish_publication_cleanup(transaction_root: Path) -> None:
    for name in ("recovery", "backups", "reports", "snapshots", "site"):
        payload_root = transaction_root / name
        if not payload_root.exists():
            continue
        if name == "recovery":
            _remove_recovery_payloads(payload_root)
        elif payload_root.is_symlink() or not payload_root.is_dir():
            raise PublicationRollbackError(
                "Invalid publication cleanup payload retained at "
                f"{payload_root}"
            )
        else:
            shutil.rmtree(payload_root)
            _fsync_directory(transaction_root)

    manifest_path = transaction_root / "manifest.json"
    manifest_path.unlink(missing_ok=True)
    _fsync_directory(transaction_root)
    cleanup_journal = transaction_root / "cleanup.json"
    cleanup_journal.unlink(missing_ok=True)
    _fsync_directory(transaction_root)
    try:
        transaction_root.rmdir()
    except FileNotFoundError:
        return
    except OSError as error:
        raise PublicationRollbackError(
            "Publication cleanup left unexpected transaction data; recovery "
            f"artifacts retained at {transaction_root}"
        ) from error
    _fsync_directory(transaction_root.parent)


def _valid_artifact_spec(spec: object) -> bool:
    if not isinstance(spec, dict):
        return False
    if not isinstance(spec.get("present"), bool):
        return False
    if spec.get("kind") not in {"file", "directory", None}:
        return False
    files = spec.get("files")
    if not isinstance(files, list):
        return False
    return all(
        isinstance(entry, dict)
        and isinstance(entry.get("path"), str)
        and isinstance(entry.get("size"), int)
        and isinstance(entry.get("sha256"), str)
        for entry in files
    )


def _manifestless_payload_is_safe(transaction_root: Path) -> bool:
    try:
        transaction_root_resolved = transaction_root.resolve(strict=False)
        entries = list(transaction_root.iterdir())
    except (OSError, RuntimeError):
        return False
    if not entries:
        return True
    if any(entry.name not in _MANIFESTLESS_PAYLOAD_ROOTS for entry in entries):
        return False
    for payload_root in entries:
        if payload_root.is_symlink() or not payload_root.is_dir():
            return False
        try:
            payload_root.resolve(strict=False).relative_to(transaction_root_resolved)
            descendants = payload_root.rglob("*")
            for descendant in descendants:
                if descendant.name in _PUBLICATION_EVIDENCE_NAMES:
                    return False
                if descendant.is_symlink() or not (
                    descendant.is_dir() or descendant.is_file()
                ):
                    return False
                descendant.resolve(strict=False).relative_to(transaction_root_resolved)
        except (OSError, RuntimeError, ValueError):
            return False
    return True


def _quarantine_manifestless_publication(
    transaction_root: Path, *, reason: str
) -> None:
    quarantine_path = transaction_root / _PUBLICATION_QUARANTINE_NAME
    if quarantine_path.exists() or quarantine_path.is_symlink():
        raise PublicationRollbackError(
            "Manifest-less publication transaction is quarantined; recovery "
            f"artifacts retained at {transaction_root}"
        )
    try:
        _atomic_write(
            quarantine_path,
            json.dumps(
                {
                    "schema_version": 1,
                    "state": "quarantined",
                    "reason": reason,
                    "entries": sorted(
                        path.name for path in transaction_root.iterdir()
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n",
        )
        _fsync_directory(transaction_root)
        _fsync_directory(transaction_root.parent)
    except OSError as error:
        raise PublicationRollbackError(
            "Could not record manifest-less publication quarantine; recovery "
            f"artifacts retained at {transaction_root}"
        ) from error
    raise PublicationRollbackError(
        "Manifest-less publication transaction is ambiguous; recovery "
        f"artifacts quarantined at {transaction_root}"
    )


def _recover_manifestless_publication(transaction_root: Path) -> None:
    if not transaction_root.exists():
        return
    quarantine_path = transaction_root / _PUBLICATION_QUARANTINE_NAME
    if quarantine_path.exists() or quarantine_path.is_symlink():
        raise PublicationRollbackError(
            "Manifest-less publication transaction is quarantined; recovery "
            f"artifacts retained at {transaction_root}"
        )
    if not _manifestless_payload_is_safe(transaction_root):
        _quarantine_manifestless_publication(
            transaction_root,
            reason=(
                "Unexpected publication, backup, recovery, cache, or out-of-scope "
                "transaction data"
            ),
        )
    shutil.rmtree(transaction_root)
    _fsync_directory(transaction_root.parent)


def _recover_publication_transaction(transaction_root: Path) -> None:
    manifest_path = transaction_root / "manifest.json"
    if not manifest_path.exists():
        cleanup = _read_cleanup_journal(transaction_root)
        if cleanup is None:
            _recover_manifestless_publication(transaction_root)
            return
        _recover_publication_cleanup(transaction_root, cleanup)
        return
    manifest = _read_publication_manifest(transaction_root)
    allowed_transaction_entries = {
        "manifest.json",
        "cleanup.json",
        "backups",
        "recovery",
        "reports",
        "snapshots",
        "site",
    }
    if {
        path.name for path in transaction_root.iterdir()
    } - allowed_transaction_entries:
        raise PublicationRollbackError(
            "Orphan publication contains unexpected transaction data; recovery "
            f"artifacts retained at {transaction_root}"
        )
    state = manifest["state"]
    if state == "committed":
        published = manifest.get("published")
        if not isinstance(published, dict):
            raise PublicationRollbackError(
                "Committed publication lacks verification metadata; recovery "
                f"artifacts retained at {transaction_root}"
            )
        targets = {
            "report": transaction_root.parents[0]
            / "reports"
            / manifest["report_date"],
            "snapshot": transaction_root.parents[0]
            / "snapshots"
            / manifest["report_date"]
            / "input.json",
            "site-index": transaction_root.parents[0] / "site" / "index.html",
            "styles": transaction_root.parents[0] / "site" / "styles.css",
        }
        if {path.name for path in transaction_root.iterdir()} - {
            "manifest.json",
            "cleanup.json",
            "recovery",
            "reports",
            "snapshots",
            "site",
        }:
            raise PublicationRollbackError(
                "Committed publication contains unverified transaction data; "
                f"recovery artifacts retained at {transaction_root}"
            )
        if any(
            not _valid_artifact_spec(published.get(name))
            or not _artifact_matches(targets[name], published[name])
            for name in targets
        ):
            raise PublicationRollbackError(
                "Committed publication targets are not verified; recovery "
                f"artifacts retained at {transaction_root}"
            )
        _ensure_cleanup_journal(transaction_root, manifest)
        _recover_publication_cleanup(
            transaction_root, _read_cleanup_journal(transaction_root)
        )
        return
    if state == "cleaning":
        _ensure_cleanup_journal(transaction_root, manifest)
        _recover_publication_cleanup(
            transaction_root, _read_cleanup_journal(transaction_root)
        )
        return
    if state not in {
        "prepared",
        "publishing",
        "finalizing",
        "verified",
        "committing",
        "rolling_back",
        "rolled_back",
    }:
        raise PublicationRollbackError(
            f"Unsupported orphan publication state {state!r}; recovery "
            f"artifacts retained at {transaction_root}"
        )

    report_date = date.fromisoformat(manifest["report_date"])
    artifacts = manifest["artifacts"]
    restore_candidates = manifest["restore_candidates"]
    progress = manifest["progress"]
    restore_plan = (
        (
            "styles",
            transaction_root.parents[0] / "site" / "styles.css",
            transaction_root / "backups" / "styles.css",
            transaction_root / "recovery" / "styles.css",
            bool(progress.get("styles_published"))
            or bool(progress.get("styles_publish_intent")),
            bool(progress.get("styles_backed_up"))
            or bool(progress.get("styles_backup_intent")),
        ),
        (
            "site-index",
            transaction_root.parents[0] / "site" / "index.html",
            transaction_root / "backups" / "site-index.html",
            transaction_root / "recovery" / "site-index.html",
            bool(progress.get("site_index_published"))
            or bool(progress.get("site_index_publish_intent")),
            bool(progress.get("site_index_backed_up"))
            or bool(progress.get("site_index_backup_intent")),
        ),
        (
            "snapshot",
            transaction_root.parents[0]
            / "snapshots"
            / report_date.isoformat()
            / "input.json",
            transaction_root / "backups" / "snapshot.json",
            transaction_root / "recovery" / "snapshot.json",
            bool(progress.get("snapshot_published"))
            or bool(progress.get("snapshot_publish_intent")),
            False,
        ),
        (
            "report",
            transaction_root.parents[0]
            / "reports"
            / report_date.isoformat(),
            transaction_root / "backups" / "report",
            transaction_root / "recovery" / "report",
            bool(progress.get("report_published"))
            or bool(progress.get("report_publish_intent")),
            bool(progress.get("report_backed_up"))
            or bool(progress.get("report_backup_intent")),
        ),
    )
    manifest["state"] = "rolling_back"
    _write_publication_manifest(transaction_root, manifest)
    recovery_manifest = _load_publication_recovery_manifest(
        transaction_root / "recovery" / "manifest.json"
    )
    if (transaction_root / "recovery").exists() and recovery_manifest is None:
        raise PublicationRollbackError(
            "Invalid orphan publication recovery manifest; recovery artifacts "
            f"retained at {transaction_root}"
        )
    for name, target, backup, recovery, published, backed_up in restore_plan:
        expected = artifacts.get(name)
        if not isinstance(expected, dict):
            raise PublicationRollbackError(
                f"Invalid orphan publication artifact metadata for {name}; "
                f"recovery artifacts retained at {transaction_root}"
            )
        if _artifact_matches(target, expected):
            continue
        should_restore = bool(restore_candidates.get(name)) and (
            backed_up or backup.exists() or published
        )
        source = None
        if should_restore:
            if recovery_manifest is not None:
                entry = recovery_manifest.get(name)
                if (
                    entry is not None
                    and entry.get("expected") == expected
                    and _artifact_matches(recovery, expected)
                ):
                    source = recovery
            if source is None and _artifact_matches(backup, expected):
                source = backup
            if source is None:
                raise PublicationRollbackError(
                    f"No complete orphan publication recovery source for {name}; "
                    f"recovery artifacts retained at {transaction_root}"
                )
            _replace_publication_target(source, target)
        elif not expected.get("present") and (published or backed_up):
            _remove_publication_target(target)
        if not _artifact_matches(target, expected):
            raise PublicationRollbackError(
                f"Orphan publication target was not restored for {name}; "
                f"recovery artifacts retained at {transaction_root}"
            )
    shutil.rmtree(transaction_root)
    _fsync_directory(transaction_root.parent)


def _load_publication_recovery_manifest(
    manifest_path: Path,
) -> dict[str, dict[str, object]] | None:
    try:
        if manifest_path.is_symlink() or (
            manifest_path.stat().st_size > _MAX_PUBLICATION_MANIFEST_BYTES
        ):
            return None
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 2
        or manifest.get("state") not in {"ready", "copying"}
        or not isinstance(manifest.get("entries"), list)
    ):
        return None
    entries: dict[str, dict[str, object]] = {}
    for entry in manifest["entries"]:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("name"), str)
            or not isinstance(entry.get("expected"), dict)
            or entry["name"] in entries
        ):
            return None
        entries[entry["name"]] = entry
    return entries


def _replace_publication_target(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink(missing_ok=True)
    _fsync_directory(target.parent)
    _replace_and_fsync(source, target)


def _remove_publication_target(target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)
    else:
        target.unlink(missing_ok=True)
    _fsync_directory(target.parent)


def _write_publication_manifest(
    transaction_root: Path, manifest: Mapping[str, object]
) -> None:
    _atomic_write(
        transaction_root / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
    )
    _fsync_directory(transaction_root)
    _fsync_directory(transaction_root.parent)


@contextmanager
def _try_publication_recovery_lock(
    root: Path,
    *,
    report_dates: Sequence[date] = (),
    cache: RawResponseCache | None = None,
):
    root = Path(root).expanduser().resolve()
    date_locks = []
    date_acquired = []
    site_lock = None
    site_acquired = False
    try:
        for report_date in sorted(set(report_dates)):
            snapshot_directory = root / "snapshots" / report_date.isoformat()
            snapshot_directory.mkdir(parents=True, exist_ok=True)
            _fsync_directory(snapshot_directory.parent)
            date_lock = (snapshot_directory / ".input.lock").open(
                "a", encoding="utf-8"
            )
            date_locks.append(date_lock)
            try:
                fcntl.flock(
                    date_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB
                )
                date_acquired.append(date_lock)
            except BlockingIOError:
                yield False
                return
        site_directory = root / "site"
        site_directory.mkdir(parents=True, exist_ok=True)
        site_lock = site_directory.joinpath(".publication.lock").open(
            "a", encoding="utf-8"
        )
        try:
            fcntl.flock(site_lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            site_acquired = True
        except BlockingIOError:
            yield False
            return
        if cache is None:
            yield True
            return
        with cache.transaction_lock(nonblocking=True) as cache_acquired:
            if not cache_acquired:
                yield False
                return
            yield True
    finally:
        if site_acquired:
            fcntl.flock(site_lock.fileno(), fcntl.LOCK_UN)
        if site_lock is not None:
            site_lock.close()
        for date_lock in reversed(date_acquired):
            fcntl.flock(date_lock.fileno(), fcntl.LOCK_UN)
        for date_lock in reversed(date_locks):
            date_lock.close()


@contextmanager
def _publication_lock(
    root: Path, report_date: date, *, cache: RawResponseCache | None = None
):
    """Lock date, then shared site files; cache locks are acquired last.

    Every pipeline-owned cache recovery or staged cache commit runs inside this
    context, establishing the fixed date -> site -> cache lock order.
    """

    root = Path(root).expanduser().resolve()
    snapshot_directory = root / "snapshots" / report_date.isoformat()
    snapshot_directory.mkdir(parents=True, exist_ok=True)
    _fsync_directory(snapshot_directory.parent)
    site_directory = root / "site"
    site_directory.mkdir(parents=True, exist_ok=True)
    with _snapshot_write_lock(snapshot_directory), _snapshot_write_lock(
        site_directory, lock_name=".publication.lock"
    ):
        if cache is None:
            yield
            return
        with cache.transaction_lock():
            yield


def _cache_lock_owner(
    *,
    service: MarketDataService | None,
    cache: RawResponseCache | None,
) -> RawResponseCache | None:
    if service is not None:
        return getattr(service, "_cache", None)
    return cache


def _publish_report_transaction(
    *,
    root: Path,
    transaction_root: Path,
    report_date: date,
    generated_at: datetime,
    settings: Settings,
    watchlist: Watchlist,
    fetched: Mapping[str, FetchedBars],
    bars_by_code: Mapping[str, Sequence[DailyBar]],
    light_quotes: Mapping[str, UniverseQuote],
    light_quote_error: str | None,
    scan_ranking: Sequence[tuple[str, float]] | None,
    watchlist_scan_info: WatchlistScanInfo | None,
    market_state_settings: MarketStateSettings,
    overwrite_snapshot: bool,
    service: MarketDataService | None,
    json_path: Path,
    markdown_path: Path,
    html_path: Path,
) -> ReportOutputs:
    publication: _PublicationTransaction | None = None
    try:
        staged_snapshot_path = write_snapshot(
            transaction_root,
            report_date=report_date,
            bars_by_code=bars_by_code,
            generated_at=generated_at,
        )
        staged_snapshot = load_snapshot(staged_snapshot_path)
        snapshot_target = root / "snapshots" / report_date.isoformat() / "input.json"
        if overwrite_snapshot:
            snapshot = _resolve_snapshot_for_publication(
                snapshot_target,
                staged_snapshot,
                overwrite=True,
            )
        else:
            snapshot = _resolve_snapshot_for_publication(
                snapshot_target,
                staged_snapshot,
            )
        market_rankings = _load_market_rankings(
            root,
            report_date,
            market_state_settings,
        )
        market_state = _load_market_state(
            root,
            report_date,
            market_state_settings,
        )
        market_state_identity = _load_market_state_identity(
            root,
            report_date,
            market_state_settings,
        )
        report = _build_report(
            settings,
            watchlist,
            fetched,
            snapshot_path=snapshot_target,
            snapshot_hash=snapshot.content_hash,
            report_date=report_date,
            generated_at=generated_at,
            market_state=market_state,
            market_state_identity=market_state_identity,
            market_rankings=market_rankings,
            light_quotes=light_quotes,
            light_quote_error=light_quote_error,
            scan_ranking=scan_ranking,
            watchlist_scan_info=watchlist_scan_info,
        )

        staged_report_dir = transaction_root / "reports" / report_date.isoformat()
        staged_report_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            staged_report_dir / "report.json",
            report.model_dump_json(indent=2) + "\n",
        )
        _atomic_write(staged_report_dir / "report.md", render_markdown(report))
        _atomic_write(staged_report_dir / "index.html", render_html(report))
        _validate_staged_report(staged_report_dir)

        staged_site_dir = transaction_root / "site"
        staged_site_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            staged_site_dir / "index.html",
            _render_site_index_for_publication(root, report_date),
        )
        staged_styles_path: Path | None = None
        styles_source = _project_root() / "site" / "styles.css"
        if styles_source.exists():
            staged_styles_path = staged_site_dir / "styles.css"
            _atomic_write(staged_styles_path, styles_source.read_text(encoding="utf-8"))

        publication = _PublicationTransaction(
            root=root,
            report_date=report_date,
            staged_report_dir=staged_report_dir,
            staged_snapshot_path=(
                None
                if (snapshot_target.exists() and not overwrite_snapshot)
                else staged_snapshot_path
            ),
            staged_site_index=staged_site_dir / "index.html",
            staged_styles_path=staged_styles_path,
        )
        publication.publish()
        publication.finalize()
        publication.complete()
        try:
            if service is not None:
                set_context = getattr(
                    service, "set_publication_recovery_context", None
                )
                if set_context is not None:
                    set_context(
                        publication.manifest_path,
                        publication_root=root,
                    )
                service.commit_staged_cache_writes()
        except Exception as error:
            raise PipelineError(
                [PipelineFailure("cache_commit", str(error))]
            ) from error
        publication.commit()
        if service is not None:
            finalize_cache = getattr(
                service, "finalize_staged_cache_commit", None
            )
            if finalize_cache is not None:
                finalize_cache()
        publication.cleanup()
    except BaseException as error:
        if service is not None:
            service.discard_staged_cache_writes()
        cache_rollback_error: Exception | None = None
        if (
            service is not None
            and publication is not None
            and not publication._finished
        ):
            try:
                rollback_cache = getattr(
                    service, "rollback_staged_cache_commit", None
                )
                if rollback_cache is not None:
                    rollback_cache()
            except (CacheRollbackError, OSError, TypeError, ValueError) as rollback_error:
                cache_rollback_error = rollback_error
        if (
            publication is not None
            and not publication._finished
            and not publication._rollback_attempted
        ):
            publication.rollback()
        if cache_rollback_error is not None and isinstance(error, Exception):
            raise CacheRollbackError(
                getattr(service, "_cache_recovery_path", None),
                str(cache_rollback_error),
            ) from error
        if isinstance(error, SnapshotConflictError):
            raise PipelineError(
                [PipelineFailure("snapshot_conflict", str(error))]
            ) from error
        if isinstance(error, SnapshotError):
            raise PipelineError(
                [
                    PipelineFailure(
                        "snapshot_error", f"Could not prepare snapshot: {error}"
                    )
                ]
            ) from error
        raise

    return ReportOutputs(
        json_path=json_path,
        markdown_path=markdown_path,
        html_path=html_path,
        snapshot_path=snapshot_target,
        report=report,
    )


def _resolve_snapshot_for_publication(
    snapshot_target: Path,
    staged_snapshot: InputSnapshot,
    *,
    overwrite: bool = False,
) -> InputSnapshot:
    if not snapshot_target.exists() or overwrite:
        return staged_snapshot
    existing_snapshot = load_snapshot(snapshot_target)
    if (
        existing_snapshot.report_date == staged_snapshot.report_date
        and existing_snapshot.bars_by_code == staged_snapshot.bars_by_code
    ):
        return existing_snapshot
    raise PipelineError(
        [
            PipelineFailure(
                "snapshot_conflict",
                "Immutable snapshot already exists with different content: "
                f"{snapshot_target}",
            )
        ]
    )


def _validate_staged_report(staged_report_dir: Path) -> None:
    report_json = staged_report_dir / "report.json"
    ReportDocument.model_validate_json(report_json.read_text(encoding="utf-8"))
    for name in ("report.md", "index.html"):
        if not (staged_report_dir / name).read_text(encoding="utf-8").strip():
            raise ValueError(f"Staged report artifact is empty: {name}")


def _render_site_index_for_publication(root: Path, report_date: date) -> str:
    report_dates = {
        path.name
        for path in (root / "reports").iterdir()
        if path.is_dir() and (path / "index.html").exists()
    } if (root / "reports").exists() else set()
    report_dates.add(report_date.isoformat())
    return render_site_index(sorted(report_dates))


def _describe_artifact(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"present": False, "kind": None, "files": []}
    if path.is_dir():
        files = []
        for child in sorted(
            (candidate for candidate in path.rglob("*") if candidate.is_file()),
            key=lambda candidate: candidate.relative_to(path).as_posix(),
        ):
            relative_path = child.relative_to(path).as_posix()
            content = child.read_bytes()
            files.append(
                {
                    "path": relative_path,
                    "size": len(content),
                    "sha256": hashlib.sha256(content).hexdigest(),
                }
            )
        return {"present": True, "kind": "directory", "files": files}
    content = path.read_bytes()
    return {
        "present": True,
        "kind": "file",
        "files": [
            {
                "path": ".",
                "size": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        ],
    }


def _artifact_matches(path: Path, expected: Mapping[str, object]) -> bool:
    if not expected["present"]:
        return not path.exists() and not path.is_symlink()
    actual = _describe_artifact(path)
    return actual == {
        "present": expected["present"],
        "kind": expected["kind"],
        "files": expected["files"],
    }


class _PublicationTransaction:
    """Publish staged outputs with reversible renames and rollback.

    A directory rename is atomic on the same filesystem, but three separate
    paths cannot be atomically replaced as one unit. Existing paths are first
    moved to a transaction-local backup; any later failure restores those
    backups before the transaction is discarded.
    """

    def __init__(
        self,
        *,
        root: Path,
        report_date: date,
        staged_report_dir: Path,
        staged_snapshot_path: Path | None,
        staged_site_index: Path,
        staged_styles_path: Path | None,
    ) -> None:
        self.root = root
        self.report_date = report_date
        self.staged_report_dir = staged_report_dir
        self.staged_snapshot_path = staged_snapshot_path
        self.staged_site_index = staged_site_index
        self.staged_styles_path = staged_styles_path
        self.transaction_root = staged_report_dir.parents[1]
        self.manifest_path = self.transaction_root / "manifest.json"
        self.report_dir = root / "reports" / report_date.isoformat()
        self.snapshot_path = (
            root / "snapshots" / report_date.isoformat() / "input.json"
        )
        self.site_index = root / "site" / "index.html"
        self.styles_path = root / "site" / "styles.css"
        self._report_was_present = self.report_dir.exists()
        self._snapshot_was_present = self.snapshot_path.exists()
        self._site_index_was_present = self.site_index.exists()
        self._styles_was_present = self.styles_path.exists()
        self.backup_root = staged_report_dir.parents[1] / "backups"
        self.recovery_root = staged_report_dir.parents[1] / "recovery"
        self.backup_report_dir = self.backup_root / "report"
        self.backup_snapshot_path = self.backup_root / "snapshot.json"
        self.backup_site_index = self.backup_root / "site-index.html"
        self.backup_styles = self.backup_root / "styles.css"
        self.recovery_report_dir = self.recovery_root / "report"
        self.recovery_snapshot_path = self.recovery_root / "snapshot.json"
        self.recovery_site_index = self.recovery_root / "site-index.html"
        self.recovery_styles = self.recovery_root / "styles.css"
        self._report_backed_up = False
        self._report_published = False
        self._report_backup_intent = False
        self._report_publish_intent = False
        self._snapshot_published = False
        self._snapshot_publish_intent = False
        self._site_index_backed_up = False
        self._site_index_published = False
        self._site_index_backup_intent = False
        self._site_index_publish_intent = False
        self._styles_backed_up = False
        self._styles_published = False
        self._styles_backup_intent = False
        self._styles_publish_intent = False
        self._finished = False
        self._rollback_attempted = False
        self._artifact_specs = {
            "report": _describe_artifact(self.report_dir),
            "snapshot": _describe_artifact(self.snapshot_path),
            "site-index": _describe_artifact(self.site_index),
            "styles": _describe_artifact(self.styles_path),
        }
        self._restore_candidates = {
            "report": self._report_was_present,
            "snapshot": False,
            "site-index": self._site_index_was_present,
            "styles": (
                self._styles_was_present
                and self.staged_styles_path is not None
            ),
        }
        self._published_artifacts = {
            "report": _describe_artifact(self.staged_report_dir),
            "snapshot": (
                _describe_artifact(self.staged_snapshot_path)
                if self.staged_snapshot_path is not None
                else _describe_artifact(self.snapshot_path)
            ),
            "site-index": _describe_artifact(self.staged_site_index),
            "styles": (
                _describe_artifact(self.staged_styles_path)
                if self.staged_styles_path is not None
                else _describe_artifact(self.styles_path)
            ),
        }
        self._write_manifest("prepared")

    def publish(self) -> None:
        self._write_manifest("publishing")
        try:
            self.backup_root.mkdir(parents=True, exist_ok=True)
            if self.report_dir.exists():
                self._report_backup_intent = True
                self._write_manifest("publishing")
                _replace_and_fsync(self.report_dir, self.backup_report_dir)
                self._report_backed_up = True
                self._write_manifest("publishing")
            self.report_dir.parent.mkdir(parents=True, exist_ok=True)
            self._report_publish_intent = True
            self._write_manifest("publishing")
            _replace_and_fsync(self.staged_report_dir, self.report_dir)
            self._report_published = True
            self._write_manifest("publishing")

            if self.staged_snapshot_path is not None:
                self.snapshot_path.parent.mkdir(parents=True, exist_ok=True)
                self._snapshot_publish_intent = True
                self._write_manifest("publishing")
                _replace_and_fsync(self.staged_snapshot_path, self.snapshot_path)
                _fsync_directory(self.snapshot_path.parent.parent)
                self._snapshot_published = True
                self._write_manifest("publishing")

            if self.site_index.exists():
                self._site_index_backup_intent = True
                self._write_manifest("publishing")
                _replace_and_fsync(self.site_index, self.backup_site_index)
                self._site_index_backed_up = True
                self._write_manifest("publishing")
            self.site_index.parent.mkdir(parents=True, exist_ok=True)
            self._site_index_publish_intent = True
            self._write_manifest("publishing")
            _replace_and_fsync(self.staged_site_index, self.site_index)
            self._site_index_published = True
            self._write_manifest("publishing")

            if self.staged_styles_path is not None:
                if self.styles_path.exists():
                    self._styles_backup_intent = True
                    self._write_manifest("publishing")
                    _replace_and_fsync(self.styles_path, self.backup_styles)
                    self._styles_backed_up = True
                    self._write_manifest("publishing")
                self.styles_path.parent.mkdir(parents=True, exist_ok=True)
                self._styles_publish_intent = True
                self._write_manifest("publishing")
                _replace_and_fsync(self.staged_styles_path, self.styles_path)
                self._styles_published = True
                self._write_manifest("publishing")
        except BaseException:
            self.rollback()
            raise

    def finalize(self) -> None:
        """Mark publication ready while retaining backups for cache commit."""

    def complete(self) -> None:
        """Verify publication and remove backups while retaining recovery copies."""

        published_targets = {
            "report": self.report_dir,
            "snapshot": self.snapshot_path,
            "site-index": self.site_index,
            "styles": self.styles_path,
        }
        for name, target in published_targets.items():
            if not _artifact_matches(target, self._published_artifacts[name]):
                raise OSError(f"Published artifact verification failed: {target}")
        self._prepare_recovery_copy()
        self._write_manifest("verified")
        if self.backup_root.exists():
            shutil.rmtree(self.backup_root)
            _fsync_directory(self.backup_root.parent)
            if self.backup_root.exists():
                raise OSError(
                    f"Could not remove publication backups: {self.backup_root}"
                )

    def commit(self) -> None:
        """Record the commit while retaining the journal for cache cleanup."""

        if self._finished:
            return
        self._write_manifest("committing")
        self._write_manifest("committed")
        self._finished = True

    def cleanup(self) -> None:
        """Release the durable publication journal after cache acknowledgement."""

        if not self._finished:
            raise OSError("Cannot clean up an uncommitted publication")
        self._write_manifest("cleaning")
        cleanup_journal = {
            "schema_version": 1,
            "state": "cleaning",
            "report_date": self.report_date.isoformat(),
            "artifacts": self._artifact_specs,
            "published": self._published_artifacts,
        }
        _write_cleanup_journal(self.transaction_root, cleanup_journal)
        _recover_publication_cleanup(self.transaction_root, cleanup_journal)

    def rollback(self) -> None:
        if self._finished:
            return
        if self._rollback_attempted:
            raise PublicationRollbackError(
                "Publication rollback previously failed; recovery artifacts "
                f"retained at {self._recovery_error_path()}"
            )
        self._rollback_attempted = True
        try:
            self._write_manifest("rolling_back")
            restore_plan = (
                (
                    "styles",
                    self.styles_path,
                    self.backup_styles,
                    self.recovery_styles,
                    self._styles_published,
                    self._styles_backed_up,
                    self.staged_styles_path,
                ),
                (
                    "site-index",
                    self.site_index,
                    self.backup_site_index,
                    self.recovery_site_index,
                    self._site_index_published,
                    self._site_index_backed_up,
                    self.staged_site_index,
                ),
                (
                    "snapshot",
                    self.snapshot_path,
                    self.backup_snapshot_path,
                    self.recovery_snapshot_path,
                    self._snapshot_published,
                    False,
                    self.staged_snapshot_path,
                ),
                (
                    "report",
                    self.report_dir,
                    self.backup_report_dir,
                    self.recovery_report_dir,
                    self._report_published,
                    self._report_backed_up,
                    self.staged_report_dir,
                ),
            )
            selected_sources = {
                name: self._select_complete_source(
                    name=name,
                    backup=backup,
                    recovery=recovery,
                    published=published,
                    backed_up=backed_up,
                )
                for name, _, backup, recovery, published, backed_up, _ in restore_plan
            }
            for (
                name,
                target,
                _backup,
                _recovery,
                published,
                _backed_up,
                staged,
            ) in restore_plan:
                source = selected_sources[name]
                if source is not None:
                    self._replace_target(source, target)
                elif not self._artifact_specs[name]["present"] and (
                    published or (staged is not None and not staged.exists())
                ):
                    self._remove_target(target)
            for name, target, *_ in restore_plan:
                if not _artifact_matches(target, self._artifact_specs[name]):
                    raise OSError(
                        f"Publication target was not restored completely: {target}"
                    )
            if self.backup_root.exists():
                shutil.rmtree(self.backup_root)
                _fsync_directory(self.backup_root.parent)
            if self.backup_root.exists():
                raise OSError(f"Could not remove publication backups: {self.backup_root}")
            if self.recovery_root.exists():
                shutil.rmtree(self.recovery_root)
                _fsync_directory(self.recovery_root.parent)
                if self.recovery_root.exists():
                    raise OSError(
                        "Could not remove publication recovery copies: "
                        f"{self.recovery_root}"
                    )
            self._write_manifest("rolled_back")
        except BaseException as error:
            if isinstance(error, PublicationRollbackError):
                raise
            raise PublicationRollbackError(
                "Publication rollback failed; recovery artifacts retained at "
                f"{self._recovery_error_path()}"
            ) from error
        self._finished = True

    def _write_manifest(self, state: str) -> None:
        manifest = {
            "schema_version": 1,
            "report_date": self.report_date.isoformat(),
            "state": state,
            "artifacts": self._artifact_specs,
            "restore_candidates": self._restore_candidates,
            "progress": {
                "report_backed_up": self._report_backed_up,
                "report_published": self._report_published,
                "report_backup_intent": self._report_backup_intent,
                "report_publish_intent": self._report_publish_intent,
                "snapshot_published": self._snapshot_published,
                "snapshot_publish_intent": self._snapshot_publish_intent,
                "site_index_backed_up": self._site_index_backed_up,
                "site_index_published": self._site_index_published,
                "site_index_backup_intent": self._site_index_backup_intent,
                "site_index_publish_intent": self._site_index_publish_intent,
                "styles_backed_up": self._styles_backed_up,
                "styles_published": self._styles_published,
                "styles_backup_intent": self._styles_backup_intent,
                "styles_publish_intent": self._styles_publish_intent,
            },
            "published": self._published_artifacts,
        }
        _write_publication_manifest(self.transaction_root, manifest)

    def _select_complete_source(
        self,
        *,
        name: str,
        backup: Path,
        recovery: Path,
        published: bool,
        backed_up: bool,
    ) -> Path | None:
        expected = self._artifact_specs[name]
        needs_restore = bool(expected["present"]) and (
            backed_up or backup.exists() or published
        )
        if not needs_restore:
            return None
        recovery_manifest = self._load_recovery_manifest()
        if recovery_manifest is not None:
            entry = recovery_manifest.get(name)
            if (
                entry is not None
                and entry["expected"] == expected
                and _artifact_matches(recovery, expected)
            ):
                return recovery
        if _artifact_matches(backup, expected):
            return backup
        raise OSError(
            f"No complete publication recovery source for {name}; "
            f"expected artifacts are retained at {self._recovery_error_path()}"
        )

    def _replace_target(self, source: Path, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        self._remove_target(target)
        _replace_and_fsync(source, target)

    @staticmethod
    def _remove_target(target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_dir() and not target.is_symlink():
            shutil.rmtree(target)
        else:
            target.unlink(missing_ok=True)
        _fsync_directory(target.parent)

    def _load_recovery_manifest(self) -> dict[str, dict[str, object]] | None:
        manifest_path = self.recovery_root / "manifest.json"
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                return None
            if (
                manifest.get("schema_version") != 2
                or manifest.get("state") != "ready"
                or not isinstance(manifest.get("entries"), list)
            ):
                return None
            entries = {}
            for entry in manifest["entries"]:
                if not isinstance(entry, dict):
                    return None
                name = entry.get("name")
                expected = entry.get("expected")
                if (
                    not isinstance(name, str)
                    or name in entries
                    or not isinstance(expected, dict)
                ):
                    return None
                entries[name] = {"expected": expected}
            return entries
        except (OSError, ValueError, TypeError, KeyError):
            return None

    def _recovery_error_path(self) -> Path:
        return self.recovery_root if self.recovery_root.exists() else self.backup_root

    def _prepare_recovery_copy(self) -> None:
        sources = (
            (self.backup_report_dir, self.recovery_report_dir, "report"),
            (self.backup_snapshot_path, self.recovery_snapshot_path, "snapshot"),
            (self.backup_site_index, self.recovery_site_index, "site-index"),
            (self.backup_styles, self.recovery_styles, "styles"),
        )
        entries = [
            {
                "name": name,
                "expected": self._artifact_specs[name],
                "restore": self._restore_candidates[name],
            }
            for _, _, name in sources
        ]
        to_copy = [
            (source, destination, name)
            for source, destination, name in sources
            if self._restore_candidates[name]
        ]
        self.recovery_root.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": 2,
            "state": "copying",
            "entries": entries,
        }
        _atomic_write(
            self.recovery_root / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        )
        _fsync_directory(self.transaction_root)
        _fsync_directory(self.transaction_root.parent)
        for source, destination, name in to_copy:
            if not source.exists():
                raise OSError(f"Missing publication backup for {name}: {source}")
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
            _fsync_tree(destination)
            if not _artifact_matches(destination, self._artifact_specs[name]):
                raise OSError(
                    f"Publication recovery copy verification failed: {destination}"
                )
        _fsync_tree(self.recovery_root)
        manifest["state"] = "ready"
        _atomic_write(
            self.recovery_root / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True) + "\n",
        )
        _fsync_tree(self.recovery_root)
        _fsync_directory(self.transaction_root)
        _fsync_directory(self.transaction_root.parent)


def _fetch_direct(
    provider: MarketDataProvider | None,
    code: str,
    *,
    report_date: date,
    settings: Settings,
) -> FetchedBars:
    if provider is None:
        raise ValueError("provider or service must be supplied")
    raw_bars = provider.get_daily_bars(code, end=report_date)
    if isinstance(raw_bars, (str, bytes)) or not isinstance(raw_bars, Sequence):
        raise TypeError(f"Provider {provider.name} returned a non-sequence response")
    quality = validate_bars(
        code,
        raw_bars,
        as_of=report_date,
        settings=DataQualitySettings(
            minimum_history_bars=settings.market_data.minimum_history_bars,
            max_completed_trading_day_lag=settings.market_data.max_completed_trading_day_lag,
        ),
    )
    if not quality.analysis_allowed:
        from stock_daily_report.providers.service import DataQualityError

        raise DataQualityError(provider.name, quality)
    try:
        normalized = tuple(
            item if isinstance(item, DailyBar) else DailyBar.model_validate(item)
            for item in raw_bars
        )
    except (ValidationError, TypeError, ValueError) as error:
        raise ValueError(f"Could not normalize provider data for {code}: {error}") from error
    return FetchedBars(
        code=code,
        provider_name=provider.name,
        bars=normalized,
        quality=quality,
        from_cache=False,
    )


def _build_report(
    settings: Settings,
    watchlist: Watchlist,
    fetched: Mapping[str, FetchedBars],
    *,
    snapshot_path: Path,
    snapshot_hash: str,
    report_date: date,
    generated_at: datetime,
    market_state: MarketState | None,
    market_state_identity: str | None,
    market_rankings: MarketRankings,
    light_quotes: Mapping[str, UniverseQuote],
    light_quote_error: str | None,
    scan_ranking: Sequence[tuple[str, float]] | None = None,
    watchlist_scan_info: WatchlistScanInfo | None = None,
) -> ReportDocument:
    rule_hash = configuration_hash(resolve_risk_rules(settings))
    stocks: list[StockReport] = []
    latest_source_timestamp: datetime | None = None
    provider_names: set[str] = set()
    scan_rank_by_code = (
        {
            code: (index + 1, score)
            for index, (code, score) in enumerate(scan_ranking)
        }
        if scan_ranking
        else {}
    )
    # Only core stocks receive full analysis; extended stocks appear in the
    # lightweight pool overview instead of consuming history and indicators.
    analyzed = [
        stock
        for stock in sorted(
            watchlist.stocks,
            key=lambda item: (
                scan_rank_by_code.get(item.code, (float("inf"), 0.0))[0],
                item.code,
            ),
        )
        if stock.code in fetched
    ]
    for stock in analyzed:
        item = fetched[stock.code]
        bars = list(item.bars)
        metrics = calculate_technical_metrics(bars)
        trend = classify_trend(bars)
        structure = SimplifiedChanAnalyzer().analyze(bars)
        decision = decide(
            metrics=metrics,
            structure=structure,
            quality=item.quality,
            trend=trend,
            settings=settings,
        )
        item_latest_timestamp = max(bar.source_timestamp for bar in bars)
        latest_source_timestamp = (
            item_latest_timestamp
            if latest_source_timestamp is None
            else max(latest_source_timestamp, item_latest_timestamp)
        )
        provider_names.add(item.provider_name)
        stocks.append(
            StockReport(
                code=stock.code,
                name=stock.name,
                group=stock.group,
                scan_rank=(
                    scan_rank_by_code.get(stock.code, (None, None))[0]
                ),
                scan_score=(
                    scan_rank_by_code.get(stock.code, (None, None))[1]
                ),
                provider_name=item.provider_name,
                latest_trade_date=bars[-1].trade_date,
                latest_source_timestamp=item_latest_timestamp,
                bar_count=len(bars),
                quality_status="passed",
                quality_issues=item.quality.issue_codes,
                metrics=_report_metrics(metrics),
                structure=StructureSummary(
                    state_label=structure.state.label,
                    status=structure.state.status,
                    rule_version=RULE_VERSION,
                    levels=tuple(
                        StructureLevel(
                            kind=level.kind,
                            price=level.price,
                            source=level.source,
                        )
                        for level in structure.support_resistance
                    ),
                    observations=tuple(
                        observation.code for observation in structure.observations
                    ),
                ),
                decision_label=decision.label,
                evidence=decision.evidence,
                risks=decision.risk_codes,
                key_levels=tuple(
                    f"{level.kind} {level.price:.4f} ({level.source})"
                    for level in decision.key_levels
                ),
                next_conditions=decision.next_conditions,
            )
        )
    assert latest_source_timestamp is not None
    snapshot_reference = _relative_to_root(snapshot_path)
    return ReportDocument(
        metadata=ReportMetadata(
            report_date=report_date,
            generated_at=generated_at,
            latest_source_timestamp=latest_source_timestamp,
            snapshot_path=snapshot_reference,
            snapshot_hash=snapshot_hash,
            provider_names=tuple(sorted(provider_names)),
            config_hash=rule_hash,
            analyzer_versions=AnalyzerMetadata(structural=RULE_VERSION),
            quality_status="passed",
            stock_count=len(watchlist.stocks),
            analyzed_stock_count=len(stocks),
            render_version=REPORT_RENDER_VERSION,
        ),
        market_summary=_build_market_summary(fetched),
        market_state=market_state,
        market_state_identity=market_state_identity,
        market_rankings=market_rankings,
        stocks=tuple(stocks),
        pool_overview=_build_pool_overview(
            watchlist,
            light_quotes,
            light_quote_error,
            report_date=report_date,
            scan_ranking=scan_ranking,
            watchlist_scan_info=watchlist_scan_info,
        ),
    )


def _build_pool_overview(
    watchlist: Watchlist,
    light_quotes: Mapping[str, UniverseQuote],
    light_quote_error: str | None,
    *,
    report_date: date,
    scan_ranking: Sequence[tuple[str, float]] | None = None,
    watchlist_scan_info: WatchlistScanInfo | None = None,
) -> PoolOverview | None:
    """Build the lightweight full-pool overview, or None in legacy mode."""
    if not light_quotes and light_quote_error is None:
        return None
    rank_by_code = (
        {code: (index + 1, score) for index, (code, score) in enumerate(scan_ranking)}
        if scan_ranking
        else {}
    )
    rows: list[PoolOverviewRow] = []
    for stock in sorted(watchlist.stocks, key=lambda item: item.code):
        quote = light_quotes.get(stock.code)
        rank, score = rank_by_code.get(stock.code, (None, None))
        rows.append(
            PoolOverviewRow(
                code=stock.code,
                name=stock.name,
                group=stock.group,
                priority=stock.priority or "core",
                latest_price=quote.latest_price if quote else None,
                change_pct=quote.change_pct if quote else None,
                amount=quote.amount if quote else None,
                scan_rank=rank,
                scan_score=score,
            )
        )
    quote_dates = {quote.quote_date for quote in light_quotes.values()}
    quote_date = (
        max(quote_dates) if quote_dates else report_date
    )
    return PoolOverview(
        quote_date=quote_date,
        rows=tuple(rows),
        unavailable_reason=light_quote_error,
        scan_date=(
            watchlist_scan_info.scan_date if watchlist_scan_info is not None else None
        ),
        config_hash=(
            watchlist_scan_info.config_hash if watchlist_scan_info is not None else None
        ),
        input_hash=(
            watchlist_scan_info.input_hash if watchlist_scan_info is not None else None
        ),
    )


def _load_market_rankings(
    root: Path,
    report_date: date,
    market_state_settings: MarketStateSettings,
) -> MarketRankings:
    scan_path = root / "market-scans" / report_date.isoformat() / "scan.json"
    if not scan_path.exists():
        if _scan_progress_exists(root, report_date):
            return _unavailable_market_rankings("scan_incomplete")
        return _unavailable_market_rankings("scan_artifact_missing")
    try:
        artifact = load_scan_artifact(
            scan_path,
            market_state_settings=market_state_settings,
        )
    except MarketScanArtifactError:
        return _unavailable_market_rankings("scan_artifact_invalid")
    if artifact.report_date != report_date:
        return _unavailable_market_rankings("scan_date_mismatch")
    if any(status.status == "not_processed" for status in artifact.statuses):
        return _unavailable_market_rankings("scan_incomplete", artifact)
    if not artifact.rankings.trend or not artifact.rankings.balanced:
        return _unavailable_market_rankings(
            "scan_rankings_unavailable",
            artifact,
        )
    return _market_rankings_from_artifact(artifact)


def _load_market_state(
    root: Path,
    report_date: date,
    market_state_settings: MarketStateSettings,
) -> MarketState | None:
    scan_path = root / "market-scans" / report_date.isoformat() / "scan.json"
    if not scan_path.exists():
        return None
    try:
        artifact = load_scan_artifact(
            scan_path,
            market_state_settings=market_state_settings,
        )
    except MarketScanArtifactError:
        return None
    if artifact.report_date != report_date:
        return None
    return artifact.market_state


def _load_market_state_identity(
    root: Path,
    report_date: date,
    market_state_settings: MarketStateSettings,
) -> str | None:
    scan_path = root / "market-scans" / report_date.isoformat() / "scan.json"
    if not scan_path.exists():
        return None
    try:
        artifact = load_scan_artifact(
            scan_path,
            market_state_settings=market_state_settings,
        )
    except MarketScanArtifactError:
        return None
    if artifact.report_date != report_date:
        return None
    return artifact.market_state_identity


def _scan_progress_exists(root: Path, report_date: date) -> bool:
    """Return whether a resumable scan has an in-flight checkpoint."""

    progress_path = root / "market-scans" / report_date.isoformat() / "progress.json"
    return progress_path.exists()


def _unavailable_market_rankings(
    reason: MarketRankingsUnavailableReason,
    artifact: MarketScanArtifact | None = None,
) -> MarketRankings:
    if artifact is None:
        return MarketRankings(status="unavailable", unavailable_reason=reason)
    return MarketRankings(
        status="unavailable",
        unavailable_reason=reason,
        scan_date=artifact.report_date,
        generated_at=artifact.generated_at,
        rule_version=artifact.rule_version,
        config_hash=artifact.config_hash,
        input_hash=artifact.input_hash,
        provider_names=artifact.provider_names,
        universe_count=artifact.universe_count,
        eligible_count=artifact.eligible_count,
        valid_count=artifact.valid_count,
        coverage=artifact.coverage,
        exclusion_counts=_market_reason_counts(artifact.exclusion_counts),
        failure_counts=_market_reason_counts(artifact.failure_counts),
    )


def _market_rankings_from_artifact(
    artifact: MarketScanArtifact,
) -> MarketRankings:
    trend = tuple(_market_ranking(record) for record in artifact.rankings.trend)
    balanced = tuple(
        _market_ranking(record) for record in artifact.rankings.balanced
    )
    trend_by_code = {record.code: record for record in trend}
    balanced_by_code = {record.code: record for record in balanced}
    consensus = tuple(
        _market_consensus_ranking(
            record,
            trend_by_code[record.code],
            balanced_by_code[record.code],
        )
        for record in artifact.consensus
    )
    return MarketRankings(
        status="available",
        unavailable_reason=None,
        scan_date=artifact.report_date,
        generated_at=artifact.generated_at,
        rule_version=artifact.rule_version,
        config_hash=artifact.config_hash,
        input_hash=artifact.input_hash,
        provider_names=artifact.provider_names,
        universe_count=artifact.universe_count,
        eligible_count=artifact.eligible_count,
        valid_count=artifact.valid_count,
        coverage=artifact.coverage,
        exclusion_counts=_market_reason_counts(artifact.exclusion_counts),
        failure_counts=_market_reason_counts(artifact.failure_counts),
        trend=trend,
        balanced=balanced,
        consensus=consensus,
    )


def _market_ranking(record: RankingRecord) -> MarketRanking:
    return MarketRanking(
        code=record.code,
        name=record.name,
        profile=record.profile,
        rank=record.rank,
        score=record.score,
        components=MarketRankingComponents.model_validate(
            record.components.model_dump()
        ),
        evidence_codes=record.evidence_codes,
        risk_codes=record.risk_codes,
        latest_trade_date=record.latest_trade_date,
        provider_name=record.provider_name,
    )


def _market_consensus_ranking(
    record: ConsensusRecord,
    trend: MarketRanking,
    balanced: MarketRanking,
) -> MarketConsensusRanking:
    return MarketConsensusRanking(
        code=record.code,
        name=record.name,
        trend_rank=record.trend_rank,
        trend_score=record.trend_score,
        balanced_rank=record.balanced_rank,
        balanced_score=record.balanced_score,
        trend_components=trend.components,
        balanced_components=balanced.components,
        evidence_codes=tuple(
            sorted(set(trend.evidence_codes).union(balanced.evidence_codes))
        ),
        risk_codes=tuple(
            sorted(set(trend.risk_codes).union(balanced.risk_codes))
        ),
        latest_trade_date=record.latest_trade_date,
        provider_name=record.provider_name,
    )


def _market_reason_counts(counts: Mapping[str, int]) -> tuple[MarketScanReasonCount, ...]:
    return tuple(
        MarketScanReasonCount(code=code, count=count)
        for code, count in sorted(counts.items())
    )


def _build_market_summary(fetched: Mapping[str, FetchedBars]) -> MarketSummary:
    """Summarize only the latest returns and source timestamps in validated data."""

    latest_returns = [
        bars[-1].close / bars[-2].close - 1.0
        for result in fetched.values()
        if (bars := result.bars) and len(bars) >= 2
    ]
    up_count = sum(value > 0.0 for value in latest_returns)
    down_count = sum(value < 0.0 for value in latest_returns)
    unchanged_count = sum(value == 0.0 for value in latest_returns)
    average_return = fsum(latest_returns) / len(latest_returns) if latest_returns else None
    latest_source = max(
        bar.source_timestamp
        for result in fetched.values()
        for bar in result.bars
    )
    average_text = "n/a" if average_return is None else f"{average_return:.2%}"
    return MarketSummary(
        status="validated_watchlist",
        text=(
            "Broad-market data unavailable; validated watchlist only: "
            f"count={len(fetched)}, average_latest_return={average_text}, "
            f"up={up_count}, down={down_count}, unchanged={unchanged_count}, "
            f"latest_source={latest_source.isoformat()}."
        ),
    )


def _report_metrics(metrics: TechnicalMetrics) -> ReportMetrics:
    return ReportMetrics(
        close=metrics.close,
        ma20=metrics.ma20,
        ma60=metrics.ma60,
        return20=metrics.return20,
        return60=metrics.return60,
        realized_volatility20=metrics.realized_volatility20,
        drawdown60=metrics.drawdown60,
        volume_ratio20=metrics.volume_ratio20,
        recent_high20=metrics.recent_high20,
        recent_low20=metrics.recent_low20,
    )


def build_market_data_service(
    settings: Settings,
    *,
    providers: Mapping[str, MarketDataProvider] | None = None,
    fixture_directory: str | Path | None = None,
    now: Callable[[], datetime] | None = None,
    output_root: Path,
) -> MarketDataService:
    """Build the configured production provider, fallback, and cache chain."""

    active_now = now or (lambda: datetime.now(UTC))
    active_providers = dict(providers or {})
    fixture_path = Path(fixture_directory or _project_root() / "fixtures" / "bars")
    active_providers.setdefault("fixture", FixtureMarketDataProvider(fixture_path))
    active_providers.setdefault("akshare", AkShareMarketDataProvider(now=active_now))
    active_providers.setdefault("sina", SinaMarketDataProvider(now=active_now))
    cache_directory = Path(settings.market_data.cache_directory)
    if not cache_directory.is_absolute():
        cache_directory = output_root / cache_directory
    cache_directory = cache_directory.expanduser().resolve()
    active_settings = settings.market_data.model_copy(
        update={"cache_directory": str(cache_directory)}
    )
    return MarketDataService.from_settings(
        active_providers,
        active_settings,
        recover_pending=False,
        now=active_now,
    )


def _build_recovery_cache(settings: Settings, output_root: Path) -> RawResponseCache:
    cache_directory = Path(settings.market_data.cache_directory)
    if not cache_directory.is_absolute():
        cache_directory = output_root / cache_directory
    cache_directory = cache_directory.expanduser().resolve()
    return RawResponseCache(
        cache_directory,
        ttl_seconds=settings.market_data.cache_ttl_seconds,
        recover_pending=False,
    )


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            delete=False,
            prefix=f".{path.name}.",
            suffix=".tmp",
        ) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        _fsync_directory(path.parent)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _normalise_now(now: Callable[[], datetime] | None) -> datetime:
    value = now() if now is not None else datetime.now(UTC)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("now must return a timezone-aware datetime")
    return value.astimezone(UTC)


def _relative_to_root(path: Path) -> str:
    return path.relative_to(path.parents[2]).as_posix()


def _load_default_watchlist() -> Watchlist:
    return load_watchlist(_project_root() / "config" / "watchlist.yaml")


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


__all__ = [
    "PipelineError",
    "PipelineFailure",
    "PublicationRollbackError",
    "ReportOutputs",
    "run_daily_report",
]
