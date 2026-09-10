# Project status

## Current state

- Branch: `feature/a-share-daily-report`
- Repository: `git@github.com:pengz4/stock-daily-report.git`
- Scope: A-share close research reports with simplified/strict Chan-style
  structure analysis, WeCom/Feishu notifications, GitHub Pages publication,
  and an execution-aware backtest CLI.
- Latest implementation commit: run `git log -1 --oneline` after pulling.

## Completed

- Deterministic daily report pipeline with JSON, Markdown, and HTML output.
- Input snapshots, SHA-256 hashes, transactional publication, recovery, and
  `reports-history` persistence.
- WeCom and Feishu notifications with retries, timeouts, byte limits, and
  channel-isolated failures.
- GitHub Actions scheduling, Pages deployment, and post-deployment
  notifications.
- `simplified-v1` and versioned `strict-v1` structural analyzers.
- No-lookahead prefix replay with formed/confirmed/tradable timestamps and
  structural precision/recall/F1 metrics.
- Execution-aware long-only backtest with costs, slippage, price limits,
  suspensions, holding horizons, overlap suppression, and evidence status.
- Independent `.github/workflows/structural-backtest.yml` and
  `stock-daily-report backtest` command.

## Verification

The latest local verification completed with:

- `pytest -q`: 317 tests passed.
- `ruff check .`: passed.
- `git diff --check`: passed.
- Fixture CLI smoke report: `reports/backtests/2026-09-04/report.json`.

The committed fixture is intentionally a small deterministic smoke fixture.
Its backtest evaluations are expected to report `not_enough_evidence`; they
must not be interpreted as performance evidence.

## Daily publication status

- The 2026-09-10 daily workflow completed successfully.
- Report artifacts are persisted on `reports-history` under
  `reports/2026-09-10/`.
- GitHub Pages is enabled with GitHub Actions as its build source.
- Production market data uses AkShare/Eastmoney first and the independent
  AkShare/Sina endpoint as fallback; the short fixture is no longer the
  production fallback.

## Known boundaries

- The repository does not yet bundle a sufficiently long historical dataset
  or an index data source. A real out-of-sample index baseline and a
  statistically meaningful watchlist portfolio comparison require those data.
- The current backtest report evaluates each watchlist symbol independently;
  equal-weight portfolio aggregation is not yet a production result.
- The simplified execution rule is explicitly versioned as
  `simplified-breakout-v1`; simplified structural observations remain
  descriptive outside that adapter.
- The system is research-only: it does not place orders or promise returns.

## Pull and resume at home

```bash
git clone git@github.com:pengz4/stock-daily-report.git
cd stock-daily-report
git fetch origin
git checkout --track origin/feature/a-share-daily-report
python3.11 -m venv .venv
.venv/bin/pip install -r requirements.lock
.venv/bin/pip install --no-build-isolation --no-deps .
.venv/bin/stock-daily-report backtest \
  --settings config/backtest.yaml \
  --watchlist config/watchlist.yaml \
  --fixture-directory fixtures/bars \
  --output-root .
```
