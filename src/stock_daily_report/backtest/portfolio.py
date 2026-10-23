"""Deterministic next-session portfolio simulation for market-scan rankings."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from itertools import pairwise
from statistics import pstdev

from stock_daily_report.backtest.execution import (
    Costs,
    is_limit_down,
    is_limit_up,
    is_suspended,
)
from stock_daily_report.models import DailyBar, PortfolioBacktestSettings

_LOT_SIZE = 100
_ANNUAL_SESSIONS = 252


@dataclass(frozen=True)
class PortfolioTrade:
    trade_date: date
    signal_date: date | None
    code: str
    side: str
    shares: int
    raw_price: float
    commission: float
    slippage: float


@dataclass(frozen=True)
class PortfolioPoint:
    trade_date: date
    equity: float
    cash: float
    holdings_count: int


@dataclass(frozen=True)
class PortfolioSimulation:
    portfolio_curve: tuple[PortfolioPoint, ...]
    baseline_curve: tuple[PortfolioPoint, ...]
    trades: tuple[PortfolioTrade, ...]
    blocked_orders: int
    metrics: Mapping[str, object]
    baseline_metrics: Mapping[str, object]


def simulate_portfolio(
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    targets_by_date: Mapping[date, Sequence[str]],
    *,
    settings: PortfolioBacktestSettings,
) -> PortfolioSimulation:
    """Rebalance ranked targets at the next session's open and mark at close."""

    if not bars_by_symbol:
        raise ValueError("bars_by_symbol must not be empty")
    normalized: dict[str, tuple[DailyBar, ...]] = {}
    for code, raw_bars in bars_by_symbol.items():
        bars = tuple(raw_bars)
        if not bars:
            raise ValueError(f"No bars provided for symbol {code}")
        if any(
            previous.trade_date >= current.trade_date
            for previous, current in pairwise(bars)
        ):
            raise ValueError(f"Bar dates must be strictly increasing for {code}")
        normalized[code] = bars

    target_codes: dict[date, tuple[str, ...]] = {}
    for signal_date, raw_codes in targets_by_date.items():
        codes = tuple(raw_codes[: settings.max_positions])
        if len(codes) != len(set(codes)):
            raise ValueError(f"Duplicate target symbols on {signal_date}")
        unknown = sorted(set(codes) - normalized.keys())
        if unknown:
            raise ValueError(f"unknown symbols in target list: {', '.join(unknown)}")
        target_codes[signal_date] = codes

    bars_by_date = {
        code: {bar.trade_date: bar for bar in bars}
        for code, bars in normalized.items()
    }
    sessions = sorted(
        {bar.trade_date for bars in normalized.values() for bar in bars}
    )
    evaluation_sessions = [
        session
        for session in sessions
        if settings.out_of_sample_start is None
        or session >= settings.out_of_sample_start
    ]
    if not evaluation_sessions:
        return _empty_simulation(settings)

    session_index = {session: index for index, session in enumerate(sessions)}
    targets_for_execution: dict[date, tuple[date, tuple[str, ...]]] = {}
    eligible_signal_sessions = [
        session
        for session in sessions
        if settings.out_of_sample_start is None
        or session >= settings.out_of_sample_start
    ]
    rebalance_index = {
        session: index for index, session in enumerate(eligible_signal_sessions)
    }
    for signal_date, codes in target_codes.items():
        index = session_index.get(signal_date)
        if index is None:
            raise ValueError(f"Target date {signal_date} is not a market session")
        if (
            signal_date in rebalance_index
            and rebalance_index[signal_date] % settings.rebalance_every_sessions == 0
            and index + 1 < len(sessions)
        ):
            targets_for_execution[sessions[index + 1]] = (signal_date, codes)

    costs = Costs(
        commission_bps=settings.costs.commission_bps,
        slippage_bps=settings.costs.slippage_bps,
        price_limit_pct=settings.costs.price_limit_pct,
        price_tick=settings.costs.price_tick,
    )
    current_positions: dict[str, int] = {}
    last_close: dict[str, float] = {}
    cash = settings.initial_cash
    trades: list[PortfolioTrade] = []
    portfolio_curve: list[PortfolioPoint] = []
    blocked_orders = 0

    baseline_cash = settings.initial_cash
    baseline_positions: dict[str, int] = {}
    baseline_curve: list[PortfolioPoint] = []
    baseline_trades: list[PortfolioTrade] = []
    baseline_pending = set(normalized)

    for session_index_in_eval, session in enumerate(evaluation_sessions):
        todays_bars = {
            code: bars_by_date[code][session]
            for code in normalized
            if session in bars_by_date[code]
        }
        if session_index_in_eval >= 1 and baseline_pending:
            baseline_cash, baseline_positions = _initialize_baseline(
                normalized,
                todays_bars,
                bars_by_date,
                settings.initial_cash,
                costs,
                baseline_trades,
                baseline_pending,
                baseline_cash,
                baseline_positions,
            )
        scheduled = targets_for_execution.get(session)
        if scheduled is not None:
            signal_date, targets = scheduled
            blocked, cash = _rebalance(
                session,
                signal_date,
                targets,
                normalized,
                bars_by_date,
                todays_bars,
                current_positions,
                last_close,
                costs,
                trades,
                cash,
            )
            blocked_orders += blocked

        for code, bar in todays_bars.items():
            last_close[code] = bar.close
        portfolio_equity = cash + _position_value(
            current_positions, todays_bars, last_close
        )
        baseline_equity = baseline_cash + _position_value(
            baseline_positions, todays_bars, last_close
        )
        portfolio_curve.append(
            PortfolioPoint(
                trade_date=session,
                equity=portfolio_equity,
                cash=cash,
                holdings_count=len(current_positions),
            )
        )
        baseline_curve.append(
            PortfolioPoint(
                trade_date=session,
                equity=baseline_equity,
                cash=baseline_cash,
                holdings_count=len(baseline_positions),
            )
        )

    strategy_metrics = _curve_metrics(
        portfolio_curve,
        settings.initial_cash,
        settings.minimum_sample_count,
        settings.out_of_sample_start,
    )
    if not trades and settings.out_of_sample_start is not None:
        strategy_metrics["evidence_status"] = "no_trades"
    strategy_metrics["turnover"] = _turnover_ratio(
        trades, settings.initial_cash
    )
    baseline_metrics = _curve_metrics(
        baseline_curve,
        settings.initial_cash,
        settings.minimum_sample_count,
        settings.out_of_sample_start,
    )
    if (
        baseline_curve
        and not baseline_positions
        and settings.out_of_sample_start is not None
    ):
        baseline_metrics["evidence_status"] = "no_baseline_positions"
        baseline_metrics["annualized_return"] = None
        baseline_metrics["annualized_volatility"] = None
        baseline_metrics["sharpe"] = None
    baseline_metrics["turnover"] = _turnover_ratio(
        baseline_trades, settings.initial_cash
    )
    return PortfolioSimulation(
        portfolio_curve=tuple(portfolio_curve),
        baseline_curve=tuple(baseline_curve),
        trades=tuple(trades),
        blocked_orders=blocked_orders,
        metrics=strategy_metrics,
        baseline_metrics=baseline_metrics,
    )


