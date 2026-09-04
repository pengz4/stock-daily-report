"""Typed configuration models for the report pipeline."""

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


class WatchlistStock(BaseModel):
    """A tracked mainland A-share security."""

    model_config = ConfigDict(extra="forbid")

    code: str = Field(pattern=r"^\d{6}$")
    name: NonEmptyString
    group: str = "default"
    tags: list[str] = Field(default_factory=list)
    news_enabled: bool = True


class Watchlist(BaseModel):
    """The version-controlled set of securities to report on."""

    model_config = ConfigDict(extra="forbid")

    stocks: list[WatchlistStock]

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
