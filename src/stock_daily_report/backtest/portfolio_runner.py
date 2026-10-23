"""Historical point-in-time ranking and portfolio report orchestration."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from datetime import date
from itertools import pairwise
from pathlib import Path
from typing import Protocol

from stock_daily_report.market_scan.filters import (
    filter_history,
    filter_universe_quote,
)
from stock_daily_report.market_scan.scoring import rank_scores, score_candidate
from stock_daily_report.models import (
    DailyBar,
    MarketScanSettings,
    PortfolioBacktestSettings,
    Watchlist,
)
from stock_daily_report.providers.universe import UniverseQuote

from .portfolio import PortfolioPoint, simulate_portfolio

_DATA_LIMITATIONS = (
    (
        "The configured watchlist and names are treated as the historical universe; "
        "membership, delisted securities, and historical risk labels are not reconstructed."
    ),
    (
        "Dividends, splits, and other corporate actions are not separately modeled; "
        "results inherit the supplied bar adjustment convention."
    ),
    (
        "The cost model does not include sell-side stamp duty or minimum brokerage "
        "charges; one configured price-limit rate is applied to all symbols, and "
        "limit checks use the supplied OHLC series."
    ),
    (
        "Unfilled equal-weight baseline slots remain in cash until the first "
        "executable session, so entry dates can be staggered."
    ),
    (
        "Positions without a new bar are valued at their last observed close; "
        "delisting recovery is not inferred."
    ),
    (
        "The equal-weight buy-and-hold baseline uses the same configured watchlist "
        "and is not a broad-market index benchmark."
    ),
    (
        "Only one configured holdout is evaluated; walk-forward validation and "
        "parameter-selection bias correction are not implemented."
    ),
)


class HistoricalMarketDataProvider(Protocol):
    """Provider contract used by historical portfolio replay."""

    def get_daily_bars(
        self,
        code: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[DailyBar]:
        raise NotImplementedError


def run_portfolio_backtest(
    settings: PortfolioBacktestSettings,
    scan_settings: MarketScanSettings,
    watchlist: Watchlist,
    provider: HistoricalMarketDataProvider,
    *,
    output_root: str | Path,
    report_date: date | None = None,
) -> Path:
    """Replay rankings by historical date and write an auditable portfolio report."""

    bars_by_symbol: dict[str, tuple[DailyBar, ...]] = {}
    for stock in watchlist.stocks:
        bars = tuple(provider.get_daily_bars(stock.code, end=report_date))
        if not bars:
            raise ValueError(f"No bars returned for stock {stock.code}")
        if report_date is not None and any(
            bar.trade_date > report_date for bar in bars
        ):
            raise ValueError(
                f"Provider returned bars after requested report date for {stock.code}"
            )
        if any(
            previous.trade_date >= current.trade_date
            for previous, current in pairwise(bars)
        ):
            raise ValueError(f"Bar dates must be strictly increasing for {stock.code}")
        bars_by_symbol[stock.code] = bars

    sessions = sorted(
        {
            bar.trade_date
            for bars in bars_by_symbol.values()
            for bar in bars
            if report_date is None or bar.trade_date <= report_date
        }
    )
    if not sessions:
        raise ValueError("No historical sessions available for portfolio replay")
    targets_by_date, coverage_by_date = _build_targets(
        bars_by_symbol,
        watchlist,
        scan_settings,
        settings,
        sessions,
    )
    simulation = simulate_portfolio(
        bars_by_symbol,
        targets_by_date,
        settings=settings,
    )
    snapshot_hash = _sha256_json(
        {
            code: [bar.model_dump(mode="json") for bar in bars]
            for code, bars in sorted(bars_by_symbol.items())
        }
    )
    report = {
        "schema_version": 1,
        "report_type": "portfolio-backtest",
        "report_date": sessions[-1].isoformat(),
        "input_sha256": snapshot_hash,
        "settings": settings.model_dump(mode="json"),
        "market_scan_settings": scan_settings.model_dump(mode="json"),
        "universe": [stock.code for stock in watchlist.stocks],
        "data_period": {
            "start": min(sessions).isoformat(),
            "end": max(sessions).isoformat(),
        },
        "signal_profile": settings.profile,
        "signal_coverage_by_date": {
            day.isoformat(): coverage for day, coverage in sorted(coverage_by_date.items())
        },
        "portfolio_curve": [_point_to_dict(point) for point in simulation.portfolio_curve],
        "baseline_curve": [_point_to_dict(point) for point in simulation.baseline_curve],
        "trades": [
            {
                **asdict(trade),
                "trade_date": trade.trade_date.isoformat(),
                "signal_date": (
                    trade.signal_date.isoformat() if trade.signal_date else None
                ),
            }
            for trade in simulation.trades
        ],
        "blocked_orders": simulation.blocked_orders,
        "strategy_metrics": dict(simulation.metrics),
        "baseline_metrics": dict(simulation.baseline_metrics),
        "metric_assumptions": {
            "annualization_sessions": 252,
            "sharpe_risk_free_rate": 0.0,
            "sample_count_unit": "daily equity observations",
            "turnover_definition": (
                "gross traded notional divided by initial capital"
            ),
        },
        "excess_return": _excess_return(
            simulation.metrics, simulation.baseline_metrics
        ),
        "evidence_status": str(simulation.metrics["evidence_status"]),
        "data_limitations": list(_DATA_LIMITATIONS),
    }
    destination = (
        Path(output_root)
        / "reports"
        / "backtests"
        / "portfolio"
        / sessions[-1].isoformat()
    )
    destination.mkdir(parents=True, exist_ok=True)
    report_path = destination / "report.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=True, allow_nan=False, indent=2, sort_keys=True)
        + "\n",
        encoding="utf-8",
    )
    return report_path


def _build_targets(
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    watchlist: Watchlist,
    scan_settings: MarketScanSettings,
    portfolio_settings: PortfolioBacktestSettings,
    sessions: Sequence[date],
) -> tuple[dict[date, tuple[str, ...]], dict[date, float]]:
    stocks_by_code = {stock.code: stock for stock in watchlist.stocks}
    targets: dict[date, tuple[str, ...]] = {}
    coverage_by_date: dict[date, float] = {}
    decision_sessions = [
        session
        for session in sessions
        if portfolio_settings.out_of_sample_start is None
        or session >= portfolio_settings.out_of_sample_start
    ]
    for decision_index, as_of in enumerate(decision_sessions):
        if decision_index % portfolio_settings.rebalance_every_sessions:
            continue
        quote_candidates: list[tuple[str, tuple[DailyBar, ...]]] = []
        scores = []
        for code, stock in stocks_by_code.items():
            history = tuple(
                bar for bar in bars_by_symbol[code] if bar.trade_date <= as_of
            )
            if not history:
                continue
            latest = history[-1]
            quote = UniverseQuote(
                code=code,
                name=stock.name,
                market=_market_for_code(code),
                latest_price=latest.close,
                volume=latest.volume,
                amount=latest.amount,
                quote_date=latest.trade_date,
            )
            if not filter_universe_quote(
                quote, report_date=as_of, settings=scan_settings
            ).eligible:
                continue
            quote_candidates.append((code, history))
        processed = quote_candidates[: scan_settings.max_candidates]
        for code, history in processed:
            if not filter_history(
                code, history, report_date=as_of, settings=scan_settings
            ).eligible:
                continue
            candidate_scores = score_candidate(code, history, as_of=as_of)
            scores.append(getattr(candidate_scores, portfolio_settings.profile))
        coverage = len(scores) / len(quote_candidates) if quote_candidates else 0.0
        coverage_by_date[as_of] = coverage
        if (
            len(processed) == len(quote_candidates)
            and coverage >= scan_settings.minimum_coverage_ratio
        ):
            targets[as_of] = tuple(
                score.code
                for score in rank_scores(scores)[: portfolio_settings.max_positions]
            )
        else:
            targets[as_of] = ()
    return targets, coverage_by_date


def _market_for_code(code: str) -> str:
    if code.startswith("920"):
        return "BJ"
    if code.startswith(("60", "68")):
        return "SH"
    if code.startswith(("00", "30")):
        return "SZ"
    raise ValueError(f"Unsupported A-share code in watchlist: {code}")


def _point_to_dict(point: PortfolioPoint) -> dict[str, object]:
    return {
        "trade_date": point.trade_date.isoformat(),
        "equity": point.equity,
        "cash": point.cash,
        "holdings_count": point.holdings_count,
    }


def _excess_return(
    strategy_metrics: Mapping[str, object],
    baseline_metrics: Mapping[str, object],
) -> float | None:
    if (
        strategy_metrics.get("evidence_status") != "enough_evidence"
        or baseline_metrics.get("evidence_status") != "enough_evidence"
    ):
        return None
    return float(strategy_metrics["total_return"]) - float(
        baseline_metrics["total_return"]
    )


def _sha256_json(value: object) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


__all__ = ["HistoricalMarketDataProvider", "run_portfolio_backtest"]
