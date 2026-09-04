import pytest

from stock_daily_report.config import ConfigurationError, load_settings, load_watchlist


def test_load_watchlist_returns_valid_a_share_codes(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        "stocks:\n  - code: '600519'\n    name: 贵州茅台\n",
        encoding="utf-8",
    )

    watchlist = load_watchlist(path)

    assert watchlist.stocks[0].code == "600519"


@pytest.mark.parametrize(
    ("code", "market"),
    [
        ("600519", "Shanghai main board"),
        ("000001", "Shenzhen main board"),
        ("300750", "ChiNext"),
        ("688981", "STAR Market"),
        ("920001", "Beijing Stock Exchange"),
    ],
)
def test_load_watchlist_accepts_supported_mainland_a_share_codes(
    tmp_path, code, market
):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        f"stocks:\n  - code: '{code}'\n    name: {market}\n",
        encoding="utf-8",
    )

    watchlist = load_watchlist(path)

    assert watchlist.stocks[0].code == code


@pytest.mark.parametrize("code", ["430047", "831010", "840000"])
def test_load_watchlist_rejects_legacy_bse_aliases(tmp_path, code):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        f"stocks:\n  - code: '{code}'\n    name: 旧北交所代码\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigurationError, match="legacy BSE aliases are not accepted"
    ):
        load_watchlist(path)


@pytest.mark.parametrize("code", ["200012", "900901", "100000", "500000", "700000"])
def test_load_watchlist_rejects_non_a_share_code_prefixes(tmp_path, code):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        f"stocks:\n  - code: '{code}'\n    name: 非 A 股\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ConfigurationError, match="supported mainland A-share code prefix"
    ):
        load_watchlist(path)


