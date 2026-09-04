"""Tolerance-aware structural fidelity metrics."""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from numbers import Real

from .replay import ReplayEvent


@dataclass(frozen=True)
class StructureMetrics:
    """Comparison metrics for one candidate/reference event population."""

    matched: int
    candidate_count: int
    reference_count: int
    precision: float
    recall: float
    f1: float
    confirmation_lags: tuple[int, ...]
    confirmed_count: int
    rewritten_confirmed_count: int
    rewrite_rate: float

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-compatible representation."""

        return {
            "matched": self.matched,
            "candidate_count": self.candidate_count,
            "reference_count": self.reference_count,
            "precision": self.precision,
            "recall": self.recall,
            "f1": self.f1,
            "confirmation_lags": list(self.confirmation_lags),
            "confirmed_count": self.confirmed_count,
            "rewritten_confirmed_count": self.rewritten_confirmed_count,
            "rewrite_rate": self.rewrite_rate,
        }


def compare_structures(
    reference: Sequence[ReplayEvent],
    candidate: Sequence[ReplayEvent],
    *,
    price_tolerance: float = 0.0,
    time_tolerance_days: int = 0,
) -> StructureMetrics:
    """Compare latest visible events with one-to-one time/price matching."""

    if (
        isinstance(price_tolerance, bool)
        or not isinstance(price_tolerance, Real)
        or not math.isfinite(price_tolerance)
        or price_tolerance < 0
    ):
        raise ValueError("price_tolerance must be finite and non-negative")
    if (
        isinstance(time_tolerance_days, bool)
        or not isinstance(time_tolerance_days, int)
        or time_tolerance_days < 0
    ):
        raise ValueError("time_tolerance_days must be a non-negative integer")
    reference_latest = _latest(reference)
    candidate_latest = _latest(candidate)
    unmatched = set(reference_latest)
    matched = 0
    lags: list[int] = []
    matching = _maximum_matching(
        reference_latest,
        candidate_latest,
        price_tolerance,
        time_tolerance_days,
    )
    for candidate_id, reference_id in matching.items():
        unmatched.remove(reference_id)
        matched += 1
        reference_event = reference_latest[reference_id]
        event = candidate_latest[candidate_id]
        if reference_event.confirmed_at and event.confirmed_at:
            lags.append(
                (event.confirmed_at - reference_event.confirmed_at).days
            )
    candidate_count = len(candidate_latest)
    reference_count = len(reference_latest)
    precision = matched / candidate_count if candidate_count else 0.0
    recall = matched / reference_count if reference_count else 0.0
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision + recall
        else 0.0
    )
    structural_candidate = [
        event
        for event in candidate
        if event.kind in {"fractal", "stroke", "central_area"}
    ]
    confirmed_fingerprints: dict[str, tuple[object, ...]] = {}
    rewritten_ids: set[str] = set()
    for event in structural_candidate:
        if event.status == "confirmed":
            first_confirmed = confirmed_fingerprints.get(event.event_id)
            if (
                first_confirmed is not None
                and _confirmed_fingerprint(event) != first_confirmed
            ):
                rewritten_ids.add(event.event_id)
            elif first_confirmed is None:
                confirmed_fingerprints[event.event_id] = _confirmed_fingerprint(event)
    confirmed_count = len(confirmed_fingerprints)
    rewritten_count = len(rewritten_ids)
    return StructureMetrics(
        matched=matched,
        candidate_count=candidate_count,
        reference_count=reference_count,
        precision=precision,
        recall=recall,
        f1=f1,
        confirmation_lags=tuple(sorted(lags)),
        confirmed_count=confirmed_count,
        rewritten_confirmed_count=rewritten_count,
        rewrite_rate=(
            rewritten_count / confirmed_count if confirmed_count else 0.0
        ),
    )


def _latest(events: Sequence[ReplayEvent]) -> dict[str, ReplayEvent]:
    events = [
        event
        for event in events
        if event.kind in {"fractal", "stroke", "central_area"}
    ]
    if events:
        latest_observation = max(event.observed_at for event in events)
        events = [
            event for event in events if event.observed_at == latest_observation
        ]
    latest: dict[str, ReplayEvent] = {}
    for event in events:
        previous = latest.get(event.event_id)
        if previous is None or event.observed_at >= previous.observed_at:
            latest[event.event_id] = event
    return latest


def _maximum_matching(
    reference: dict[str, ReplayEvent],
    candidate: dict[str, ReplayEvent],
    price_tolerance: float,
    time_tolerance_days: int,
) -> dict[str, str]:
    """Find a deterministic maximum-cardinality event matching."""

    adjacency = {
        candidate_id: sorted(
            (
                reference_id
                for reference_id, reference_event in reference.items()
                if _matches(
                    reference_event,
                    candidate_event,
                    price_tolerance,
                    time_tolerance_days,
                )
            ),
            key=lambda reference_id: (
                abs(
                    (
                        reference[reference_id].formed_at
                        - candidate_event.formed_at
                    ).days
                ),
                _distance(reference[reference_id], candidate_event),
                reference_id,
            ),
        )
        for candidate_id, candidate_event in candidate.items()
    }
    assigned: dict[str, str] = {}

    def assign(candidate_id: str, visited: set[str]) -> bool:
        for reference_id in adjacency[candidate_id]:
            if reference_id in visited:
                continue
            visited.add(reference_id)
            previous = next(
                (
                    other_candidate
                    for other_candidate, assigned_reference in assigned.items()
                    if assigned_reference == reference_id
                ),
                None,
            )
            if previous is None or assign(previous, visited):
                assigned[candidate_id] = reference_id
                return True
        return False

    for candidate_id in sorted(candidate):
        assign(candidate_id, set())
    return assigned


def _distance(reference: ReplayEvent, candidate: ReplayEvent) -> float:
    values = [
        abs(left - right)
        for left, right in (
            (reference.price, candidate.price),
            (reference.low, candidate.low),
            (reference.high, candidate.high),
        )
        if left is not None and right is not None
    ]
    return max(values, default=0.0)


def _matches(
    reference: ReplayEvent,
    candidate: ReplayEvent,
    price_tolerance: float,
    time_tolerance_days: int,
) -> bool:
    if reference.kind != candidate.kind:
        return False
    reference_subtype = _normalized_subtype(reference)
    candidate_subtype = _normalized_subtype(candidate)
    if (
        reference_subtype is not None or candidate_subtype is not None
    ) and reference_subtype != candidate_subtype:
        return False
    if abs((reference.formed_at - candidate.formed_at).days) > time_tolerance_days:
        return False
    for left, right in (
        (reference.price, candidate.price),
        (reference.low, candidate.low),
        (reference.high, candidate.high),
    ):
        if left is not None and right is not None:
            if abs(left - right) > price_tolerance:
                return False
        elif left != right:
            return False
    return True


def _normalized_subtype(event: ReplayEvent) -> str | None:
    if event.subtype is not None:
        return event.subtype
    reason = event.reason_code.lower()
    if event.kind == "fractal":
        if "top" in reason:
            return "top"
        if "bottom" in reason:
            return "bottom"
    if event.kind == "stroke":
        if "up" in reason:
            return "up"
        if "down" in reason:
            return "down"
    return None


def _confirmed_fingerprint(event: ReplayEvent) -> tuple[object, ...]:
    """Exclude normal post-confirmation tradeability enrichment."""

    return (
        event.kind,
        event.formed_at,
        event.confirmed_at,
        event.status,
        event.reason_code,
        event.subtype,
        event.price,
        event.low,
        event.high,
    )


__all__ = ["StructureMetrics", "compare_structures"]
