"""Canonical identity helpers for persisted market-state observations."""

from __future__ import annotations

import hashlib
import json

from stock_daily_report.market_scan.models import MarketState
from stock_daily_report.models import MarketStateSettings


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


__all__ = [
    "build_market_state_identity",
    "canonical_market_state_payload",
]
