"""Canonical identity helpers for persisted market-state observations."""

from __future__ import annotations

import hashlib
import json
import re

from stock_daily_report.market_scan.models import MarketState
from stock_daily_report.models import (
    MARKET_STATE_LOOKBACK_PERIODS,
    MarketStateSettings,
)

_IDENTITY_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class MarketStateIdentityError(ValueError):
    """Raised when a stored market-state identity is unsafe to reuse."""


def canonical_market_state_payload(state: MarketState) -> dict[str, object]:
    """Return market-state data with only observation time removed."""

    payload = state.model_dump(mode="json")
    payload.pop("generated_at", None)
    return payload


def build_market_state_identity(
    settings: MarketStateSettings,
    state: MarketState,
) -> str:
    """Hash the market-state configuration and semantic observation payload."""

    if settings.lookback_periods != MARKET_STATE_LOOKBACK_PERIODS:
        raise MarketStateIdentityError(
            "unsupported market-state lookback_periods cannot be hashed"
        )
    identity_payload = {
        "configuration": settings.model_dump(mode="json"),
        "state": canonical_market_state_payload(state),
    }
    encoded = json.dumps(
        identity_payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def resolve_market_state_identity(
    settings: MarketStateSettings,
    state: MarketState | None,
    stored_identity: str | None,
) -> str | None:
    """Validate a stored identity or migrate a legacy missing identity."""

    if state is None:
        if stored_identity is None:
            return None
        raise MarketStateIdentityError(
            "market_state_identity requires a market_state payload"
        )

    canonical_identity = build_market_state_identity(settings, state)
    if stored_identity is None:
        return canonical_identity
    if (
        not isinstance(stored_identity, str)
        or _IDENTITY_PATTERN.fullmatch(stored_identity) is None
    ):
        raise MarketStateIdentityError(
            "stored market_state_identity is invalid"
        )
    if stored_identity != canonical_identity:
        raise MarketStateIdentityError(
            "stored market_state_identity does not match the canonical "
            "market-state payload and configuration"
        )
    return stored_identity


__all__ = [
    "MarketStateIdentityError",
    "build_market_state_identity",
    "canonical_market_state_payload",
    "resolve_market_state_identity",
]