def test_load_watchlist_rejects_empty_stock_list(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text("stocks: []\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="at least one stock"):
        load_watchlist(path)


@pytest.mark.parametrize(
    "document",
    [
        "stocks:\n  - name: 贵州茅台\n",
        "stocks:\n  - code: ''\n    name: 贵州茅台\n",
    ],
)
def test_load_watchlist_rejects_missing_codes(tmp_path, document):
    path = tmp_path / "watchlist.yaml"
    path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigurationError, match="Invalid watchlist configuration"):
        load_watchlist(path)


@pytest.mark.parametrize("code", ["60051", "6005190", "ABCDEF"])
def test_load_watchlist_rejects_non_six_digit_codes(tmp_path, code):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        f"stocks:\n  - code: '{code}'\n    name: 贵州茅台\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="Invalid watchlist configuration"):
        load_watchlist(path)


@pytest.mark.parametrize("name", ["''", "'   '"])
def test_load_watchlist_rejects_empty_names(tmp_path, name):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        f"stocks:\n  - code: '600519'\n    name: {name}\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="Invalid watchlist configuration"):
        load_watchlist(path)


def test_load_watchlist_rejects_duplicate_codes(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        "stocks:\n"
        "  - code: '600519'\n"
        "    name: 贵州茅台\n"
        "  - code: '600519'\n"
        "    name: 重复股票\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="duplicate stock code"):
        load_watchlist(path)


@pytest.mark.parametrize("tag", ["''", "'   '"])
def test_load_watchlist_rejects_blank_tags(tmp_path, tag):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        "stocks:\n"
        "  - code: '600519'\n"
        "    name: 贵州茅台\n"
        f"    tags: [{tag}]\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="tag must not be blank"):
        load_watchlist(path)


def test_load_watchlist_rejects_tags_duplicated_after_normalization(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        "stocks:\n"
        "  - code: '600519'\n"
        "    name: 贵州茅台\n"
        "    tags: ['value', ' value ']\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="duplicate tag"):
        load_watchlist(path)


def test_load_watchlist_normalizes_valid_tags_by_stripping_whitespace(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text(
        "stocks:\n"
        "  - code: '600519'\n"
        "    name: 贵州茅台\n"
        "    tags: [' value ', Growth]\n",
        encoding="utf-8",
    )

    watchlist = load_watchlist(path)

    assert watchlist.stocks[0].tags == ["value", "Growth"]


@pytest.mark.parametrize("channel", ["wecom", "feishu"])
def test_load_settings_allows_enabled_channel_without_secret_in_file(tmp_path, channel):
    path = tmp_path / "settings.yaml"
    path.write_text(
        f"rule_version:\n  name: simplified\n  version: v1\n"
        f"notifications:\n  enabled_channels: ['{channel}']\n",
        encoding="utf-8",
    )

    assert channel in load_settings(path).notifications.enabled_channels


@pytest.mark.parametrize("channel", ["wecom", "feishu"])
def test_load_settings_rejects_webhook_secret_fields(tmp_path, channel):
    path = tmp_path / "settings.yaml"
    path.write_text(
        f"rule_version:\n  name: simplified\n  version: v1\n"
        f"notifications:\n  enabled_channels: ['{channel}']\n",
        encoding="utf-8",
    )
    path.write_text(
        path.read_text(encoding="utf-8")
        + f"  {channel}_webhook_url: https://example.invalid/hook\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="Extra inputs are not permitted"):
        load_settings(path)


def test_load_settings_allows_no_enabled_notification_channels(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "rule_version:\n  name: simplified\n  version: v1\n"
        "notifications:\n  enabled_channels: []\n",
        encoding="utf-8",
    )

    settings = load_settings(str(path))

    assert settings.notifications.enabled_channels == set()


def test_load_settings_uses_configurable_market_data_defaults(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "rule_version:\n  name: simplified\n  version: v1\n"
        "notifications:\n  enabled_channels: []\n"
        "market_data:\n"
        "  primary_provider: akshare\n"
        "  fallback_provider: fixture\n"
        "  cache_directory: .cache/stock-daily-report\n"
        "  cache_ttl_seconds: 900\n"
        "  minimum_history_bars: 120\n"
        "  max_completed_trading_day_lag: 2\n",
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.market_data.primary_provider == "akshare"
    assert settings.market_data.cache_ttl_seconds == 900
    assert settings.market_data.minimum_history_bars == 120
    assert settings.market_data.max_completed_trading_day_lag == 2


def test_load_settings_rejects_duplicate_enabled_channels(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "rule_version:\n  name: simplified\n  version: v1\n"
        "notifications:\n"
        "  enabled_channels: [wecom, wecom]\n"
        "  wecom_webhook_url: https://example.com/wecom\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="duplicate enabled channel"):
        load_settings(path)


def test_load_settings_rejects_unsupported_enabled_channels(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "rule_version:\n  name: simplified\n  version: v1\n"
        "notifications:\n  enabled_channels: [slack]\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="enabled_channels"):
        load_settings(path)


def test_load_settings_accepts_unique_enabled_channels(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "rule_version:\n  name: simplified\n  version: v1\n"
        "notifications:\n"
        "  enabled_channels: [wecom, feishu]\n",
        encoding="utf-8",
    )

    settings = load_settings(path)

    assert settings.notifications.enabled_channels == {"wecom", "feishu"}


def test_load_settings_requires_explicit_notification_configuration(tmp_path):
    path = tmp_path / "settings.yaml"
    path.write_text(
        "rule_version:\n  name: simplified\n  version: v1\n",
        encoding="utf-8",
    )

    with pytest.raises(ConfigurationError, match="Invalid settings configuration"):
        load_settings(path)


@pytest.mark.parametrize(
    ("filename", "document", "expected_message"),
    [
        ("missing.yaml", None, "Configuration file not found"),
        ("empty.yaml", "", "Configuration file is empty"),
        ("list.yaml", "- not\n- a mapping\n", "top-level YAML value must be a mapping"),
    ],
)
def test_configuration_loaders_raise_clear_errors_for_invalid_documents(
    tmp_path, filename, document, expected_message
):
    path = tmp_path / filename
    if document is not None:
        path.write_text(document, encoding="utf-8")

    with pytest.raises(ConfigurationError, match=expected_message):
        load_watchlist(path)


def test_load_watchlist_rejects_non_utf8_documents(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_bytes(b"stocks:\n  - code: '\xff'\n")

    with pytest.raises(
        ConfigurationError, match="must be valid UTF-8"
    ):
        load_watchlist(path)


def test_load_watchlist_rejects_arbitrary_yaml_objects(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text("!!python/object/apply:os.system ['echo unsafe']\n", encoding="utf-8")

    with pytest.raises(ConfigurationError, match="Invalid YAML"):
        load_watchlist(path)
