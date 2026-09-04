"""Shared immutable records for simplified structural observations.

All dates are daily-bar dates.  ``tradable_at`` is the next supplied daily
bar's date after the confirming bar; it is deliberately absent for candidates.
The next date establishes timing only and is never used to calculate a
structure's price, direction, or confirmation.
"""

from dataclasses import dataclass
from datetime import date
from typing import Literal

RULE_VERSION = "simplified-v1"
Status = Literal["candidate", "confirmed"]
Direction = Literal["up", "down", "neutral"]


@dataclass(frozen=True)
class ProcessedBar:
    """An inclusion-processed bar with its original input-bar trace."""

    high: float
    low: float
    direction: Direction
    source_indices: tuple[int, ...]
    source_dates: tuple[date, ...]


@dataclass(frozen=True)
class Fractal:
    kind: Literal["top", "bottom"]
    price: float
    formed_at: date
    confirmed_at: date | None
    tradable_at: date | None
    status: Status
    rule_version: str
    processed_index: int
    source_indices: tuple[int, ...]


@dataclass(frozen=True)
class Stroke:
    kind: Literal["upward", "downward"]
    low: float
    high: float
    formed_at: date
    confirmed_at: date | None
    tradable_at: date | None
    status: Status
    rule_version: str
    start_formed_at: date
    end_formed_at: date
    start_processed_index: int
    end_processed_index: int


@dataclass(frozen=True)
class CentralArea:
    low: float
    high: float
    formed_at: date
    confirmed_at: date | None
    tradable_at: date | None
    status: Status
    rule_version: str
    start_stroke_index: int
    end_stroke_index: int


@dataclass(frozen=True)
class SupportResistance:
    kind: Literal["support", "resistance"]
    price: float
    source: str
    strength: int
    formed_at: date
    confirmed_at: date | None
    tradable_at: date | None
    status: Status
    rule_version: str


@dataclass(frozen=True)
class Observation:
    code: Literal[
        "potential_central_breakout", "pullback_holds_above_central_range"
    ]
    formed_at: date
    confirmed_at: date | None
    tradable_at: date | None
    status: Status
    rule_version: str


@dataclass(frozen=True)
class StructureState:
    label: Literal[
        "not_enough_data",
        "candidate_upward",
        "candidate_downward",
        "confirmed_upward",
        "confirmed_downward",
        "neutral_consolidation",
    ]
    formed_at: date | None
    confirmed_at: date | None
    tradable_at: date | None
    status: Status
    rule_version: str


@dataclass(frozen=True)
class SimplifiedChanResult:
    processed_bars: tuple[ProcessedBar, ...]
    fractals: tuple[Fractal, ...]
    strokes: tuple[Stroke, ...]
    central_candidates: tuple[CentralArea, ...]
    central_areas: tuple[CentralArea, ...]
    support_resistance: tuple[SupportResistance, ...]
    observations: tuple[Observation, ...]
    state: StructureState

    @property
    def all_items(self) -> tuple[object, ...]:
        """Return auditable structural records, excluding processing trace."""

        return (
            *self.fractals,
            *self.strokes,
            *self.central_candidates,
            *self.central_areas,
            *self.support_resistance,
            *self.observations,
            self.state,
        )