def _rebalance(
    session: date,
    signal_date: date,
    targets: tuple[str, ...],
    normalized: Mapping[str, tuple[DailyBar, ...]],
    bars_by_date: Mapping[str, Mapping[date, DailyBar]],
    todays_bars: Mapping[str, DailyBar],
    positions: dict[str, int],
    last_close: Mapping[str, float],
    costs: Costs,
    trades: list[PortfolioTrade],
    cash: float,
) -> tuple[int, float]:
    blocked = 0
    target_set = set(targets)
    prices = {
        code: todays_bars[code].open if code in todays_bars else last_close.get(code, 0.0)
        for code in normalized
    }
    equity_at_open = cash + math.fsum(
        shares * prices[code] for code, shares in positions.items()
    )

    for code in sorted(set(positions) - target_set):
        bar = todays_bars.get(code)
        if bar is None or is_suspended(bar) or _locked_down(
            code, session, bar, bars_by_date, costs
        ):
            blocked += 1
            continue
        cash += _execute(
            session,
            signal_date,
            code,
            "sell",
            positions.pop(code),
            bar.open,
            costs,
            trades,
        )

    if not targets:
        return blocked, cash
    target_value = equity_at_open / len(targets)
    desired: dict[str, int] = {}
    for code in targets:
        bar = todays_bars.get(code)
        if bar is None or is_suspended(bar):
            blocked += 1
            continue
        desired[code] = int(target_value / bar.open / _LOT_SIZE) * _LOT_SIZE

    for code in targets:
        bar = todays_bars.get(code)
        if bar is None or code not in desired:
            continue
        difference = desired[code] - positions.get(code, 0)
        if difference < 0:
            if _locked_down(code, session, bar, bars_by_date, costs):
                blocked += 1
                continue
            shares = -difference
            cash += _execute(
                session, signal_date, code, "sell", shares, bar.open, costs, trades
            )
            positions[code] = positions.get(code, 0) - shares
            if positions[code] == 0:
                positions.pop(code)

    for code in targets:
        bar = todays_bars.get(code)
        if bar is None or code not in desired:
            continue
        difference = desired[code] - positions.get(code, 0)
        if difference <= 0:
            continue
        if _locked_up(code, session, bar, bars_by_date, costs):
            blocked += 1
            continue
        shares = min(difference, _affordable_lots(cash, bar.open, costs))
        if shares <= 0:
            continue
        cash += _execute(
            session, signal_date, code, "buy", shares, bar.open, costs, trades
        )
        positions[code] = positions.get(code, 0) + shares
    return blocked, cash


