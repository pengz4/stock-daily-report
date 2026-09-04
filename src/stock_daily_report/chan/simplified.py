"""A reproducible, intentionally simplified Chan-style observation layer.

This is not an implementation of canonical/original Chan theory.  The
``simplified-v1`` rules are:

* Adjacent bars include one another when either high/low range contains the
  other, including equality.  An upward merge retains the higher high and
  higher low; a downward merge retains the lower high and lower low.  The
  prior effective-bar direction is used; an initial tie resolves upward.
* A top (bottom) fractal has both high and low strictly above (below) the
  adjacent processed bars.  Its right processed bar confirms it.  It remains
  a candidate if no subsequent supplied daily bar establishes a next-open
  ``tradable_at`` date.
* Confirmed strokes connect alternating confirmed fractals separated by at
  least two processed-bar indices.  They may share only a common endpoint.
* Two latest overlapping strokes form a non-tradable central candidate.  A
  central area is confirmed from three consecutive overlapping strokes and
  is extended only by subsequent overlapping confirmed strokes.
* Support/resistance levels are direct confirmed-fractal extrema (strength 2)
  and confirmed central bounds (strength 3).  Observations are descriptive,
  never buy/sell directives.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date
from itertools import pairwise

from stock_daily_report.models import DailyBar

from .common import (
    RULE_VERSION,
    CentralArea,
    Direction,
    Fractal,
    Observation,
    ProcessedBar,
    SimplifiedChanResult,
    Status,
    Stroke,
    StructureState,
    SupportResistance,
)

MINIMUM_STROKE_SEPARATION = 2


class SimplifiedChanAnalyzer:
    """Analyze copied, chronological ``DailyBar`` inputs using simplified-v1."""

    def analyze(self, bars: list[DailyBar]) -> SimplifiedChanResult:
        copied_bars = self._validate_and_copy(bars)
        processed = self._process_inclusion(copied_bars)
        fractals = self._find_fractals(processed, copied_bars)
        strokes = self._find_strokes(fractals)
        central_candidates = self._find_central_candidates(strokes)
        central_areas = self._find_central_areas(strokes)
        levels = self._find_support_resistance(fractals, central_areas)
        observations = self._find_observations(copied_bars, central_areas)
        state = self._state(fractals, strokes, central_candidates, central_areas)
        return SimplifiedChanResult(
            processed_bars=tuple(processed),
            fractals=tuple(fractals),
            strokes=tuple(strokes),
            central_candidates=tuple(central_candidates),
            central_areas=tuple(central_areas),
            support_resistance=tuple(levels),
            observations=tuple(observations),
            state=state,
        )

    @staticmethod
    def _validate_and_copy(bars: list[DailyBar]) -> list[DailyBar]:
        if any(not isinstance(bar, DailyBar) for bar in bars):
            raise TypeError("bars must contain valid DailyBar instances")
        if any(
            earlier.trade_date >= later.trade_date
            for earlier, later in pairwise(bars)
        ):
            raise ValueError("DailyBar trade_date values must be strictly increasing")
        return [bar.model_copy(deep=True) for bar in bars]

    @classmethod
    def _process_inclusion(cls, bars: list[DailyBar]) -> list[ProcessedBar]:
        processed: list[ProcessedBar] = []
        for source_index, bar in enumerate(bars):
            incoming = ProcessedBar(
                high=bar.high,
                low=bar.low,
                direction="neutral",
                source_indices=(source_index,),
                source_dates=(bar.trade_date,),
            )
            if not processed:
                processed.append(incoming)
                continue

            previous = processed[-1]
            if not cls._includes(previous, incoming):
                processed.append(
                    replace(incoming, direction=cls._direction(previous, incoming))
                )
                continue

            direction = previous.direction
            if direction == "neutral":
                direction = cls._direction(previous, incoming)
            if direction == "neutral":
                direction = "up"
            if direction == "up":
                high, low = max(previous.high, incoming.high), max(
                    previous.low, incoming.low
                )
            else:
                high, low = min(previous.high, incoming.high), min(
                    previous.low, incoming.low
                )
            processed[-1] = ProcessedBar(
                high=high,
                low=low,
                direction=direction,
                source_indices=previous.source_indices + incoming.source_indices,
                source_dates=previous.source_dates + incoming.source_dates,
            )
        return processed

    @staticmethod
    def _includes(left: ProcessedBar, right: ProcessedBar) -> bool:
        return (left.high >= right.high and left.low <= right.low) or (
            right.high >= left.high and right.low <= left.low
        )

    @staticmethod
    def _direction(left: ProcessedBar, right: ProcessedBar) -> Direction:
        if right.high > left.high and right.low >= left.low:
            return "up"
        if right.high <= left.high and right.low < left.low:
            return "down"
        return "neutral"

    @staticmethod
    def _trade_date_after(
        source_index: int, bars: list[DailyBar]
    ) -> date | None:
        next_index = source_index + 1
        return bars[next_index].trade_date if next_index < len(bars) else None

    @classmethod
    def _find_fractals(
        cls, processed: list[ProcessedBar], bars: list[DailyBar]
    ) -> list[Fractal]:
        fractals: list[Fractal] = []
        for index in range(1, len(processed) - 1):
            left, center, right = processed[index - 1 : index + 2]
            kind: str | None = None
            price = 0.0
            if center.high > left.high and center.high > right.high and (
                center.low > left.low and center.low > right.low
            ):
                kind, price = "top", center.high
            elif center.high < left.high and center.high < right.high and (
                center.low < left.low and center.low < right.low
            ):
                kind, price = "bottom", center.low
            if kind is None:
                continue

            confirmed_at = right.source_dates[-1]
            tradable_at = cls._trade_date_after(right.source_indices[-1], bars)
            status: Status = "confirmed" if tradable_at is not None else "candidate"
            fractals.append(
                Fractal(
                    kind=kind,  # type: ignore[arg-type]
                    price=price,
                    formed_at=center.source_dates[-1],
                    confirmed_at=confirmed_at if status == "confirmed" else None,
                    tradable_at=tradable_at,
                    status=status,
                    rule_version=RULE_VERSION,
                    processed_index=index,
                    source_indices=center.source_indices,
                )
            )
        return fractals

    @classmethod
    def _find_strokes(cls, fractals: list[Fractal]) -> list[Stroke]:
        confirmed = [fractal for fractal in fractals if fractal.status == "confirmed"]
        strokes: list[Stroke] = []
        active: Fractal | None = None
        for fractal in confirmed:
            if active is None:
                active = fractal
                continue
            if fractal.kind == active.kind:
                if cls._is_more_extreme(fractal, active):
                    active = fractal
                continue
            if fractal.processed_index - active.processed_index < MINIMUM_STROKE_SEPARATION:
                continue
            strokes.append(
                Stroke(
                    kind="upward" if fractal.kind == "top" else "downward",
                    low=min(active.price, fractal.price),
                    high=max(active.price, fractal.price),
                    formed_at=active.formed_at,
                    confirmed_at=fractal.confirmed_at,
                    tradable_at=fractal.tradable_at,
                    status="confirmed",
                    rule_version=RULE_VERSION,
                    start_formed_at=active.formed_at,
                    end_formed_at=fractal.formed_at,
                    start_processed_index=active.processed_index,
                    end_processed_index=fractal.processed_index,
                )
            )
            active = fractal
        return strokes

    @staticmethod
    def _is_more_extreme(new: Fractal, old: Fractal) -> bool:
        return new.price > old.price if new.kind == "top" else new.price < old.price

    @staticmethod
    def _overlap(strokes: list[Stroke]) -> tuple[float, float] | None:
        low = max(stroke.low for stroke in strokes)
        high = min(stroke.high for stroke in strokes)
        return (low, high) if low <= high else None

    @classmethod
    def _find_central_candidates(cls, strokes: list[Stroke]) -> list[CentralArea]:
        if len(strokes) < 2:
            return []
        pair = strokes[-2:]
        overlap = cls._overlap(pair)
        if overlap is None:
            return []
        return [
            CentralArea(
                low=overlap[0],
                high=overlap[1],
                formed_at=pair[-1].end_formed_at,
                confirmed_at=None,
                tradable_at=None,
                status="candidate",
                rule_version=RULE_VERSION,
                start_stroke_index=len(strokes) - 2,
                end_stroke_index=len(strokes) - 1,
            )
        ]

    @classmethod
    def _find_central_areas(cls, strokes: list[Stroke]) -> list[CentralArea]:
        areas: list[CentralArea] = []
        start_index = 0
        while start_index <= len(strokes) - 3:
            initial = strokes[start_index : start_index + 3]
            overlap = cls._overlap(initial)
            if overlap is None:
                start_index += 1
                continue
            end_index = start_index + 2
            while end_index + 1 < len(strokes):
                extended = cls._overlap(initial + [strokes[end_index + 1]])
                if extended is None:
                    break
                overlap = extended
                initial.append(strokes[end_index + 1])
                end_index += 1
            last = strokes[end_index]
            areas.append(
                CentralArea(
                    low=overlap[0],
                    high=overlap[1],
                    formed_at=strokes[start_index + 2].end_formed_at,
                    confirmed_at=last.confirmed_at,
                    tradable_at=last.tradable_at,
                    status="confirmed",
                    rule_version=RULE_VERSION,
                    start_stroke_index=start_index,
                    end_stroke_index=end_index,
                )
            )
            start_index = end_index + 1
        return areas

    @staticmethod
    def _find_support_resistance(
        fractals: list[Fractal], central_areas: list[CentralArea]
    ) -> list[SupportResistance]:
        confirmed = [fractal for fractal in fractals if fractal.status == "confirmed"]
        supports = [
            SupportResistance(
                kind="support",
                price=area.low,
                source="confirmed_central_range",
                strength=3,
                formed_at=area.formed_at,
                confirmed_at=area.confirmed_at,
                tradable_at=area.tradable_at,
                status="confirmed",
                rule_version=RULE_VERSION,
            )
            for area in central_areas
        ]
        supports.extend(
            SupportResistance(
                kind="support",
                price=fractal.price,
                source="confirmed_bottom_fractal",
                strength=2,
                formed_at=fractal.formed_at,
                confirmed_at=fractal.confirmed_at,
                tradable_at=fractal.tradable_at,
                status="confirmed",
                rule_version=RULE_VERSION,
            )
            for fractal in confirmed
            if fractal.kind == "bottom"
        )
        resistances = [
            SupportResistance(
                kind="resistance",
                price=area.high,
                source="confirmed_central_range",
                strength=3,
                formed_at=area.formed_at,
                confirmed_at=area.confirmed_at,
                tradable_at=area.tradable_at,
                status="confirmed",
                rule_version=RULE_VERSION,
            )
            for area in central_areas
        ]
        resistances.extend(
            SupportResistance(
                kind="resistance",
                price=fractal.price,
                source="confirmed_top_fractal",
                strength=2,
                formed_at=fractal.formed_at,
                confirmed_at=fractal.confirmed_at,
                tradable_at=fractal.tradable_at,
                status="confirmed",
                rule_version=RULE_VERSION,
            )
            for fractal in confirmed
            if fractal.kind == "top"
        )
        return [*supports, *resistances]

    @staticmethod
    def _find_observations(
        bars: list[DailyBar], central_areas: list[CentralArea]
    ) -> list[Observation]:
        if not bars or not central_areas:
            return []
        latest = central_areas[-1]
        if bars[-1].close <= latest.high:
            return []
        return [
            Observation(
                code="potential_central_breakout",
                formed_at=bars[-1].trade_date,
                confirmed_at=None,
                tradable_at=None,
                status="candidate",
                rule_version=RULE_VERSION,
            )
        ]

    @staticmethod
    def _state(
        fractals: list[Fractal],
        strokes: list[Stroke],
        central_candidates: list[CentralArea],
        central_areas: list[CentralArea],
    ) -> StructureState:
        if central_areas:
            latest = central_areas[-1]
            return StructureState(
                label="neutral_consolidation",
                formed_at=latest.formed_at,
                confirmed_at=latest.confirmed_at,
                tradable_at=latest.tradable_at,
                status="confirmed",
                rule_version=RULE_VERSION,
            )
        if central_candidates:
            latest = central_candidates[-1]
            return StructureState(
                label="neutral_consolidation",
                formed_at=latest.formed_at,
                confirmed_at=None,
                tradable_at=None,
                status="candidate",
                rule_version=RULE_VERSION,
            )
        candidates = [
            fractal for fractal in fractals if fractal.status == "candidate"
        ]
        if candidates:
            latest = candidates[-1]
            return StructureState(
                label="candidate_upward"
                if latest.kind == "bottom"
                else "candidate_downward",
                formed_at=latest.formed_at,
                confirmed_at=None,
                tradable_at=None,
                status="candidate",
                rule_version=RULE_VERSION,
            )
        if strokes:
            latest = strokes[-1]
            return StructureState(
                label=f"confirmed_{latest.kind.removesuffix('ward')}",  # type: ignore[arg-type]
                formed_at=latest.formed_at,
                confirmed_at=latest.confirmed_at,
                tradable_at=latest.tradable_at,
                status="confirmed",
                rule_version=RULE_VERSION,
            )
        return StructureState(
            label="not_enough_data",
            formed_at=None,
            confirmed_at=None,
            tradable_at=None,
            status="candidate",
            rule_version=RULE_VERSION,
        )
