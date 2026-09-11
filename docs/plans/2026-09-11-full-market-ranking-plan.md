# Full-Market Dual Ranking Implementation Plan

> **For Copilot:** Use executing-plans skill to implement this plan task-by-task.

**Goal:** Add an explainable full-A-share trend Top 30, balanced Top 30, and highlighted consensus group while preserving YAML watchlist reports.

**Architecture:** A separate scanner enumerates the market, filters every symbol, retrieves history with bounded concurrency, scores eligible stocks, and writes a versioned immutable scan artifact. The daily report consumes a same-date artifact when present but remains publishable when scanning is unavailable. Ranking-group performance is evaluated separately before any stronger confidence wording is allowed.

**Tech Stack:** Python 3.11, Pydantic 2, AkShare, existing technical/Chan analyzers, pytest, Ruff, GitHub Actions.

---

### Task 1: Add versioned market-scan configuration

**Files:**
- Create: `config/market_scan.yaml`
- Modify: `src/stock_daily_report/models.py`
- Modify: `src/stock_daily_report/config.py`
- Test: `tests/test_config.py`

**Step 1: Write the failing tests**

Add tests that load:

```yaml
rule_version: market-scan-v1
trend_limit: 30
balanced_limit: 30
minimum_history_bars: 120
minimum_latest_amount: 50000000
minimum_coverage_ratio: 0.80
max_workers: 8
max_candidates: 1200
```

Assert positive limits, coverage in `(0, 1]`, finite numeric thresholds,
unique profile names, and rejection of extra keys.

**Step 2: Run the tests and verify failure**

Run:

```bash
pytest -q tests/test_config.py -k market_scan
```

Expected: fail because `MarketScanSettings` and `load_market_scan_settings`
do not exist.

**Step 3: Implement the minimal models and loader**

Add frozen Pydantic settings models and reuse the existing safe YAML loader.
Keep scoring weights fixed in `market-scan-v1`; do not expose arbitrary user
weights under the same version.

**Step 4: Verify**

Run:

```bash
pytest -q tests/test_config.py -k market_scan
ruff check src/stock_daily_report/models.py src/stock_daily_report/config.py tests/test_config.py
```

Expected: pass.

**Step 5: Commit**

```bash
git add config/market_scan.yaml src/stock_daily_report/models.py \
  src/stock_daily_report/config.py tests/test_config.py
git commit -m "feat: configure full-market scan"
```

### Task 2: Add the full-market universe provider

**Files:**
- Create: `src/stock_daily_report/providers/universe.py`
- Test: `tests/test_universe_provider.py`

**Step 1: Write the failing tests**

Use an injected bulk fetcher and verify:

- Shanghai, Shenzhen, ChiNext, STAR, and BSE codes normalize correctly;
- names and latest quote fields are preserved;
- malformed rows raise `ProviderDataError`;
- network failures raise `ProviderAvailabilityError`;
- duplicate codes and unsupported instruments are rejected.

**Step 2: Verify failure**

Run:

```bash
pytest -q tests/test_universe_provider.py
```

Expected: import failure because the provider does not exist.

**Step 3: Implement**

Create immutable `UniverseQuote` records and an `AkShareUniverseProvider`
adapter around the bulk A-share quote endpoint. Do not couple universe
discovery to historical-data retrieval.

**Step 4: Verify**

Run:

```bash
pytest -q tests/test_universe_provider.py
ruff check src/stock_daily_report/providers/universe.py tests/test_universe_provider.py
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/stock_daily_report/providers/universe.py tests/test_universe_provider.py
git commit -m "feat: discover full A-share universe"
```

### Task 3: Implement deterministic eligibility filtering

**Files:**
- Create: `src/stock_daily_report/market_scan/__init__.py`
- Create: `src/stock_daily_report/market_scan/filters.py`
- Test: `tests/test_market_scan_filters.py`

**Step 1: Write the failing tests**

Cover ST/`*ST`/delisting names, unsupported codes, nonpositive quotes,
suspension, insufficient latest amount, insufficient bars, stale histories,
and stable exclusion reason codes.

**Step 2: Verify failure**

Run:

```bash
pytest -q tests/test_market_scan_filters.py
```

Expected: fail because filter functions do not exist.

**Step 3: Implement**

Implement separate universe-stage and history-stage filters. Return structured
results rather than booleans:

```python
EligibilityResult(eligible: bool, reason_codes: tuple[str, ...])
```

