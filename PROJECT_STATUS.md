# Project status

## Current state

Status checked 2026-10-23 against `master` at `41ac1f6` (PR #6). The repository
default branch is `master`.

The project generates auditable A-share close research reports, full-market and
watchlist scans, market-state summaries, notifications, GitHub Pages output, and
execution-aware backtest diagnostics. It is research-only and does not place
orders or promise returns.

## Delivered capabilities

- Deterministic JSON, Markdown, and HTML reports with validated inputs,
  immutable snapshots, hashes, transactional publication, and recovery.
- WeCom/Feishu notification adapters and scheduled GitHub Actions/Pages
  deployment.
- Versioned simplified and strict Chan-style analyzers, no-lookahead replay,
  structural evaluation, and execution-aware long-only backtest assumptions.
- Full-market screening with primary/fallback data providers, data-quality and
  coverage gates, explainable rankings, checkpoints, retry/resume, and
  immutable date-partitioned scan artifacts.
- Per-candidate provider attempt telemetry is retained in resumable checkpoints
  and summarized in scan logs by provider: requests, cache hits, provider
  fallback and resumable-fetch retries, failure rate, and latency. Metrics do
  not include provider-client internal retries; forcibly terminated requests
  may lack timing data.
- Market-state breadth and five configured index histories; dedicated Sina
  index-price retrieval with AkShare/Eastmoney fallback.
- PR CI on `master` for Ruff and the complete pytest suite. The CI installation
  uses the checkout in editable mode because tests and CLI defaults resolve
  configuration and site files relative to the source tree.

## Verified operational configuration

`config/market_scan.yaml` currently sets 4 history workers, a maximum of 6,000
eligible candidates, an 80% minimum successful-history coverage ratio, and 120
minimum history bars. Rankings remain unavailable when the scan is incomplete
or coverage is below threshold.

The market-scan and daily-report history writers use serialized Actions
concurrency groups with `queue: max` (up to 100 pending jobs per group).
Provider response caching is best-effort and date/configuration scoped.

The latest confirmed daily-report and full-market scan Actions on `master`
completed successfully on 2026-10-22. CI was added by PR #6 and its quality job
passed on the merged change. Check GitHub Actions for runs after this status
date before relying on current service health.

## Verification

- Latest local full-suite run on the current code: 682 passed, 2 skipped.
- `ruff check .` passed.
- The suite reports a known Python multiprocessing fork/thread deprecation
  warning; it does not fail the tests.
- CI runtime dependencies are pinned in `requirements.lock`; pytest, Ruff, and
  their test-time dependencies are pinned in `requirements-dev.lock`.

## Data and backtest limitations

- The committed bar fixture contains one symbol (`600519`) and only three
  trading days. It is a deterministic smoke fixture, not real historical
  evidence and not enough to estimate out-of-sample performance.
- No approved multi-symbol historical dataset is currently bundled. Portfolio
  backtesting remains deferred until data provenance, usage rights, adjustment
  methodology, corporate actions, delisted-symbol treatment, date coverage,
  and a predeclared holdout split are available and validated.
- The current backtest evaluates symbols individually. It does not produce a
  portfolio-level, equal-weight out-of-sample result.
- Before portfolio evaluation, obtain a sufficiently long, multi-symbol
  historical dataset with documented provenance/usage rights, consistent
  adjustment semantics, trading calendars, survivorship treatment, and a
  predeclared out-of-sample date split. Validate coverage and corporate-action
  handling before making any performance claim.
- Do not interpret fixture outcomes or a green CI/Actions run as investment
  performance evidence.

## Local development

Install `requirements.lock`, install the checkout editable, then install
`requirements-dev.lock` to reproduce CI. Run `ruff check .` and `pytest -q`
before opening or updating a PR.
