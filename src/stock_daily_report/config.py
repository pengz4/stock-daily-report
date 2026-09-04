"""Safe, validated loaders for version-controlled YAML configuration."""

from pathlib import Path
from typing import TypeVar

import yaml
from pydantic import BaseModel, ValidationError

from stock_daily_report.models import Settings, Watchlist

ConfigModel = TypeVar("ConfigModel", bound=BaseModel)


class ConfigurationError(ValueError):
    """Raised when a configuration document cannot be safely validated."""


def _read_yaml_mapping(path: str | Path) -> dict[str, object]:
    config_path = Path(path)
    try:
        document = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise ConfigurationError(f"Configuration file not found: {config_path}") from error
    except UnicodeDecodeError as error:
        raise ConfigurationError(
            f"Configuration file must be valid UTF-8: {config_path}"
        ) from error
    except OSError as error:
        raise ConfigurationError(
            f"Could not read configuration file: {config_path}"
        ) from error
    except yaml.YAMLError as error:
        raise ConfigurationError(f"Invalid YAML in configuration file: {config_path}") from error

    if document is None:
        raise ConfigurationError(f"Configuration file is empty: {config_path}")
    if not isinstance(document, dict):
        raise ConfigurationError(
            f"Configuration top-level YAML value must be a mapping: {config_path}"
        )
    return document


def _load_model(path: str | Path, model: type[ConfigModel], label: str) -> ConfigModel:
    document = _read_yaml_mapping(path)
    try:
        return model.model_validate(document)
    except ValidationError as error:
        raise ConfigurationError(
            f"Invalid {label} configuration: {error}"
        ) from error


def load_watchlist(path: str | Path) -> Watchlist:
    """Load and validate a YAML watchlist from a UTF-8 file."""

    return _load_model(path, Watchlist, "watchlist")


def load_settings(path: str | Path) -> Settings:
    """Load and validate YAML settings from a UTF-8 file."""

    return _load_model(path, Settings, "settings")