def _locked_up(
    code: str,
    session: date,
    bar: DailyBar,
    bars_by_date: Mapping[str, Mapping[date, DailyBar]],
    costs: Costs,
) -> bool:
    previous = _previous_bar(code, session, bars_by_date)
    return previous is not None and is_limit_up(
        bar, previous, costs.price_limit_pct, costs.price_tick
    )


def _locked_down(
    code: str,
    session: date,
    bar: DailyBar,
    bars_by_date: Mapping[str, Mapping[date, DailyBar]],
    costs: Costs,
) -> bool:
    previous = _previous_bar(code, session, bars_by_date)
    return previous is not None and is_limit_down(
        bar, previous, costs.price_limit_pct, costs.price_tick
    )


def _previous_bar(
    code: str, session: date, bars_by_date: Mapping[str, Mapping[date, DailyBar]]
) -> DailyBar | None:
    earlier = [day for day in bars_by_date[code] if day < session]
    return bars_by_date[code][max(earlier)] if earlier else None


def _execute(
    session: date,
    signal_date: date | None,
    code: str,
    side: str,
    shares: int,
    raw_price: float,
    costs: Costs,
    trades: list[PortfolioTrade],
) -> float:
    gross = shares * raw_price
    slippage = gross * costs.slippage_rate
    commission = gross * costs.commission_rate
    trades.append(
        PortfolioTrade(
            trade_date=session,
            signal_date=signal_date,
            code=code,
            side=side,
            shares=shares,
            raw_price=raw_price,
            commission=commission,
            slippage=slippage,
        )
    )
    return (
        gross - commission - slippage
        if side == "sell"
        else -(gross + commission + slippage)
    )


def _affordable_lots(cash: float, price: float, costs: Costs) -> int:
    per_share_cost = price * (1 + costs.slippage_rate + costs.commission_rate)
    lots = math.floor(cash / (per_share_cost * _LOT_SIZE))
    return max(0, lots) * _LOT_SIZE


