# A 股大盘状态与移动端 Panel Implementation Plan

> **For Copilot:** Use executing-plans skill to implement this plan task-by-task.

**Goal:** 在日报开头增加可审计的 A 股大盘状态，并将 HTML 报告拆成适合手机阅读的六个 panel。

**Architecture:** 全市场扫描在同一份不可变 artifact 中保存指数表现与全市场广度，日报 pipeline 只读取该 artifact。HTML 使用横向导航加原生 `<details>` 折叠 panel，默认展开大盘；Markdown 保持线性章节，JSON 保留原始字段、规则版本和数据源。

**Tech Stack:** Python 3.11+, Pydantic v2, AkShare adapters, PyYAML, deterministic Markdown/HTML rendering, pytest, Ruff.

---

### Task 1: Extend market-state configuration and typed models

**Files:**
- Modify: `src/stock_daily_report/models.py`
- Modify: `src/stock_daily_report/market_scan/models.py`
- Modify: `config/market_scan.yaml`
- Test: `tests/test_config.py`
- Test: `tests/test_market_scan_models.py`

**Step 1: Write failing tests**

Add tests for the five configured index identifiers, strict configuration validation, `MarketState` index/breadth fields, unavailable/partial states, finite numeric values, report-date consistency, and backward loading of a scan artifact without `market_state`.

**Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_config.py tests/test_market_scan_models.py -q
```

Expected: FAIL because the index configuration and market-state models do not exist.

**Step 3: Implement minimal models**

Add a frozen `MarketStateSettings` with the five index codes, lookback periods, and rule version. Add frozen `MarketState`, `MarketIndexState`, and `MarketBreadth` models with explicit nullable fields for provider omissions and stable `available`, `partial`, and `unavailable` states. Add `market_state` as an optional field on `MarketScanArtifact` and include the settings in its configuration hash input without changing old artifact schema compatibility.

**Step 4: Run tests to verify they pass**

Run the same pytest command. Expected: PASS.

**Step 5: Commit**

```bash
git add src/stock_daily_report/models.py src/stock_daily_report/market_scan/models.py config/market_scan.yaml tests/test_config.py tests/test_market_scan_models.py
git commit -m "feat: add market state models"
```

### Task 2: Add index history provider and breadth calculation

**Files:**
- Create: `src/stock_daily_report/providers/index.py`
- Create: `src/stock_daily_report/market_scan/state.py`
- Modify: `src/stock_daily_report/providers/service.py`
- Test: `tests/test_index_provider.py`
- Test: `tests/test_market_state.py`

**Step 1: Write failing tests**

Cover AkShare index field mapping, end-date truncation, missing/invalid rows, provider availability errors, 20/60-day moving averages, index trend classification, breadth counts from `UniverseQuote.change_pct`, and classification cases for aligned, divergent, and insufficient inputs.

**Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_index_provider.py tests/test_market_state.py -q
```

Expected: FAIL because the provider and state calculator are absent.

**Step 3: Implement minimal provider and calculator**

Implement a provider contract dedicated to index history so stock code normalization is not reused incorrectly. Normalize AkShare index records into `DailyBar`, reject future rows, and surface provider/code/reason errors. Implement pure state calculation functions that accept the same quote snapshot and normalized index bars, preserving partial results when one input fails.

**Step 4: Run tests to verify they pass**

Run the same pytest command. Expected: PASS.

**Step 5: Commit**

```bash
git add src/stock_daily_report/providers/index.py src/stock_daily_report/market_scan/state.py src/stock_daily_report/providers/service.py tests/test_index_provider.py tests/test_market_state.py
git commit -m "feat: calculate index and breadth market state"
```

### Task 3: Persist market state in the full-market scan

**Files:**
- Modify: `src/stock_daily_report/market_scan/runner.py`
- Modify: `src/stock_daily_report/market_scan/report.py`
- Modify: `src/stock_daily_report/cli.py`
- Modify: `src/stock_daily_report/market_scan/resumable.py`
- Test: `tests/test_market_scan_runner.py`
- Test: `tests/test_market_scan_cli.py`
- Test: `tests/test_market_scan_resume.py`

**Step 1: Write failing tests**

