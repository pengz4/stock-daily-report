"""Versioned strict structural baseline for reproducible Chan comparisons."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from importlib import resources
from itertools import pairwise
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from stock_daily_report.models import DailyBar

StrictStatus = Literal["candidate", "confirmed"]


class StrictProfile(BaseModel):
    """Explicit, versioned rules for the strict benchmark analyzer."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rule_version: str = Field(pattern=r"^strict-v\d+$")
    minimum_stroke_separation: int = Field(ge=3)
    minimum_central_strokes: int = Field(ge=3)
    require_next_bar_for_tradeability: bool = True

    @model_validator(mode="after")
    def enforce_v1_rules(self) -> StrictProfile:
        expected = (3, 3, True)
        actual = (
            self.minimum_stroke_separation,
            self.minimum_central_strokes,
            self.require_next_bar_for_tradeability,
        )
        if self.rule_version == "strict-v1" and actual != expected:
            raise ValueError("strict-v1 rules are fixed and cannot be overridden")
        return self


class StrictProfileError(ValueError):
    """Raised when the strict rule profile cannot be loaded."""


def load_strict_profile(path: str | Path | None = None) -> StrictProfile:
    """Load the checked-in strict profile with safe YAML parsing."""

    try:
        if path is None:
            profile_text = resources.files(__package__).joinpath(
                "strict_chan_rules.yaml"
            ).read_text(encoding="utf-8")
        else:
            profile_text = Path(path).read_text(encoding="utf-8")
        document = yaml.safe_load(profile_text)
    except (OSError, UnicodeDecodeError, yaml.YAMLError) as error:
        location = path or "packaged strict_chan_rules.yaml"
        raise StrictProfileError(f"Could not read strict profile: {location}") from error
    if not isinstance(document, dict):
        raise StrictProfileError("strict profile must be a YAML mapping")
    try:
        return StrictProfile.model_validate(document)
    except ValidationError as error:
        raise StrictProfileError(f"Invalid strict profile: {error}") from error


@dataclass(frozen=True)
class StrictEvent:
    """One auditable strict structural event."""

    kind: Literal["fractal", "stroke", "central_area", "segment", "signal"]
    formed_at: date
    confirmed_at: date | None
    tradable_at: date | None
    status: StrictStatus
    reason_code: str
    price: float | None = None
    low: float | None = None
    high: float | None = None


@dataclass(frozen=True)
class StrictChanResult:
    """Strict structural observations and their processing trace."""

    rule_version: str
    processed_bars: tuple[_ProcessedBar, ...]
    fractals: tuple[StrictEvent, ...]
    strokes: tuple[StrictEvent, ...]
    central_candidates: tuple[StrictEvent, ...]
    central_areas: tuple[StrictEvent, ...]
    segments: tuple[StrictEvent, ...]
    signals: tuple[StrictEvent, ...]

    @property
    def all_items(self) -> tuple[StrictEvent, ...]:
        return (
            *self.fractals,
            *self.strokes,
            *self.central_candidates,
            *self.central_areas,
            *self.segments,
            *self.signals,
        )


@dataclass(frozen=True)
class _ProcessedBar:
    high: float
    low: float
    direction: Literal["up", "down", "neutral"]
    source_indices: tuple[int, ...]
    source_dates: tuple[date, ...]


@dataclass(frozen=True)
class _FractalCandidate:
    event: StrictEvent
    processed_index: int


@dataclass(frozen=True)
class _StrokeCandidate:
    event: StrictEvent
    start_index: int
    end_index: int
    start_price: float
    end_price: float


