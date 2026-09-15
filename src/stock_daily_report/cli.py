"""Command-line entry points for the daily report pipeline."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from stock_daily_report.backtest.runner import run_backtest
from stock_daily_report.config import (
    ConfigurationError,
    load_backtest_settings,
    load_market_scan_settings,
    load_settings,
    load_watchlist,
)
from stock_daily_report.market_scan.report import (
    MarketScanArtifactError,
    load_scan_artifact,
)
from stock_daily_report.market_scan.resume import CheckpointError
from stock_daily_report.market_scan.runner import (
    market_scan_config_hash,
    run_market_scan,
)
from stock_daily_report.models import MarketStateSettings
from stock_daily_report.notify import NotificationDeliveryError, NotificationService
from stock_daily_report.pipeline import (
    PipelineError,
    PublicationRollbackError,
    build_market_data_service,
    run_daily_report,
)
from stock_daily_report.providers.base import ProviderError
from stock_daily_report.providers.fixture import FixtureMarketDataProvider
from stock_daily_report.providers.index import AkShareIndexProvider
from stock_daily_report.providers.service import CacheRollbackError
from stock_daily_report.providers.universe import (
    AkShareUniverseProvider,
    WatchlistUniverseProvider,
)
from stock_daily_report.report.models import ReportDocument
from stock_daily_report.snapshots import SnapshotError


class _MarketScanCliError(ValueError):
    """Raised for safe market-scan command usage errors."""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock-daily-report")
    subparsers = parser.add_subparsers(dest="command", required=True)
    daily = subparsers.add_parser("daily", help="publish one validated daily report")
    daily.add_argument("--date", required=True, type=date.fromisoformat)
    daily.add_argument("--settings", type=Path, default=_project_root() / "config/settings.yaml")
    daily.add_argument("--watchlist", type=Path, default=_project_root() / "config/watchlist.yaml")
    daily.add_argument("--output-root", type=Path, default=Path.cwd())
    daily.add_argument("--fixture-directory", type=Path)
    daily.add_argument(
        "--skip-notifications",
        action="store_true",
        help="defer notifications until after static-site deployment",
    )
    daily.add_argument(
        "--reuse-existing-snapshot",
        action="store_true",
        help="refresh report rendering without refetching an immutable snapshot",
    )
    daily.add_argument(
        "--overwrite-snapshot",
        action="store_true",
        help="replace the date's snapshot when the deep-analysis set changed",
    )
    daily.add_argument(
        "--report-url",
        help="Final absolute published report URL; otherwise REPORT_BASE_URL is used",
    )
    notify = subparsers.add_parser(
        "notify", help="send notifications for an already published report"
    )
    notify.add_argument("--report", type=Path, required=True)
    notify.add_argument(
        "--settings", type=Path, default=_project_root() / "config/settings.yaml"
    )
    notify.add_argument("--report-url", required=True)
    backtest = subparsers.add_parser(
        "backtest", help="run structural and execution-aware comparisons"
    )
    backtest.add_argument(
        "--settings", type=Path, default=_project_root() / "config/backtest.yaml"
    )
    backtest.add_argument(
        "--watchlist", type=Path, default=_project_root() / "config/watchlist.yaml"
    )
    backtest.add_argument("--fixture-directory", type=Path, required=True)
    backtest.add_argument("--output-root", type=Path, default=Path.cwd())
    backtest.add_argument("--date", type=date.fromisoformat)
    market_scan = subparsers.add_parser(
        "market-scan", help="run one full-market opportunity scan"
    )
    market_scan.add_argument(
        "--date",
        required=True,
        type=date.fromisoformat,
        help="current Asia/Shanghai date unless reusing a validated artifact",
    )
    market_scan.add_argument(
        "--settings",
        type=Path,
        default=_project_root() / "config/market_scan.yaml",
    )
    market_scan.add_argument(
        "--data-settings",
        type=Path,
        default=_project_root() / "config/settings.yaml",
    )
    market_scan.add_argument("--output-root", type=Path, default=Path.cwd())
    market_scan.add_argument(
        "--resumable",
        action="store_true",
        help="process in resumable batches, persisting a checkpoint after each",
    )
    market_scan.add_argument(
        "--max-batches",
        type=int,
        default=None,
        help="stop after this many batches, leaving the rest for a later run",
    )
    market_scan.add_argument(
        "--scope",
        choices=("market", "watchlist"),
        default="market",
        help="scan the full market, or only the watchlist (watchlist-scans/)",
    )
    market_scan.add_argument(
        "--watchlist",
        type=Path,
        default=_project_root() / "config/watchlist.yaml",
        help="watchlist used when --scope watchlist is selected",
    )
    args = parser.parse_args(argv)

    if args.command == "daily":
        try:
            settings = load_settings(args.settings)
            watchlist = load_watchlist(args.watchlist)
            market_scan_settings = load_market_scan_settings(
                _project_root() / "config/market_scan.yaml"
            )
            provider = (
                FixtureMarketDataProvider(args.fixture_directory)
                if args.fixture_directory is not None
                else None
            )
            outputs = run_daily_report(
                settings,
                output_root=args.output_root,
                watchlist=watchlist,
                provider=provider,
                report_date=args.date,
                reuse_existing_snapshot=args.reuse_existing_snapshot,
                overwrite_snapshot=args.overwrite_snapshot,
                market_scan_settings=market_scan_settings.market_state,
            )
            if settings.notifications.enabled_channels and not args.skip_notifications:
                if args.report_url:
                    report_url = args.report_url
                else:
                    report_base_url = os.environ.get("REPORT_BASE_URL", "").rstrip("/")
                    report_url = (
                        f"{report_base_url}/reports/{args.date.isoformat()}/"
                        if report_base_url
                        else ""
                    )
                NotificationService(settings.notifications).send_report(
                    outputs.report,
                    report_url=report_url,
                )
        except (
            CacheRollbackError,
            ConfigurationError,
            NotificationDeliveryError,
            PipelineError,
            PublicationRollbackError,
            SnapshotError,
        ) as error:
            print(error, file=sys.stderr)
            return 1
        print(outputs.html_path)
        return 0
    if args.command == "notify":
        try:
            settings = load_settings(args.settings)
            report = ReportDocument.model_validate_json(
                args.report.read_text(encoding="utf-8")
            )
            NotificationService(settings.notifications).send_report(
                report,
                report_url=args.report_url,
            )
        except (
            ConfigurationError,
            NotificationDeliveryError,
            OSError,
            ValidationError,
        ) as error:
            print(error, file=sys.stderr)
            return 1
        print("Notifications delivered")
        return 0
    if args.command == "backtest":
        try:
            settings = load_backtest_settings(args.settings)
            watchlist = load_watchlist(args.watchlist)
            report_path = run_backtest(
                settings,
                watchlist,
                FixtureMarketDataProvider(args.fixture_directory),
                output_root=args.output_root,
                report_date=args.date,
            )
        except (ConfigurationError, OSError, ProviderError, ValueError) as error:
            print(error, file=sys.stderr)
            return 1
        print(report_path)
        return 0
    if args.command == "market-scan":
        try:
            if args.max_batches is not None and args.max_batches <= 0:
                raise _MarketScanCliError(
                    "--max-batches must be a positive integer"
                )
            settings = load_market_scan_settings(args.settings)
            data_settings = load_settings(args.data_settings)
            configuration_hash = market_scan_config_hash(
                settings,
                data_settings.market_data,
            )
            directory = (
                "watchlist-scans" if args.scope == "watchlist" else "market-scans"
            )
            index_provider = (
                AkShareIndexProvider() if directory == "market-scans" else None
            )
            if args.scope == "watchlist":
                universe_provider: object = WatchlistUniverseProvider(
                    (stock.code for stock in load_watchlist(args.watchlist).stocks),
                    AkShareUniverseProvider(),
                )
            else:
                universe_provider = AkShareUniverseProvider()
            artifact_path = _scan_artifact_path(args.output_root, args.date, directory)
            if artifact_path.exists():
                artifact_path = _existing_scan_artifact(
                    artifact_path,
                    report_date=args.date,
                    expected_config_hash=configuration_hash,
                    expected_rule_version=settings.rule_version,
                    market_state_settings=settings.market_state,
                )
            elif args.resumable:
                current_date = _current_market_date()
                if args.date != current_date:
                    raise _MarketScanCliError(
                        "Resumable live scans only support the current "
                        f"Asia/Shanghai date {current_date.isoformat()}; "
                        f"requested {args.date.isoformat()}."
                    )
                from stock_daily_report.market_scan.resumable import (
                    run_resumable_scan,
                )

                artifact_path = run_resumable_scan(
                    settings,
                    universe_provider,
                    build_market_data_service(
                        data_settings,
                        output_root=args.output_root,
                    ),
                    report_date=args.date,
                    output_root=args.output_root,
                    configuration_hash=configuration_hash,
                    index_provider=index_provider,
                    max_batches=args.max_batches,
                    directory=directory,
                )
                if artifact_path is None:
                    print(
                        "scan incomplete; checkpoint persisted, resume later",
                        file=sys.stderr,
                    )
                    return 0
            else:
                current_date = _current_market_date()
                if args.date != current_date:
                    raise _MarketScanCliError(
                        "Live market scans only support the current "
                        f"Asia/Shanghai date {current_date.isoformat()}; "
                        f"requested {args.date.isoformat()}."
                    )
                history_service = build_market_data_service(
                    data_settings,
                    output_root=args.output_root,
                )
                if directory == "market-scans":
                    artifact_path = run_market_scan(
                        settings,
                        universe_provider,
                        history_service,
                        report_date=args.date,
                        output_root=args.output_root,
                        configuration_hash=configuration_hash,
                        index_provider=index_provider,
                    )
                else:
                    artifact_path = run_market_scan(
                        settings,
                        universe_provider,
                        history_service,
                        report_date=args.date,
                        output_root=args.output_root,
                        configuration_hash=configuration_hash,
                        directory=directory,
                    )
        except (
            ConfigurationError,
            CheckpointError,
            MarketScanArtifactError,
            ProviderError,
            _MarketScanCliError,
        ) as error:
            print(error, file=sys.stderr)
            return 1
        print(artifact_path)
        return 0
    return 2


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _current_market_date() -> date:
    return datetime.now(ZoneInfo("Asia/Shanghai")).date()


def _scan_artifact_path(
    output_root: Path, report_date: date, directory: str = "market-scans"
) -> Path:
    return output_root / directory / report_date.isoformat() / "scan.json"


def _existing_scan_artifact(
    path: Path,
    *,
    report_date: date,
    expected_config_hash: str,
    expected_rule_version: str,
    market_state_settings: MarketStateSettings,
) -> Path:
    artifact = load_scan_artifact(
        path,
        market_state_settings=market_state_settings,
    )
    if artifact.report_date != report_date:
        raise MarketScanArtifactError(
            "Existing market scan artifact report_date does not match "
            f"requested date: {path}"
        )
    if (
        artifact.rule_version != expected_rule_version
        or artifact.config_hash != expected_config_hash
    ):
        raise MarketScanArtifactError(
            f"Existing market scan artifact configuration does not match: {path}"
        )
    return path


if __name__ == "__main__":
    raise SystemExit(main())