def _initialize_baseline(
    normalized: Mapping[str, tuple[DailyBar, ...]],
    todays_bars: Mapping[str, DailyBar],
    bars_by_date: Mapping[str, Mapping[date, DailyBar]],
    initial_cash: float,
    costs: Costs,
    trades: list[PortfolioTrade],
    pending: set[str],
    cash: float,
    positions: dict[str, int],
) -> tuple[float, dict[str, int]]:
    codes = tuple(sorted(normalized))
    budget = initial_cash / len(codes)
    for code in sorted(pending):
        bar = todays_bars.get(code)
        if (
            bar is None
            or is_suspended(bar)
            or _locked_up(code, bar.trade_date, bar, bars_by_date, costs)
        ):
            continue
        shares = min(
            int(budget / bar.open / _LOT_SIZE) * _LOT_SIZE,
            _affordable_lots(cash, bar.open, costs),
        )
        if shares:
            cash += _execute(
                bar.trade_date,
                None,
                code,
                "buy",
                shares,
                bar.open,
                costs,
                trades,
            )
            positions[code] = shares
            pending.remove(code)
    return cash, positions


def _position_value(
    positions: Mapping[str, int],
    todays_bars: Mapping[str, DailyBar],
    last_close: Mapping[str, float],
) -> float:
    return math.fsum(
        shares
        * (
            todays_bars[code].close
            if code in todays_bars
            else last_close.get(code, 0.0)
        )
        for code, shares in positions.items()
    )


def _curve_metrics(
    curve: Sequence[PortfolioPoint],
    initial_cash: float,
    minimum_sample_count: int,
    out_of_sample_start: date | None,
) -> dict[str, object]:
    if not curve:
        return {
            "sample_count": 0,
            "turnover": 0.0,
            "total_return": 0.0,
            "annualized_return": None,
            "annualized_volatility": None,
            "sharpe": None,
            "maximum_drawdown": 0.0,
            "evidence_status": "no_evaluation_data",
        }
    returns = [curve[0].equity / initial_cash - 1]
    returns.extend(
        current.equity / previous.equity - 1
        for previous, current in pairwise(curve)
        if previous.equity > 0
    )
    peak = initial_cash
    maximum_drawdown = 0.0
    for point in curve:
        peak = max(peak, point.equity)
        if peak > 0:
            maximum_drawdown = min(maximum_drawdown, point.equity / peak - 1)
    total_return = curve[-1].equity / initial_cash - 1
    deviation = pstdev(returns) if len(returns) > 1 else 0.0
    evidence_status = (
        "holdout_not_configured"
        if out_of_sample_start is None
        else (
            "enough_evidence"
            if len(returns) >= minimum_sample_count
            else "not_enough_evidence"
        )
    )
    evidence_sufficient = evidence_status == "enough_evidence"
    return {
        "start_date": curve[0].trade_date.isoformat(),
        "end_date": curve[-1].trade_date.isoformat(),
        "sample_count": len(returns),
        "total_return": total_return,
        "annualized_return": (
            (curve[-1].equity / initial_cash) ** (_ANNUAL_SESSIONS / len(returns))
            - 1
            if evidence_sufficient and curve[-1].equity > 0
            else None
        ),
        "annualized_volatility": (
            deviation * math.sqrt(_ANNUAL_SESSIONS)
            if evidence_sufficient
            else None
        ),
        "sharpe": (
            sum(returns) / len(returns) / deviation * math.sqrt(_ANNUAL_SESSIONS)
            if evidence_sufficient and deviation
            else None
        ),
        "maximum_drawdown": maximum_drawdown,
        "evidence_status": evidence_status,
    }


def _turnover_ratio(trades: Sequence[PortfolioTrade], initial_cash: float) -> float:
    gross_traded = math.fsum(trade.shares * trade.raw_price for trade in trades)
    return gross_traded / initial_cash


def _empty_simulation(
    settings: PortfolioBacktestSettings,
) -> PortfolioSimulation:
    metrics = _curve_metrics(
        (),
        settings.initial_cash,
        settings.minimum_sample_count,
        settings.out_of_sample_start,
    )
    return PortfolioSimulation((), (), (), 0, metrics, dict(metrics))


__all__ = [
    "PortfolioPoint",
    "PortfolioSimulation",
    "PortfolioTrade",
    "simulate_portfolio",
]
