"""Prefix replay for auditable structural analyzer comparisons."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, replace
from datetime import date
from itertools import pairwise
from typing import Literal, Protocol

from stock_daily_report.models import DailyBar

ReplayStatus = Literal["candidate", "confirmed"]
SIMPLIFIED_BREAKOUT_STRATEGY_VERSION = "simplified-breakout-v1"


class Analyzer(Protocol):
    """Structural analyzer interface required by the replay engine."""

    def analyze(self, bars: list[DailyBar]) -> object:
        """Analyze one chronological bar prefix."""


@dataclass(frozen=True)
class ReplayEvent:
    """One event visible after analyzing a single bar prefix."""

    analyzer: str
    event_id: str
    kind: str
    observed_at: date
    max_input_date: date
    formed_at: date
    confirmed_at: date | None
    tradable_at: date | None
    status: ReplayStatus
    reason_code: str
    price: float | None
    low: float | None
    high: float | None
    revision: int
    first_observed_at: date
    subtype: str | None = None
    symbol: str | None = None

    def fingerprint(self) -> tuple[object, ...]:
        """Return fields whose changes constitute an event revision."""

        return (
            self.kind,
            self.formed_at,
            self.confirmed_at,
            self.tradable_at,
            self.status,
            self.reason_code,
            self.subtype,
            self.price,
            self.low,
            self.high,
        )

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""

        return {
            "analyzer": self.analyzer,
            "event_id": self.event_id,
            "kind": self.kind,
            "observed_at": self.observed_at.isoformat(),
            "max_input_date": self.max_input_date.isoformat(),
            "formed_at": self.formed_at.isoformat(),
            "confirmed_at": (
                self.confirmed_at.isoformat() if self.confirmed_at else None
            ),
            "tradable_at": (
                self.tradable_at.isoformat() if self.tradable_at else None
            ),
            "status": self.status,
            "reason_code": self.reason_code,
            "price": self.price,
            "low": self.low,
            "high": self.high,
            "revision": self.revision,
            "first_observed_at": self.first_observed_at.isoformat(),
            "subtype": self.subtype,
            "symbol": self.symbol,
        }


def replay(bars: list[DailyBar], analyzer: Analyzer) -> tuple[ReplayEvent, ...]:
    """Replay an analyzer against each available non-empty bar prefix.

    The analyzer is rerun from scratch for every prefix. This deliberately
    favors auditability over speed and makes every emitted event attributable
    to the exact input date that was available at observation time.
    """

    if any(
        earlier.trade_date >= later.trade_date
        for earlier, later in pairwise(bars)
    ):
        raise ValueError("DailyBar trade_date values must be strictly increasing")
    history: dict[str, tuple[tuple[object, ...], int, date]] = {}
    events: list[ReplayEvent] = []
    for end in range(1, len(bars) + 1):
        prefix = bars[:end]
        observed_at = prefix[-1].trade_date
        result = analyzer.analyze(prefix)
        analyzer_name = _analyzer_name(result)
        visible = _coalesce_visible(
            _as_replay_event(item, observed_at, analyzer_name)
            for item in _structural_items(result)
        )
        for event in visible:
            previous = history.get(event.event_id)
            if previous is None:
                revision = 0
                first_observed_at = observed_at
            else:
                previous_fingerprint, previous_revision, first_observed_at = previous
                revision = previous_revision + (
                    previous_fingerprint != event.fingerprint()
                )
            event = ReplayEvent(
                **{
                    **event.__dict__,
                    "revision": revision,
                    "first_observed_at": first_observed_at,
                }
            )
            history[event.event_id] = (
                event.fingerprint(),
                event.revision,
                event.first_observed_at,
            )
            events.append(event)
    return tuple(events)


def _structural_items(result: object) -> tuple[object, ...]:
    items = getattr(result, "all_items", ())
    return tuple(item for item in items if _kind_for(item) is not None)


def _kind_for(item: object) -> str | None:
    class_name = type(item).__name__
    if class_name == "StrictEvent":
        return getattr(item, "kind", None)
    return {
        "Fractal": "fractal",
        "Stroke": "stroke",
        "CentralArea": "central_area",
        "Observation": "observation",
    }.get(class_name)


def _as_replay_event(
    item: object, observed_at: date, analyzer_name: str | None = None
) -> ReplayEvent:
    kind = _kind_for(item)
    if kind is None:
        raise TypeError(f"unsupported structural item: {type(item).__name__}")
    analyzer = analyzer_name or getattr(item, "rule_version", None) or "strict-v1"
    reason_code = getattr(item, "reason_code", None)
    if reason_code is None:
        reason_code = (
            getattr(item, "code", None)
            or f"{kind}_{getattr(item, 'kind', kind)}"
        )
    formed_at = getattr(item, "formed_at", None)
    if not isinstance(formed_at, date):
        raise TypeError("structural items must provide a date formed_at")
    subtype = _subtype_for(item, kind, reason_code)
    event_id = f"{kind}:{formed_at.isoformat()}:{reason_code}:{subtype or ''}"
    return ReplayEvent(
        analyzer=analyzer,
        event_id=event_id,
        kind=kind,
        observed_at=observed_at,
        max_input_date=observed_at,
        formed_at=formed_at,
        confirmed_at=getattr(item, "confirmed_at", None),
        tradable_at=getattr(item, "tradable_at", None),
        status=getattr(item, "status", "candidate"),
        reason_code=reason_code,
        price=getattr(item, "price", None),
        low=getattr(item, "low", None),
        high=getattr(item, "high", None),
        revision=0,
        first_observed_at=observed_at,
        subtype=subtype,
    )


def _analyzer_name(result: object) -> str | None:
    result_name = getattr(result, "rule_version", None)
    if isinstance(result_name, str) and result_name:
        return result_name
    for item in _structural_items(result):
        item_name = getattr(item, "rule_version", None)
        if isinstance(item_name, str) and item_name:
            return item_name
    return None


def execution_signals(
    events: list[ReplayEvent] | tuple[ReplayEvent, ...],
) -> tuple[ReplayEvent, ...]:
    """Apply the explicitly versioned execution adapter to structural events."""

    signals: list[ReplayEvent] = []
    for event in events:
        if event.kind == "signal":
            signals.append(event)
        elif (
            event.kind == "observation"
            and event.reason_code == "potential_central_breakout"
        ):
            signals.append(
                replace(
                    event,
                    kind="signal",
                    reason_code="simplified_breakout_up",
                )
            )
    return tuple(signals)


def _coalesce_visible(events: Iterable[ReplayEvent]) -> tuple[ReplayEvent, ...]:
    coalesced: dict[str, ReplayEvent] = {}
    for event in events:
        if not isinstance(event, ReplayEvent):
            raise TypeError("replay structural items must be ReplayEvent instances")
        previous = coalesced.get(event.event_id)
        if previous is None:
            coalesced[event.event_id] = event
            continue
        if previous.fingerprint() == event.fingerprint():
            continue
        if previous.status == event.status:
            raise ValueError(f"ambiguous structural event identity: {event.event_id}")
        if event.status == "confirmed":
            coalesced[event.event_id] = event
    return tuple(coalesced.values())


def _subtype_for(item: object, kind: str, reason_code: str) -> str | None:
    explicit = getattr(item, "direction", None)
    if isinstance(explicit, str):
        return explicit
    item_kind = getattr(item, "kind", None)
    if kind == "fractal":
        if "top" in reason_code or item_kind == "top":
            return "top"
        if "bottom" in reason_code or item_kind == "bottom":
            return "bottom"
    if (
        kind == "stroke"
        and isinstance(item_kind, str)
        and item_kind in {"upward", "downward"}
    ):
        return item_kind.removesuffix("ward")
    return None


__all__ = [
    "SIMPLIFIED_BREAKOUT_STRATEGY_VERSION",
    "Analyzer",
    "ReplayEvent",
    "execution_signals",
    "replay",
]
