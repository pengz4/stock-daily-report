"""Shared admission control for history requests, including forked scan workers."""

import fcntl
import hashlib
import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from stock_daily_report.providers.base import ProviderAvailabilityError

_LOGGER = logging.getLogger(__name__)


class ProviderRequestGate:
    """Space request starts and temporarily skip unavailable upstream providers."""

    def __init__(
        self,
        directory: str | Path,
        *,
        interval_seconds: float,
        cooldown_seconds: float,
    ) -> None:
        for value in (interval_seconds, cooldown_seconds):
            if not math.isfinite(value) or value < 0:
                raise ValueError("request gate durations must be finite and non-negative")
        self._directory = Path(directory) / ".request-limits"
        self._interval = interval_seconds
        self._cooldown = cooldown_seconds

    def acquire(self, provider: str) -> None:
        if self._interval == 0 and self._cooldown == 0:
            return
        while True:
            with self._state(provider) as state:
                now = time.time()
                if state["retry_after"] > now:
                    raise ProviderAvailabilityError(
                        provider,
                        "provider_circuit_open",
                        "Provider cooling down after an availability failure; "
                        f"retry in {state['retry_after'] - now:.1f}s",
                    )
                delay = state["next_request"] - now
                if delay <= 0:
                    state["next_request"] = now + self._interval
                    return
            # Never hold the file lock while sleeping or doing network I/O.
            time.sleep(min(delay, 0.1))

    def record_failure(self, provider: str) -> None:
        if self._cooldown == 0:
            return
        with self._state(provider) as state:
            state["retry_after"] = max(
                state["retry_after"], time.time() + self._cooldown
            )
        _LOGGER.warning(
            "%s unavailable; pausing new history requests for %.1fs",
            provider,
            self._cooldown,
        )

    @contextmanager
    def _state(self, provider: str) -> Iterator[dict[str, float]]:
        self._directory.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(provider.encode("utf-8")).hexdigest()
        path = self._directory / f"{name}.json"
        with path.with_suffix(".lock").open("a+", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                try:
                    text = path.read_text(encoding="utf-8")
                except FileNotFoundError:
                    text = ""
                state = (
                    json.loads(text)
                    if text
                    else {"next_request": 0.0, "retry_after": 0.0}
                )
                if (
                    not isinstance(state, dict)
                    or set(state) != {"next_request", "retry_after"}
                    or any(
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(value)
                        or value < 0
                        for value in state.values()
                    )
                ):
                    raise ValueError(f"Invalid request gate state: {path}")
                yield state
                temporary_path = None
                try:
                    with tempfile.NamedTemporaryFile(
                        mode="w",
                        encoding="utf-8",
                        dir=self._directory,
                        prefix=f".{name}.",
                        suffix=".tmp",
                        delete=False,
                    ) as temporary:
                        temporary_path = Path(temporary.name)
                        json.dump(state, temporary)
                        temporary.flush()
                        os.fsync(temporary.fileno())
                    os.replace(temporary_path, path)
                finally:
                    if temporary_path is not None:
                        temporary_path.unlink(missing_ok=True)
            except json.JSONDecodeError as error:
                raise ValueError(f"Invalid request gate state: {path}") from error
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
