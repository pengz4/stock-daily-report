# Notifications

The daily CLI sends a compact summary only after the report and snapshot have
been published successfully. Each enabled channel is attempted independently;
transient network errors and HTTP `408`, `429`, and `5xx` responses receive
bounded exponential retries. A failed channel makes the command exit non-zero
and is reported without printing the webhook URL.

Enable channels in `config/settings.yaml`:

```yaml
notifications:
  enabled_channels: [wecom, feishu]
  timeout_seconds: 10
  max_attempts: 3
```

Webhook URLs must be supplied at runtime, never committed to YAML:

```bash
export WECOM_WEBHOOK_URL='https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...'
export FEISHU_WEBHOOK_URL='https://open.feishu.cn/open-apis/bot/v2/hook/...'
export REPORT_BASE_URL='https://<owner>.github.io/<repository>'
stock-daily-report daily --date 2026-09-04 --output-root .
```

When the Feishu custom bot has signature verification enabled, also export its
signing secret; the adapter then stamps each request with a fresh `timestamp`
and matching HMAC-SHA256 `sign`:

```bash
export FEISHU_WEBHOOK_SECRET='<bot signing secret>'
```

`--report-url` is the final absolute report URL and overrides
`REPORT_BASE_URL` for a single run. An absolute URL is required whenever
notifications are enabled.
