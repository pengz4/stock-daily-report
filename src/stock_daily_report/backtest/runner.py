"""Orchestration for reproducible execution-aware backtest reports."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Protocol

from stock_daily_report.chan.simplified import SimplifiedChanAnalyzer
from stock_daily_report.chan.strict import StrictChanAnalyzer, load_strict_profile
from stock_daily_report.models import BacktestSettings, DailyBar, Watchlist

from .evaluate import evaluate_signals
from .execution import Costs
from .replay import (
    SIMPLIFIED_BREAKOUT_STRATEGY_VERSION,
    ReplayEvent,
    execution_signals,
    replay,
)
from .report import build_report, write_report


class MarketDataProvider(Protocol):
    """Minimal provider interface needed by the backtest runner."""

    def get_daily_bars(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[DailyBar]:
        """Return chronological normalized bars."""


def run_backtest(
    settings: BacktestSettings,
    watchlist: Watchlist,
    provider: MarketDataProvider,
    *,
    output_root: str | Path,
    report_date: date | None = None,
) -> Path:
    """Run both analyzers for the watchlist and write one audit report."""

    analyzers = {
        "simplified-v1": SimplifiedChanAnalyzer(),
        "strict-v1": StrictChanAnalyzer(load_strict_profile()),
    }
    events_by_analyzer: dict[str, list[ReplayEvent]] = {
        name: [] for name in analyzers
    }
    evaluations: dict[str, dict[str, object]] = {}
    snapshot_payload: dict[str, object] = {}
    latest_bar_dates: list[date] = []
    holding_periods = tuple(
        dict.fromkeys(
            (
                settings.holding_days,
                *(settings.holding_periods or ()),
            )
        )
    )
    for stock in watchlist.stocks:
        bars = provider.get_daily_bars(stock.code, end=report_date)
        if not bars:
            raise ValueError(f"No bars returned for stock {stock.code}")
        if report_date is not None and any(
            bar.trade_date > report_date for bar in bars
        ):
            raise ValueError(
                f"Provider returned bars after requested report date for {stock.code}"
            )
        latest_bar_dates.append(max(bar.trade_date for bar in bars))
        snapshot_payload[stock.code] = [
            bar.model_dump(mode="json") for bar in bars
        ]
        for name, analyzer in analyzers.items():
            events = [
                replace(
                    event,
                    event_id=f"{stock.code}:{event.event_id}",
                    symbol=stock.code,
                )
                for event in replay(bars, analyzer)
            ]
            events_by_analyzer[name].extend(events)
            evaluation_events = execution_signals(events)
            strategy_version = (
                SIMPLIFIED_BREAKOUT_STRATEGY_VERSION
                if name == "simplified-v1"
                else name
            )
            for holding_days in holding_periods:
                evaluation = evaluate_signals(
                    evaluation_events,
                    bars,
                    costs=Costs(
                        commission_bps=settings.costs.commission_bps,
                        slippage_bps=settings.costs.slippage_bps,
                        price_limit_pct=settings.costs.price_limit_pct,
                        price_tick=settings.costs.price_tick,
                    ),
                    holding_days=holding_days,
                    out_of_sample_start=settings.out_of_sample_start,
                    minimum_sample_count=settings.minimum_sample_count,
                ).to_dict()
                evaluation["strategy_version"] = strategy_version
                key = f"{name}:{stock.code}"
                if holding_days != settings.holding_days:
                    key = f"{key}:{holding_days}d"
                evaluations[key] = evaluation
    if report_date is None:
        report_date = max(latest_bar_dates)
    snapshot_hash = _sha256_json(snapshot_payload)
    profile_hashes = {
        "simplified-v1": _sha256_file(
            Path(__file__).parents[1] / "chan" / "simplified.py"
        ),
        "strict-v1": _sha256_json(load_strict_profile().model_dump(mode="json")),
    }
    report = build_report(
        report_date=report_date,
        snapshot_hash=snapshot_hash,
        events_by_analyzer=events_by_analyzer,
        analyzer_profile_hashes=profile_hashes,
        backtest_assumptions={
            **settings.model_dump(mode="json"),
            "execution_strategy_versions": {
                "simplified-v1": SIMPLIFIED_BREAKOUT_STRATEGY_VERSION,
                "strict-v1": "strict-v1",
            },
        },
        evaluations=evaluations,
    )
    return write_report(report, Path(output_root) / "reports" / "backtests")


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


__all__ = ["MarketDataProvider", "run_backtest"]