Never silently discard a symbol.

**Step 4: Verify**

Run:

```bash
pytest -q tests/test_market_scan_filters.py
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/stock_daily_report/market_scan tests/test_market_scan_filters.py
git commit -m "feat: filter market scan candidates"
```

### Task 4: Implement trend and balanced scoring

**Files:**
- Create: `src/stock_daily_report/market_scan/scoring.py`
- Modify: `src/stock_daily_report/indicators/technical.py`
- Test: `tests/test_market_scan_scoring.py`
- Test: `tests/test_technical_indicators.py`

**Step 1: Write the failing tests**

Create synthetic histories for:

- aligned rising trend;
- positive but overheated momentum;
- confirmed structure with moderate risk;
- high-volatility/deep-drawdown candidate;
- exact score ties.

Assert every component and total is finite and within `[0, 100]`, future bars
do not affect prior scores, trend profile favors clean momentum, balanced
profile rewards structural confirmation, and ties sort by code.

**Step 2: Verify failure**

Run:

```bash
pytest -q tests/test_market_scan_scoring.py tests/test_technical_indicators.py
```

Expected: fail because score models and MA-slope inputs do not exist.

**Step 3: Implement**

Add trailing MA slope metrics without centered windows. Implement fixed
`market-scan-v1` component functions and these weights:

```text
trend:    trend 45, momentum 30, volume 15, risk 10
balanced: trend 30, momentum 20, volume 15, structure 20, risk 15
```

Return component scores, evidence codes, and risk codes. Keep report language
descriptive.

**Step 4: Verify**

Run:

```bash
pytest -q tests/test_market_scan_scoring.py tests/test_technical_indicators.py
ruff check src/stock_daily_report/market_scan/scoring.py \
  src/stock_daily_report/indicators/technical.py
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/stock_daily_report/market_scan/scoring.py \
  src/stock_daily_report/indicators/technical.py \
  tests/test_market_scan_scoring.py tests/test_technical_indicators.py
git commit -m "feat: score market opportunities"
```

### Task 5: Build the failure-tolerant scanner and immutable artifact

**Files:**
- Create: `src/stock_daily_report/market_scan/models.py`
- Create: `src/stock_daily_report/market_scan/runner.py`
- Create: `src/stock_daily_report/market_scan/report.py`
- Test: `tests/test_market_scan_runner.py`
- Test: `tests/test_market_scan_report.py`

**Step 1: Write the failing tests**

Use fake universe/history providers to verify:

- bounded worker count;
- deterministic output despite completion order;
- individual failures are counted and do not abort;
- coverage below threshold suppresses both rankings;
- each ranking has at most 30 unique codes;
- consensus equals the exact set intersection;
- same-date identical artifact is reused;
- same-date different artifact raises a conflict.

**Step 2: Verify failure**

Run:

```bash
pytest -q tests/test_market_scan_runner.py tests/test_market_scan_report.py
```

Expected: fail because runner and report do not exist.

**Step 3: Implement**

Create schema-versioned JSON at:

```text
market-scans/YYYY-MM-DD/scan.json
```

Include universe/eligible/valid counts, coverage, exclusion/failure counts,
profile rankings, consensus records, config hash, input hash, provider names,
and generated timestamp. Use `ThreadPoolExecutor(max_workers=settings.max_workers)`
and collect results by code before sorting.

**Step 4: Verify**

Run:

```bash
pytest -q tests/test_market_scan_runner.py tests/test_market_scan_report.py
ruff check src/stock_daily_report/market_scan
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/stock_daily_report/market_scan tests/test_market_scan_runner.py \
  tests/test_market_scan_report.py
git commit -m "feat: run full-market opportunity scan"
```

### Task 6: Add CLI and a separate scheduled scan workflow

**Files:**
- Modify: `src/stock_daily_report/cli.py`
- Create: `.github/workflows/market-scan.yml`
- Test: `tests/test_market_scan_cli.py`
- Test: `tests/test_market_scan_workflow.py`

**Step 1: Write the failing tests**

Assert:

- `stock-daily-report market-scan` accepts date, settings, and output root;
- provider/config failures return exit code 1 without traceback;
- workflow uses Python 3.11 and locked dependencies;
- timeout and concurrency are explicit;
- scan artifacts persist independently;
- same-date reruns reuse immutable output.

**Step 2: Verify failure**

Run:

```bash
pytest -q tests/test_market_scan_cli.py tests/test_market_scan_workflow.py
```