class StrictChanAnalyzer:
    """Analyze a copied bar prefix using the strict-v1 profile."""

    def __init__(self, profile: StrictProfile | None = None) -> None:
        loaded = profile or load_strict_profile()
        self.profile = StrictProfile.model_validate(loaded.model_dump())

    def analyze(self, bars: list[DailyBar]) -> StrictChanResult:
        copied = self._validate_and_copy(bars)
        processed = self._process_inclusion(copied)
        fractal_candidates = self._find_fractals(processed, copied)
        stroke_candidates = self._find_strokes(fractal_candidates)
        central_candidates, central_areas = self._find_central_areas(
            stroke_candidates
        )
        segments = self._find_segments(stroke_candidates)
        signals = self._find_signals(copied, central_areas)
        return StrictChanResult(
            rule_version=self.profile.rule_version,
            processed_bars=tuple(processed),
            fractals=tuple(item.event for item in fractal_candidates),
            strokes=tuple(item.event for item in stroke_candidates),
            central_candidates=tuple(central_candidates),
            central_areas=tuple(central_areas),
            segments=tuple(segments),
            signals=tuple(signals),
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

    @staticmethod
    def _includes(left: _ProcessedBar, right: _ProcessedBar) -> bool:
        return (left.high >= right.high and left.low <= right.low) or (
            right.high >= left.high and right.low <= left.low
        )

    @staticmethod
    def _direction(
        left: _ProcessedBar, right: _ProcessedBar
    ) -> Literal["up", "down", "neutral"]:
        if right.high > left.high and right.low >= left.low:
            return "up"
        if right.high <= left.high and right.low < left.low:
            return "down"
        return "neutral"

    @classmethod
    def _process_inclusion(cls, bars: list[DailyBar]) -> list[_ProcessedBar]:
        processed: list[_ProcessedBar] = []
        for source_index, bar in enumerate(bars):
            incoming = _ProcessedBar(
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
                direction = cls._direction(previous, incoming)
                processed.append(
                    _ProcessedBar(
                        **{**incoming.__dict__, "direction": direction}
                    )
                )
                continue
            direction = previous.direction
            if direction == "neutral":
                direction = cls._direction(previous, incoming)
            if direction == "neutral":
                direction = "up"
            high, low = (
                (max(previous.high, incoming.high), max(previous.low, incoming.low))
                if direction == "up"
                else (min(previous.high, incoming.high), min(previous.low, incoming.low))
            )
            processed[-1] = _ProcessedBar(
                high=high,
                low=low,
                direction=direction,
                source_indices=previous.source_indices + incoming.source_indices,
                source_dates=previous.source_dates + incoming.source_dates,
            )
        return processed

    def _find_fractals(
        self, processed: list[_ProcessedBar], bars: list[DailyBar]
    ) -> list[_FractalCandidate]:
        result: list[_FractalCandidate] = []
        for index in range(1, len(processed) - 1):
            left, center, right = processed[index - 1 : index + 2]
            if center.high > left.high and center.high > right.high and (
                center.low > left.low and center.low > right.low
            ):
                price = center.high
                reason = "strict_top_fractal"
            elif center.high < left.high and center.high < right.high and (
                center.low < left.low and center.low < right.low
            ):
                price = center.low
                reason = "strict_bottom_fractal"
            else:
                continue
            confirmed_at = right.source_dates[0]
            right_source_index = right.source_indices[0]
            tradable_at = (
                bars[right_source_index + 1].trade_date
                if right_source_index + 1 < len(bars)
                else None
            )
            if not self.profile.require_next_bar_for_tradeability:
                tradable_at = confirmed_at
            status: StrictStatus = "confirmed"
            result.append(
                _FractalCandidate(
                    event=StrictEvent(
                        kind="fractal",
                        formed_at=center.source_dates[-1],
                        confirmed_at=confirmed_at,
                        tradable_at=tradable_at,
                        status=status,
                        reason_code=reason,
                        price=price,
                    ),
                    processed_index=index,
                )
            )
        return result

    def _find_strokes(
        self, fractals: list[_FractalCandidate]
    ) -> list[_StrokeCandidate]:
        eligible = [fractal for fractal in fractals if fractal.event.confirmed_at]
        strokes: list[_StrokeCandidate] = []
        active: _FractalCandidate | None = None
        active_kind: str | None = None
        for candidate in eligible:
            kind = candidate.event.reason_code.endswith("top_fractal")
            if active is None:
                active = candidate
                active_kind = "top" if kind else "bottom"
                continue
            current_kind = "top" if kind else "bottom"
            if current_kind == active_kind:
                if (
                    (current_kind == "top" and candidate.event.price > active.event.price)
                    or (
                        current_kind == "bottom"
                        and candidate.event.price < active.event.price
                    )
                ) and strokes and strokes[-1].event.tradable_at is None:
                    previous = strokes[-1]
                    assert candidate.event.price is not None
                    strokes[-1] = _StrokeCandidate(
                        event=StrictEvent(
                            kind="stroke",
                            formed_at=previous.event.formed_at,
                            confirmed_at=candidate.event.confirmed_at,
                            tradable_at=candidate.event.tradable_at,
                            status="confirmed",
                            reason_code="strict_alternating_fractals",
                            low=min(previous.start_price, candidate.event.price),
                            high=max(previous.start_price, candidate.event.price),
                        ),
                        start_index=previous.start_index,
                        end_index=candidate.processed_index,
                        start_price=previous.start_price,
                        end_price=candidate.event.price,
                    )
                    active = candidate
                continue
            if (
                candidate.processed_index - active.processed_index
                < self.profile.minimum_stroke_separation
            ):
                continue
            first_price = active.event.price
            second_price = candidate.event.price
            assert first_price is not None and second_price is not None
            strokes.append(
                _StrokeCandidate(
                    event=StrictEvent(
                        kind="stroke",
                        formed_at=active.event.formed_at,
                        confirmed_at=candidate.event.confirmed_at,
                        tradable_at=candidate.event.tradable_at,
                        status="confirmed",
                        reason_code="strict_alternating_fractals",
                        low=min(first_price, second_price),
                        high=max(first_price, second_price),
                    ),
                    start_index=active.processed_index,
                    end_index=candidate.processed_index,
                    start_price=first_price,
                    end_price=second_price,
                )
            )
            active = candidate
            active_kind = current_kind
        return strokes

    @staticmethod
    def _overlap(strokes: list[_StrokeCandidate]) -> tuple[float, float] | None:
        low = max(stroke.event.low for stroke in strokes)
        high = min(stroke.event.high for stroke in strokes)
        return (low, high) if low <= high else None

    def _find_central_areas(
        self, strokes: list[_StrokeCandidate]
    ) -> tuple[list[StrictEvent], list[StrictEvent]]:
        candidates: list[StrictEvent] = []
        areas: list[StrictEvent] = []
        start = 0
        consumed_until = 0
        while start + self.profile.minimum_central_strokes <= len(strokes):
            group = strokes[start : start + self.profile.minimum_central_strokes]
            overlap = self._overlap(group)
            if overlap is None:
                start += 1
                continue
            end = start + self.profile.minimum_central_strokes
            while end < len(strokes):
                next_stroke = strokes[end].event
                if (
                    next_stroke.high is None
                    or next_stroke.low is None
                    or next_stroke.high < overlap[0]
                    or next_stroke.low > overlap[1]
                ):
                    break
                group.append(strokes[end])
                end += 1
            initial_last = strokes[start + self.profile.minimum_central_strokes - 1].event
            areas.append(
                StrictEvent(
                    kind="central_area",
                    formed_at=strokes[start + 2].event.formed_at,
                    confirmed_at=initial_last.confirmed_at,
                    tradable_at=initial_last.tradable_at,
                    status="confirmed",
                    reason_code="strict_three_stroke_overlap",
                    low=overlap[0],
                    high=overlap[1],
                )
            )
            start = end
            consumed_until = max(consumed_until, end)
        trailing = strokes[consumed_until:]
        if len(trailing) >= 2:
            overlap = self._overlap(trailing[-2:])
            if overlap is not None:
                candidates.append(
                    StrictEvent(
                        kind="central_area",
                        formed_at=trailing[-1].event.formed_at,
                        confirmed_at=None,
                        tradable_at=None,
                        status="candidate",
                        reason_code="strict_two_stroke_overlap_candidate",
                        low=overlap[0],
                        high=overlap[1],
                    )
                )
        return candidates, areas

    @staticmethod
    def _find_segments(strokes: list[_StrokeCandidate]) -> list[StrictEvent]:
        segments: list[StrictEvent] = []
        for start in range(0, len(strokes) - 2, 3):
            group = strokes[start : start + 3]
            if len(group) < 3:
                break
            first, third = group[0], group[2]
            first_is_up = first.end_price > first.start_price
            third_is_up = third.end_price > third.start_price
            if first_is_up != third_is_up:
                continue
            if (
                (first_is_up and third.end_price <= first.end_price)
                or (not first_is_up and third.end_price >= first.end_price)
            ):
                continue
            events = [stroke.event for stroke in group]
            segments.append(
                StrictEvent(
                    kind="segment",
                    formed_at=events[0].formed_at,
                    confirmed_at=events[-1].confirmed_at,
                    tradable_at=events[-1].tradable_at,
                    status="confirmed",
                    reason_code="strict_three_stroke_feature_sequence",
                    low=min(event.low for event in events if event.low is not None),
                    high=max(
                        event.high for event in events if event.high is not None
                    ),
                )
            )
        return segments

    @staticmethod
    def _find_signals(
        bars: list[DailyBar], central_areas: list[StrictEvent]
    ) -> list[StrictEvent]:
        confirmed: list[StrictEvent] = []
        candidate: StrictEvent | None = None
        for area in central_areas:
            signal = StrictChanAnalyzer._find_signal_for_area(bars, area)
            if signal is None:
                continue
            if signal.status == "confirmed":
                if signal not in confirmed:
                    confirmed.append(signal)
            else:
                candidate = signal
        return [*confirmed, *([candidate] if candidate is not None else [])]

    @staticmethod
    def _find_signal_for_area(
        bars: list[DailyBar], area: StrictEvent
    ) -> StrictEvent | None:
        if not bars:
            return None
        if area.high is None or area.low is None:
            return None
        candidate: tuple[int, str] | None = None
        for index, bar in enumerate(bars):
            if area.confirmed_at is not None and bar.trade_date <= area.confirmed_at:
                continue
            direction = (
                "up"
                if bar.close > area.high
                else "down"
                if bar.close < area.low
                else None
            )
            if direction is None:
                candidate = None
                continue
            if candidate is not None and candidate[1] == direction:
                formed_index = candidate[0]
                confirmed_at = bar.trade_date
                tradable_at = (
                    bars[index + 1].trade_date if index + 1 < len(bars) else None
                )
                return StrictEvent(
                    kind="signal",
                    formed_at=bars[formed_index].trade_date,
                    confirmed_at=confirmed_at,
                    tradable_at=tradable_at,
                    status="confirmed",
                    reason_code=f"strict_breakout_{direction}_candidate",
                )
            candidate = (index, direction)
        if candidate is not None:
            index, direction = candidate
            return StrictEvent(
                kind="signal",
                formed_at=bars[index].trade_date,
                confirmed_at=None,
                tradable_at=None,
                status="candidate",
                reason_code=f"strict_breakout_{direction}_candidate",
            )
        return None


__all__ = [
    "StrictChanAnalyzer",
    "StrictChanResult",
    "StrictEvent",
    "StrictProfile",
    "StrictProfileError",
    "load_strict_profile",
]
