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

The production configuration uses AkShare/Eastmoney with an AkShare/Sina fallback.
Use `--fixture-directory` for an offline run with fixture CSV files.

History requests are spaced at least one second apart per provider, shared
across processes using the same cache directory. An availability failure pauses
new history requests to that provider for 15 seconds; the independent fallback
is still available. Invalid data is not silently replaced with fallback data.
Resumable scans back off for 2 and 4 seconds before retrying availability
failures, rather than immediately repeating requests. Provider timeouts and
schema errors are retained for diagnosis instead of immediately retried.

The scanner uses four history workers. Queued symbols do not consume their
per-symbol fetch timeout. Resumable scans checkpoint every 100 candidates by
default (or fewer when the candidate batch limit is smaller), so runner
interruption does not discard an entire full-market scan.
Successful responses are cached for six hours, and a
valid cached fallback is reused before making another primary-provider request.
The daily and scan workflows share same-date response caches through Actions
cache; locks, request cooldown state, and publication recovery journals are not
uploaded. Existing history-writer concurrency groups serialize these jobs.
History writers and the daily Pages lifecycle use `queue: max`, retaining up to
100 pending jobs or runs per group instead of replacing an older pending request.
When a group reaches that limit, GitHub cancels additional requests.
Cache reuse is best effort, subject to Actions cache availability and TTL.

Completed market scans emit one `[market-scan] provider_metrics=<JSON>` summary
to the workflow log. It reports stock-history network attempts, cache hits,
fallback and resumable-fetch retries, failures/failure rate, and
average/maximum request latency by provider. Retry counts exclude retries
internal to a provider client. A timed-out or forcibly terminated request may
not return per-provider timing telemetry.

Both scan scopes also share a full-universe snapshot after 15:30 Shanghai time.
The snapshot is cached only after the provider's existing completeness checks,
before watchlist filtering, and is validated for schema, digest, count, unique
codes, market identifiers, and quote date when reused. Intraday snapshots and
previous-day snapshots are not reused. Universe availability failures get at
most three attempts, with 5- and 10-second backoff; schema and completeness
errors fail immediately. If both upstreams stay unavailable and no valid
snapshot exists, the scan fails explicitly rather than publishing stale quotes.

This does not append yesterday's forward-adjusted prices to today's history:
each uncached request still retrieves a consistent adjusted series. Cross-day
incremental history requires separate raw-price and corporate-action storage.
These controls reduce avoidable traffic; they cannot guarantee that an upstream
service will accept requests from every network or runner.

## Full-market scan

```bash
stock-daily-report market-scan \
  --date "$(TZ=Asia/Shanghai date +%F)" \
  --settings config/market_scan.yaml \
  --data-settings config/settings.yaml \
  --output-root .
```

Live universe quotes are current-only, so a new scan must use the current
Asia/Shanghai date. A backdated request is rejected without writing an
artifact rather than relabeling current quotes. The workflow may still reuse a
same-date existing artifact after validating its schema, embedded date, rule
version, and configuration hash.

When an Actions run restores `scan.json` from `reports-history`, an artifact
created with old data or scan settings cannot be reused as current output.
Without `--resumable`, this deliberately fails with
`Existing market scan artifact configuration does not match`.
For a current-date `--resumable` run, a configuration-hash change instead logs
the old/new hashes and rebuilds the scan. The old artifact stays in place until
the replacement is successfully published; invalid schema, embedded dates,
rule versions, and market-state identities still fail validation.
Checkpoints are also bound to the full configuration hash when supplied, so
changes to data-provider or quality settings archive old progress rather than
relabeling its results. The first run after this checkpoint-identity update
rebuilds legacy progress once. This fixes the pre-fetch failure in Actions run
`39273289224`; it is separate from upstream connection/response failures.

Index market-state history uses a dedicated Sina HTTPS daily-price source for
the five configured indices, with explicit exchange-qualified index symbols,
one transport retry, a 20-second read timeout, and validated same-date
after-close caching for six hours. Eastmoney/AkShare remains the availability
fallback. Index calculations only need dated closing prices: the price-only
index model does not fabricate missing amount/turnover fields or relax the
stock-history quality contract. Empty, duplicate, non-finite, and stale index
responses fail explicitly.

Unpublished checkpoints retry incomplete index observations on resumption.
Published observations remain immutable by default. To repair a published
incomplete market state, dispatch the market-scan workflow with
`refresh_market_state=true` (CLI: `--resumable --refresh-market-state`).
The repair must produce a fully available market state with unchanged scan
configuration and valid canonical identities; the original `scan.json` is
archived under the date's `scan-archive/` before atomic replacement. Failed
repairs leave the published artifact unchanged. Healthy published observations
are not silently refreshed. Actions summaries list actual index availability
and latest dates rather than treating a green workflow as proof of complete
index coverage.

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
| `FEISHU_WEBHOOK_SECRET` | Actions secret | `feishu` has signature verification enabled |
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

## Portfolio backtest

Replay the configured market-scan profile as an equal-weight portfolio using
offline normalized daily-bar CSVs:

```bash
stock-daily-report portfolio-backtest \
  --settings config/portfolio_backtest.yaml \
  --market-scan-settings config/market_scan.yaml \
  --watchlist config/portfolio_backtest_fixture_watchlist.yaml \
  --fixture-directory fixtures/bars \
  --output-root .
```

Signals are recalculated from each symbol's history available at that session's
close. Rebalances execute no earlier than the next session's open, in 100-share
lots, using configured commission/slippage and handling locked price-limit bars
and suspensions according to the configured limit rate. The report compares the
portfolio with an equal-weight buy-and-hold baseline using the same watchlist at
`reports/backtests/portfolio/YYYY-MM-DD/report.json`. Configure
`out_of_sample_start` before interpreting any result; otherwise the report
labels evidence as `holdout_not_configured`.
Annualized return, annualized volatility, and Sharpe are omitted until the
configured holdout contains at least `minimum_sample_count` daily observations.

The command only reads local data and does not download market history. The
committed fixture is only a smoke test. Reports explicitly warn that the
watchlist is not reconstructed point-in-time (so survivorship bias is possible),
corporate actions and dividends are not modeled separately, stale holdings are
marked at the last observed close, and the baseline is not a broad-market index.
This version evaluates one holdout and does not do walk-forward testing or
correct for parameter-selection bias. A meaningful evaluation still requires a
licensed, multi-symbol historical dataset with delisted securities, validated
corporate actions, and a predeclared holdout period.

## GitHub Actions

Pull requests targeting `master` run Ruff and the full pytest suite.
The workflow runs at 16:30 China Standard Time on Monday-Friday and supports
`workflow_dispatch` with an optional `report_date`. Configure GitHub Pages
with the Actions deployment source before the first scheduled run.
CI development tools are pinned in `requirements-dev.lock`; local CI-equivalent
checks are `ruff check .` and `pytest -q`.