Expected: fail because CLI command and workflow do not exist.

**Step 3: Implement**

Schedule the scanner before the daily report. Persist `market-scans/` to the
history branch. Do not make scanner failure block generation of the watchlist
report.

**Step 4: Verify**

Run:

```bash
pytest -q tests/test_market_scan_cli.py tests/test_market_scan_workflow.py
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/stock_daily_report/cli.py .github/workflows/market-scan.yml \
  tests/test_market_scan_cli.py tests/test_market_scan_workflow.py
git commit -m "feat: schedule full-market scan"
```

### Task 7: Integrate dual rankings into the daily report

**Files:**
- Modify: `src/stock_daily_report/report/models.py`
- Modify: `src/stock_daily_report/report/render.py`
- Modify: `src/stock_daily_report/pipeline.py`
- Modify: `site/styles.css`
- Test: `tests/test_report_render.py`
- Test: `tests/test_pipeline.py`

**Step 1: Write the failing tests**

Verify JSON, Markdown, and HTML include:

- trend Top 30;
- balanced Top 30;
- highlighted consensus rows with both ranks and scores;
- component evidence and risks;
- coverage metadata;
- explicit unavailable state when no valid scan exists;
- no “high confidence” wording before the backtest gate.

**Step 2: Verify failure**

Run:

```bash
pytest -q tests/test_report_render.py tests/test_pipeline.py -k market_scan
```

Expected: fail because report models have no market rankings.

**Step 3: Implement**

Extend the report schema version and renderers. The pipeline loads only a
same-date validated scan artifact. Missing or invalid scan data produces an
unavailable section but does not fail the watchlist publication.

**Step 4: Verify**

Run:

```bash
pytest -q tests/test_report_render.py tests/test_pipeline.py -k market_scan
ruff check src/stock_daily_report/report src/stock_daily_report/pipeline.py
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/stock_daily_report/report src/stock_daily_report/pipeline.py \
  site/styles.css tests/test_report_render.py tests/test_pipeline.py
git commit -m "feat: publish dual market rankings"
```

### Task 8: Backtest ranking groups without overclaiming consensus

**Files:**
- Create: `src/stock_daily_report/backtest/ranking_groups.py`
- Modify: `src/stock_daily_report/backtest/runner.py`
- Modify: `src/stock_daily_report/backtest/report.py`
- Test: `tests/test_ranking_group_backtest.py`

**Step 1: Write the failing tests**

Replay daily membership and verify mutually exclusive groups:

```text
trend-only
balanced-only
consensus
```

Assert 1/5/20-day metrics, sample thresholds, no future membership leakage,
and `not_enough_evidence` for undersized groups.

**Step 2: Verify failure**

Run:

```bash
pytest -q tests/test_ranking_group_backtest.py
```

Expected: fail because ranking-group evaluation does not exist.

**Step 3: Implement**

Reuse execution cost assumptions and evaluation metrics. Do not label
consensus as higher confidence in production output; report only comparative
evidence.

**Step 4: Verify**

Run:

```bash
pytest -q tests/test_ranking_group_backtest.py
```

Expected: pass.

**Step 5: Commit**

```bash
git add src/stock_daily_report/backtest tests/test_ranking_group_backtest.py
git commit -m "feat: compare ranking groups out of sample"
```

### Task 9: Document, review, and validate end to end

**Files:**
- Modify: `README.md`
- Modify: `PROJECT_STATUS.md`
- Test: all affected tests

**Step 1: Update documentation**

Document YAML watchlist ownership, full-market filtering, fixed score weights,
Top 30 limits, consensus semantics, coverage gates, commands, workflows, and
research-only boundaries.

**Step 2: Run targeted integration**

Run a small injected/fixture universe and inspect:

```text
market-scans/YYYY-MM-DD/scan.json
reports/YYYY-MM-DD/report.json
reports/YYYY-MM-DD/report.md
reports/YYYY-MM-DD/index.html
```

Expected: both rankings contain stable ordering and the consensus set is
highlighted consistently in every format.

**Step 3: Run full validation**

```bash
pytest -q
ruff check .
git diff --check
```

Expected: all pass.

**Step 4: Request code review**

Review correctness, lookahead safety, failure isolation, scoring boundaries,
workflow persistence, and wording.

**Step 5: Commit**

```bash
git add README.md PROJECT_STATUS.md
git commit -m "docs: explain full-market opportunity rankings"
```

