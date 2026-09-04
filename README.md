# Stock Daily Report

An auditable, rule-first A-share daily research report. The pipeline validates
market data, writes immutable input snapshots, computes deterministic technical
and simplified Chan-structure observations, and publishes JSON, Markdown, and
HTML artifacts. It does not place orders or provide investment advice.

## Local run

```bash
python -m pip install -r requirements.lock
python -m pip install --no-deps -e .
stock-daily-report daily --date 2026-09-04 --output-root .
```

The default configuration uses the AkShare provider with a fixture fallback.
Use `--fixture-directory` for an offline run with fixture CSV files.

## Notifications

Enable one or both channels in `config/settings.yaml`:

```yaml
notifications:
  enabled_channels: [wecom, feishu]
  timeout_seconds: 10
  max_attempts: 3
```

Webhook URLs are runtime secrets, not configuration values:

| Name | Type | Required when |
| --- | --- | --- |
| `WECOM_WEBHOOK_URL` | Actions secret | `wecom` is enabled |
| `FEISHU_WEBHOOK_URL` | Actions secret | `feishu` is enabled |
| `REPORT_BASE_URL` | Actions variable | notifications are enabled |

The scheduled workflow generates the report, deploys GitHub Pages, and sends
the summary only after deployment succeeds. Reports and input snapshots are
also retained on the `reports-history` branch, while snapshots are excluded
from the Pages artifact. Run `stock-daily-report notify` locally to send an
already published `report.json`.

## Structural backtest

Run the versioned, execution-aware comparison offline with the committed
fixture data:

```bash
stock-daily-report backtest \
  --settings config/backtest.yaml \
  --watchlist config/watchlist.yaml \
  --fixture-directory fixtures/bars \
  --output-root .
```

The backtest replays both `simplified-v1` and `strict-v1` one bar at a time,
then records structural agreement and long-only execution metrics under
`reports/backtests/YYYY-MM-DD/report.json`. It models commission, slippage,
daily price limits, suspensions, fixed holding horizons, and a configured
out-of-sample split. Results below the configured minimum sample count are
marked `not_enough_evidence`; they are research diagnostics, not return
guarantees or trading instructions. GitHub Actions runs this separately from
the daily report workflow and uploads the report as an artifact.

## GitHub Actions

The workflow runs at 16:30 China Standard Time on Monday-Friday and supports
`workflow_dispatch` with an optional `report_date`. Configure GitHub Pages
with the Actions deployment source before the first scheduled run.
