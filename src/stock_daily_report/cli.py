"""Command-line entry points for the daily report pipeline."""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from stock_daily_report.config import ConfigurationError, load_settings, load_watchlist
from stock_daily_report.pipeline import (
    PipelineError,
    PublicationRollbackError,
    run_daily_report,
)
from stock_daily_report.providers.fixture import FixtureMarketDataProvider
from stock_daily_report.providers.service import CacheRollbackError
from stock_daily_report.snapshots import SnapshotError


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stock-daily-report")
    subparsers = parser.add_subparsers(dest="command", required=True)
    daily = subparsers.add_parser("daily", help="publish one validated daily report")
    daily.add_argument("--date", required=True, type=date.fromisoformat)
    daily.add_argument("--settings", type=Path, default=_project_root() / "config/settings.yaml")
    daily.add_argument("--watchlist", type=Path, default=_project_root() / "config/watchlist.yaml")
    daily.add_argument("--output-root", type=Path, default=Path.cwd())
    daily.add_argument("--fixture-directory", type=Path)
    args = parser.parse_args(argv)

    if args.command == "daily":
        try:
            settings = load_settings(args.settings)
            watchlist = load_watchlist(args.watchlist)
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
            )
        except (
            CacheRollbackError,
            ConfigurationError,
            PipelineError,
            PublicationRollbackError,
            SnapshotError,
        ) as error:
            print(error, file=sys.stderr)
            return 1
        print(outputs.html_path)
        return 0
    return 2


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


if __name__ == "__main__":
    raise SystemExit(main())
