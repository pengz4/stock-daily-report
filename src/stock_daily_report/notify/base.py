"""Shared notification models and bounded webhook delivery."""

from __future__ import annotations

import json
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

NotificationStatus = Literal["delivered", "failed"]
WebhookTransport = Callable[[str, bytes, float], tuple[int, bytes]]


@dataclass(frozen=True)
class NotificationSummary:
    """The small, non-sensitive subset of a report sent to chat."""

    report_date: str
    generated_at: datetime
    report_url: str
    stock_count: int
    decision_counts: Mapping[str, int]
    focus_items: tuple[str, ...]
    high_risks: tuple[str, ...]


@dataclass(frozen=True)
class NotificationOutcome:
    """One channel delivery result."""

    channel: str
    status: NotificationStatus
    error: str | None = None


class NotificationDeliveryError(RuntimeError):
    """Raised after all enabled channels have been attempted."""

    def __init__(self, outcomes: tuple[NotificationOutcome, ...]) -> None:
        self.outcomes = outcomes
        failed = ", ".join(
            f"{outcome.channel}: {outcome.error}"
            for outcome in outcomes
            if outcome.status == "failed"
        )
        super().__init__(f"Notification delivery failed ({failed})")


class WebhookError(RuntimeError):
    """A webhook request failed and identifies whether retrying is useful."""

    def __init__(self, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.transient = transient


def post_json(
    url: str,
    payload: Mapping[str, object],
    *,
    timeout_seconds: float,
    max_attempts: int,
    transport: WebhookTransport | None = None,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """POST JSON, retrying only transient network and HTTP failures."""

    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    request_transport = transport or _urlopen_transport
    for attempt in range(1, max_attempts + 1):
        try:
            status, response_body = request_transport(url, body, timeout_seconds)
            if not 200 <= status < 300:
                raise WebhookError(
                    f"webhook returned HTTP {status}",
                    transient=status in {408, 429} or status >= 500,
                )
            _validate_webhook_response(response_body)
            return
        except WebhookError as error:
            if not error.transient or attempt == max_attempts:
                raise
        except (TimeoutError, URLError) as error:
            if attempt == max_attempts:
                raise WebhookError("webhook network request failed", transient=True) from error
        sleep(min(4.0, 0.5 * (2 ** (attempt - 1))))


def _urlopen_transport(url: str, body: bytes, timeout: float) -> tuple[int, bytes]:
    request = Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read()


def _validate_webhook_response(response_body: bytes) -> None:
    if not response_body:
        return
    try:
        document = json.loads(response_body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return
    if not isinstance(document, dict):
        return
    for key in ("errcode", "code"):
        value = document.get(key)
        if isinstance(value, int) and value != 0:
            raise WebhookError(f"webhook API returned {key}={value}", transient=False)


class WebhookNotifier:
    """Base class for a provider-specific payload builder and sender."""

    channel: str

    def __init__(
        self,
        webhook_url: str,
        *,
        timeout_seconds: float = 10.0,
        max_attempts: int = 3,
        transport: WebhookTransport | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if not webhook_url:
            raise ValueError("webhook URL must not be blank")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.webhook_url = webhook_url
        self.timeout_seconds = timeout_seconds
        self.max_attempts = max_attempts
        self.transport = transport
        self.sleep = sleep

    def send(self, summary: NotificationSummary) -> None:
        post_json(
            self.webhook_url,
            self.build_payload(summary),
            timeout_seconds=self.timeout_seconds,
            max_attempts=self.max_attempts,
            transport=self.transport,
            sleep=self.sleep,
        )

    def build_payload(self, summary: NotificationSummary) -> dict[str, object]:
        raise NotImplementedError


def _shorten(value: str, limit: int = 240) -> str:
    value = " ".join(value.split())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


__all__ = [
    "NotificationDeliveryError",
    "NotificationOutcome",
    "NotificationSummary",
    "WebhookError",
    "WebhookNotifier",
    "WebhookTransport",
    "_shorten",
    "post_json",
]
