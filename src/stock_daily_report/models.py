"""Typed configuration models for the report pipeline."""

import re
from typing import Annotated, Literal

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

NonEmptyString = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
A_SHARE_CODE_PATTERN = re.compile(
    r"^(?:00[0-3]\d{3}|30[01]\d{3}|60[0135]\d{3}|688\d{3}|920\d{3})$"
)


class WatchlistStock(BaseModel):
    """A tracked mainland A-share security.

    Supported code classes are Shenzhen main-board (000/001/002/003), ChiNext
    (300/301), Shanghai main-board (600/601/603/605), STAR Market (688), and
    Beijing Stock Exchange (920). Legacy BSE aliases are not accepted because
    this current-date-only report supports post-2025 BSE codes only.
    """

    model_config = ConfigDict(extra="forbid")

    code: str
    name: NonEmptyString
    group: str = "default"
    # Tags are stripped of surrounding whitespace while retaining their case.
    tags: list[str] = Field(default_factory=list)
    news_enabled: bool = True

    @field_validator("code")
    @classmethod
    def require_supported_a_share_code(cls, value: str) -> str:
        if not A_SHARE_CODE_PATTERN.fullmatch(value):
            raise ValueError(
                "stock code must use a supported mainland A-share code prefix "
                "(SZ 000/001/002/003, ChiNext 300/301, SH 600/601/603/605, "
                "STAR 688, BSE 920; legacy BSE aliases are not accepted)"
            )
        return value

    @field_validator("tags")
    @classmethod
    def normalize_and_require_unique_tags(cls, value: list[str]) -> list[str]:
        normalized_tags = [tag.strip() for tag in value]
        if any(not tag for tag in normalized_tags):
            raise ValueError("tag must not be blank")
        if len(normalized_tags) != len(set(normalized_tags)):
            raise ValueError("duplicate tag(s) are not allowed")
        return normalized_tags


class Watchlist(BaseModel):
    """The version-controlled set of securities to report on."""

    model_config = ConfigDict(extra="forbid")

    stocks: list[WatchlistStock]

    @field_validator("stocks")
    @classmethod
    def require_stocks(cls, value: list[WatchlistStock]) -> list[WatchlistStock]:
        if not value:
            raise ValueError("watchlist must contain at least one stock")
        return value

    @model_validator(mode="after")
    def ensure_unique_codes(self) -> "Watchlist":
        codes = [stock.code for stock in self.stocks]
        duplicate_codes = sorted({code for code in codes if codes.count(code) > 1})
        if duplicate_codes:
            raise ValueError(f"duplicate stock code(s): {', '.join(duplicate_codes)}")
        return self


class RuleVersion(BaseModel):
    """The named version of the active analysis rule set."""

    model_config = ConfigDict(extra="forbid")

    name: NonEmptyString
    version: NonEmptyString


class NotificationSettings(BaseModel):
    """Webhook configuration for optional report summary delivery."""

    model_config = ConfigDict(extra="forbid")

    enabled_channels: set[Literal["wecom", "feishu"]] = Field(default_factory=set)
    wecom_webhook_url: AnyHttpUrl | None = None
    feishu_webhook_url: AnyHttpUrl | None = None

    @field_validator("enabled_channels", mode="before")
    @classmethod
    def reject_duplicate_enabled_channels(cls, value: object) -> object:
        if isinstance(value, list):
            seen_channels: set[str] = set()
            duplicates: set[str] = set()
            for channel in value:
                if isinstance(channel, str):
                    if channel in seen_channels:
                        duplicates.add(channel)
                    seen_channels.add(channel)
            if duplicates:
                raise ValueError(
                    "duplicate enabled channel(s) are not allowed: "
                    f"{', '.join(sorted(duplicates))}"
                )
        return value

    @field_validator("wecom_webhook_url", "feishu_webhook_url", mode="before")
    @classmethod
    def normalize_blank_webhook_urls(cls, value: object) -> object:
        if isinstance(value, str) and not value.strip():
            return None
        return value

    @model_validator(mode="after")
    def require_webhooks_for_enabled_channels(self) -> "NotificationSettings":
        if "wecom" in self.enabled_channels and self.wecom_webhook_url is None:
            raise ValueError("wecom webhook URL is required when wecom is enabled")
        if "feishu" in self.enabled_channels and self.feishu_webhook_url is None:
            raise ValueError("feishu webhook URL is required when feishu is enabled")
        return self


class Settings(BaseModel):
    """Top-level deterministic report settings."""

    model_config = ConfigDict(extra="forbid")

    rule_version: RuleVersion
    notifications: NotificationSettings