Verify a scan writes market state from the same quote snapshot, isolates index-provider failures, includes market-state configuration in artifact identity, preserves state when resuming from the same quote snapshot, and rejects reuse when the state rule/configuration changes.

**Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_market_scan_runner.py tests/test_market_scan_cli.py tests/test_market_scan_resume.py -q
```

Expected: FAIL because the scan runner does not calculate or persist market state.

**Step 3: Implement scan integration**

Load market-state settings with the scan settings, compute breadth before candidate history processing, fetch configured index histories with the existing hard-timeout/failure-isolation boundary, attach the resulting state to `MarketScanArtifact`, and include the state inputs in config/input hashes and resumable checkpoint metadata. Keep a scan usable when state is partial or unavailable.

**Step 4: Run tests to verify they pass**

Run the same pytest command. Expected: PASS.

**Step 5: Commit**

```bash
git add src/stock_daily_report/market_scan/runner.py src/stock_daily_report/market_scan/report.py src/stock_daily_report/cli.py src/stock_daily_report/market_scan/resumable.py tests/test_market_scan_runner.py tests/test_market_scan_cli.py tests/test_market_scan_resume.py
git commit -m "feat: persist market state with market scans"
```

### Task 4: Render the market panel and six mobile panels

**Files:**
- Modify: `src/stock_daily_report/report/models.py`
- Modify: `src/stock_daily_report/pipeline.py`
- Modify: `src/stock_daily_report/report/render.py`
- Modify: `site/styles.css`
- Test: `tests/test_report_render.py`
- Test: `tests/test_pipeline.py`

**Step 1: Write failing tests**

Assert that Markdown includes the market-state summary before rankings and stock sections. Assert that HTML contains exactly six ordered panel navigation entries: 大盘、多策略共识、趋势策略、均衡策略、自选股追踪、全池速览; only 大盘 has `open`; each navigation item has a safe anchor; missing state renders an explicit unavailable message; and untrusted names/provider text remain escaped.

**Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_report_render.py tests/test_pipeline.py -q
```

Expected: FAIL because market state and panel markup are absent.

**Step 3: Implement deterministic renderers**

Map the artifact state into `ReportDocument`, add readable Chinese labels and concise index/breadth summaries, split current combined ranking HTML into three panels, wrap self-selected stocks and pool overview in the remaining panels, and add responsive CSS for a horizontally scrollable navigation and compact mobile cards. Use native details/summary only; do not add JavaScript.

**Step 4: Run tests to verify they pass**

Run the same pytest command. Expected: PASS.

**Step 5: Commit**

```bash
git add src/stock_daily_report/report/models.py src/stock_daily_report/pipeline.py src/stock_daily_report/report/render.py site/styles.css tests/test_report_render.py tests/test_pipeline.py
git commit -m "feat: add market state and mobile report panels"
```

### Task 5: Invalidate stale reports and verify workflow integration

**Files:**
- Modify: `.github/workflows/daily-report.yml`
- Modify: `src/stock_daily_report/report/models.py`
- Modify: `tests/test_daily_workflow.py`
- Modify: `tests/test_report_render.py`

**Step 1: Write failing tests**

Assert that report reuse requires the market-state rule/configuration and renderer version to match, while old reports without market state are regenerated or rendered as unavailable according to the documented path.

**Step 2: Run tests to verify they fail**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_daily_workflow.py tests/test_report_render.py -q
```

Expected: FAIL because workflow reuse currently checks only existing scan and renderer metadata.

**Step 3: Implement reuse validation**

Add a market-state rule/configuration fingerprint check to the existing immutable report reuse block and update the renderer version. Keep report-history merge behavior unchanged and make missing/invalid state cause regeneration rather than silent reuse.

**Step 4: Run targeted and complete tests**

Run:

```bash
PYTHONPATH=src .venv/bin/pytest tests/test_daily_workflow.py tests/test_report_render.py -q
PYTHONPATH=src .venv/bin/pytest -q
.venv/bin/ruff check src tests
git diff --check
```

Expected: targeted tests pass, full suite reports all tests passed except documented skips, Ruff passes, and diff check is clean.

**Step 5: Commit**

```bash
git add .github/workflows/daily-report.yml src/stock_daily_report/report/models.py tests/test_daily_workflow.py tests/test_report_render.py
git commit -m "ci: invalidate reports when market state changes"
```
