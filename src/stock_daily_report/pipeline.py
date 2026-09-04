"""Composition layer for the deterministic daily report pipeline."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
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
from stock_daily_report.models import DailyBar, Settings, Watchlist
from stock_daily_report.providers.akshare import AkShareMarketDataProvider
from stock_daily_report.providers.base import MarketDataProvider, ProviderError
from stock_daily_report.providers.fixture import FixtureMarketDataProvider
from stock_daily_report.providers.service import FetchedBars, MarketDataService
from stock_daily_report.quality.checks import (
    DataQualityResult,
    DataQualitySettings,
    validate_bars,
)
from stock_daily_report.report.models import (
    AnalyzerMetadata,
    MarketSummary,
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
from stock_daily_report.snapshots import load_snapshot, write_snapshot


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


@dataclass(frozen=True)
class ReportOutputs:
    """Paths and canonical model returned after a successful publication."""

    json_path: Path
    markdown_path: Path
    html_path: Path
    snapshot_path: Path
    report: ReportDocument


def run_daily_report(
    settings: Settings | str | Path,
    *,
    output_root: str | Path,
    watchlist: Watchlist | None = None,
    provider: MarketDataProvider | None = None,
    service: MarketDataService | None = None,
    providers: Mapping[str, MarketDataProvider] | None = None,
    fixture_directory: str | Path | None = None,
    report_date: date | None = None,
    now: Callable[[], datetime] | None = None,
) -> ReportOutputs:
    """Fetch, validate, snapshot, analyze, and publish one daily report.

    Retrieval and quality validation complete for every watchlist code before
    the immutable snapshot or any success-shaped report artifact is written.
    """

    active_settings = (
        load_settings(settings) if isinstance(settings, (str, Path)) else settings
    )
    active_watchlist = watchlist or _load_default_watchlist()
    generated_at = _normalise_now(now)
    active_report_date = report_date or generated_at.date()
    if not isinstance(active_report_date, date) or isinstance(
        active_report_date, datetime
    ):
        raise TypeError("report_date must be a date, not a datetime")
    root = Path(output_root)
    failures: list[PipelineFailure] = []
    fetched: dict[str, FetchedBars] = {}

    active_service = service
    if active_service is None and provider is None:
        active_service = _build_default_service(
            active_settings,
            providers=providers,
            fixture_directory=fixture_directory,
            now=lambda: generated_at,
            output_root=root,
        )

    for stock in sorted(active_watchlist.stocks, key=lambda item: item.code):
        try:
            result = (
                active_service.fetch(
                    stock.code,
                    end=active_report_date,
                    as_of=active_report_date,
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
        except (ProviderError, ValidationError, TypeError, ValueError) as error:
            quality = getattr(error, "quality", None)
            issue_codes = (
                quality.issue_codes if isinstance(quality, DataQualityResult) else ()
            )
            failures.append(
                PipelineFailure(stock.code, str(error), tuple(issue_codes))
            )

    report_dir = root / "reports" / active_report_date.isoformat()
    _remove_report_artifacts(report_dir)
    if failures:
        raise PipelineError(failures)

    bars_by_code = {code: result.bars for code, result in fetched.items()}
    snapshot_path = write_snapshot(
        root,
        report_date=active_report_date,
        bars_by_code=bars_by_code,
        generated_at=generated_at,
    )
    snapshot = load_snapshot(snapshot_path)
    report = _build_report(
        active_settings,
        active_watchlist,
        fetched,
        snapshot_path=snapshot_path,
        snapshot_hash=snapshot.content_hash,
        report_date=active_report_date,
        generated_at=generated_at,
    )

    report_dir.mkdir(parents=True, exist_ok=True)
    json_path = report_dir / "report.json"
    markdown_path = report_dir / "report.md"
    html_path = report_dir / "index.html"
    try:
        _atomic_write(json_path, report.model_dump_json(indent=2) + "\n")
        _atomic_write(markdown_path, render_markdown(report))
        _atomic_write(html_path, render_html(report))
        _write_site_index(root)
    except Exception:
        _remove_report_artifacts(report_dir)
        raise
    return ReportOutputs(
        json_path=json_path,
        markdown_path=markdown_path,
        html_path=html_path,
        snapshot_path=snapshot_path,
        report=report,
    )


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
) -> ReportDocument:
    rule_hash = configuration_hash(resolve_risk_rules(settings))
    stocks: list[StockReport] = []
    latest_source_timestamp: datetime | None = None
    provider_names: set[str] = set()
    for stock in sorted(watchlist.stocks, key=lambda item: item.code):
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
            stock_count=len(stocks),
        ),
        market_summary=MarketSummary(
            status="unavailable",
            text=(
                "Broad market data unavailable; summary is limited to validated "
                "watchlist data and does not invent breadth or sector figures."
            ),
        ),
        stocks=tuple(stocks),
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


def _build_default_service(
    settings: Settings,
    *,
    providers: Mapping[str, MarketDataProvider] | None,
    fixture_directory: str | Path | None,
    now: Callable[[], datetime],
    output_root: Path,
) -> MarketDataService:
    active_providers = dict(providers or {})
    fixture_path = Path(fixture_directory or _project_root() / "fixtures" / "bars")
    active_providers.setdefault("fixture", FixtureMarketDataProvider(fixture_path))
    active_providers.setdefault("akshare", AkShareMarketDataProvider(now=now))
    cache_directory = Path(settings.market_data.cache_directory)
    if not cache_directory.is_absolute():
        cache_directory = output_root / cache_directory
    active_settings = settings.market_data.model_copy(
        update={"cache_directory": str(cache_directory)}
    )
    return MarketDataService.from_settings(
        active_providers,
        active_settings,
        now=now,
    )


def _write_site_index(root: Path) -> None:
    reports_root = root / "reports"
    report_dates = [
        path.name
        for path in reports_root.iterdir()
        if path.is_dir() and (path / "index.html").exists()
    ] if reports_root.exists() else []
    site_path = root / "site" / "index.html"
    site_path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(site_path, render_site_index(report_dates))
    styles_source = _project_root() / "site" / "styles.css"
    styles_target = site_path.parent / "styles.css"
    if styles_source.exists() and not styles_target.exists():
        _atomic_write(styles_target, styles_source.read_text(encoding="utf-8"))


def _remove_report_artifacts(report_dir: Path) -> None:
    for filename in ("report.json", "report.md", "index.html"):
        (report_dir / filename).unlink(missing_ok=True)


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
    "ReportOutputs",
    "run_daily_report",
]
