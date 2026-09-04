# A-Share Daily Report Implementation Plan

> **For Copilot:** Use executing-plans skill to implement this plan task-by-task.

**Goal:** Build a GitHub Actions-operated A-share daily research report that delivers explainable rule-based analysis and WeCom/Feishu summaries, while measuring a simplified Chan-style structural analyzer against a transparent strict-rule baseline through no-lookahead replay and out-of-sample backtests.

**Architecture:** A scheduled Python pipeline loads a versioned watchlist, retrieves and normalizes daily A-share data, validates it, and writes immutable input snapshots. It runs technical/risk analysis plus separate `simplified` and `strict` structural analyzers, renders JSON/Markdown/HTML reports, and sends compact webhook notifications. A separately scheduled research pipeline replays one bar at a time, preserves signal confirmation times, and produces versioned comparison metrics; it must never block the daily report.

**Tech Stack:** Python 3.11, pandas, numpy, Pydantic, HTTPX, PyYAML, pytest, ruff, GitHub Actions, GitHub Pages, WeCom robot webhook, Feishu robot webhook. SQLite is deferred; JSON snapshots and reports are the initial persistent store.

---

## Product Definition

### P0 features

1. Versioned A-share watchlist with code, name, group, tags, and per-stock news enablement.
2. Pluggable daily-bar provider with a cached primary provider, a typed fallback, and a deterministic fixture provider for tests.
3. Data-quality checks for duplicate dates, missing OHLC fields, invalid prices, stale data, and too-short history.
4. Deterministic technical analysis: moving averages, MACD, RSI, return windows, drawdown, volume changes, and support/resistance candidates.
5. Two independent structural analyzers:
   - `simplified`: confirmed fractals, confirmed strokes, simplified central-area candidates, trend/risk labels.
   - `strict`: a documented and versioned rule profile for inclusion, fractal confirmation, stroke validation, segment construction, central-area extension, and structured buy/sell candidates.
6. A risk rules engine that can mark overextension, drawdown, poor data quality, high volatility, and incomplete structures.
7. Structured JSON, Markdown, and static HTML daily reports, published to GitHub Pages.
8. Summary notifications via both WeCom and Feishu robot webhooks.
9. A no-lookahead replay and out-of-sample comparison report measuring structure agreement, confirmation delay, confirmed-structure rewrite rate, and post-signal returns after execution constraints.

### Explicit non-goals for the first release

- Automatic trading, brokerage integration, portfolio execution, or investment advice.
- Intraday/high-frequency refreshes, a WebUI, user accounts, or a database-backed configuration service.
- Hong Kong, US, crypto, Japan, Korea, Taiwan, or cross-market mapping.
- Treating LLM prose as a trading signal or including LLM output in backtest entry conditions.
- Claiming a unique or canonical implementation of the original Chan theory.

### Capability boundaries

The product may summarize data-backed evidence and classify states as `偏强`, `观察`, `偏弱`, `风险升高`, or `等待确认`. It must show data timestamp, rule version, structure confirmation state, and invalidation/observation conditions. It must not use terms such as “guaranteed”, “must rise”, or “confirmed buy” in push messages. Free data source availability is best-effort; failed validation is a reported pipeline failure, never a success-shaped empty report.

## Benefits, Tradeoffs, and Acceptance Criteria

| Area | Benefit | Cost / risk | Initial acceptance criterion |
|---|---|---|---|
| Rule-first reports | Reproducible without an LLM | Less conversational detail | A report renders with no API key |
| Dual analyzers | Makes simplification measurable | Strict profile is more complex | Analyzer versions and inputs are recorded |
| Incremental replay | Prevents hidden future knowledge | More compute than batch labeling | Every signal has `formed_at`, `confirmed_at`, `tradable_at` |
| JSON snapshots | Easy audit trail and Pages publishing | Storage grows over time | Every report references its input snapshot |
| WeCom + Feishu | Redundant delivery channels | Different payload limits and webhook formats | One compact summary reaches each enabled channel |
| GitHub Actions | No server for scheduled jobs | Runtime/network/API limits | Daily job completes within the configured timeout |

Do not optimize the simplified analyzer merely to match the strict analyzer. Upgrade it only after a versioned comparison shows material degradation in structure agreement, confirmation lag, rewrite rate, or out-of-sample risk-adjusted outcomes. “Material” must be defined in the comparison configuration rather than inferred after seeing results.

## Project Layout

```text
stock-daily-report/
├── .github/workflows/
│   ├── daily-report.yml
│   └── structural-backtest.yml
├── config/
│   ├── settings.yaml
│   ├── watchlist.yaml
│   └── strict_chan_rules.yaml
├── src/stock_daily_report/
│   ├── cli.py
│   ├── models.py
│   ├── config.py
│   ├── pipeline.py
│   ├── providers/
│   ├── quality/
│   ├── indicators/
│   ├── chan/
│   ├── risk/
│   ├── report/
│   ├── notify/
│   └── backtest/
├── tests/
├── fixtures/
├── reports/                    # generated, ignored by Git
├── snapshots/                  # generated, ignored by Git
├── site/                       # static report shell, committed
├── scripts/
├── pyproject.toml
└── README.md
```

## Implementation Tasks

### Task 1: Bootstrap the package and deterministic configuration

**Files:**
- Create: `pyproject.toml`
- Create: `src/stock_daily_report/__init__.py`
- Create: `src/stock_daily_report/models.py`
- Create: `src/stock_daily_report/config.py`
- Create: `config/settings.yaml`
- Create: `config/watchlist.yaml`
- Create: `tests/test_config.py`

**Step 1: Write the failing test**

```python
from stock_daily_report.config import load_settings, load_watchlist


def test_load_watchlist_returns_valid_a_share_codes(tmp_path):
    path = tmp_path / "watchlist.yaml"
    path.write_text("stocks:\n  - code: '600519'\n    name: 贵州茅台\n", encoding="utf-8")

    watchlist = load_watchlist(path)

    assert watchlist.stocks[0].code == "600519"
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py::test_load_watchlist_returns_valid_a_share_codes -v`

Expected: FAIL because the package and configuration loader do not exist.

**Step 3: Write minimal implementation**

Define Pydantic models for `WatchlistStock`, `Watchlist`, `Settings`, `NotificationSettings`, and a `RuleVersion`. Reject missing codes, non-six-digit mainland A-share codes, empty names, duplicate codes, and notification configurations with enabled channels but missing webhooks. Use `yaml.safe_load`; never load arbitrary YAML objects.

```python
class WatchlistStock(BaseModel):
    code: str = Field(pattern=r"^\d{6}$")
    name: str
    group: str = "default"
    tags: list[str] = []
    news_enabled: bool = True
```

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_config.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add pyproject.toml config src/stock_daily_report tests/test_config.py
git commit -m "feat: add typed report configuration"
```

### Task 2: Define normalized market data and snapshot persistence

**Files:**
- Create: `src/stock_daily_report/providers/base.py`
- Create: `src/stock_daily_report/providers/fixture.py`
- Create: `src/stock_daily_report/snapshots.py`
- Create: `fixtures/bars/600519.csv`
- Create: `tests/test_snapshots.py`

**Step 1: Write the failing test**

```python
def test_snapshot_round_trip_preserves_content_hash(tmp_path, bars):
    path = write_snapshot(tmp_path, report_date=date(2026, 9, 4), bars_by_code={"600519": bars})

    snapshot = load_snapshot(path)

    assert snapshot.content_hash
    assert snapshot.bars_by_code["600519"][-1].close == bars[-1].close
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_snapshots.py::test_snapshot_round_trip_preserves_content_hash -v`

Expected: FAIL because snapshot functions do not exist.

**Step 3: Write minimal implementation**

Use a `DailyBar` model with `trade_date`, OHLC, volume, amount, turnover rate, adjustment mode, provider name, and source timestamp. Persist only normalized, validated bars to `snapshots/YYYY-MM-DD/input.json`; include schema version, provider metadata, generation time, SHA-256 content hash, and a sorted code list. Implement a fixture provider used by all offline tests.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_snapshots.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/stock_daily_report/providers src/stock_daily_report/snapshots.py fixtures tests/test_snapshots.py
git commit -m "feat: add normalized data snapshots"
```

### Task 3: Add provider fallback and data-quality gates

**Files:**
- Create: `src/stock_daily_report/providers/akshare.py`
- Create: `src/stock_daily_report/providers/service.py`
- Create: `src/stock_daily_report/quality/checks.py`
- Create: `tests/test_quality_checks.py`
- Modify: `config/settings.yaml`

**Step 1: Write the failing test**

```python
def test_quality_gate_rejects_duplicate_trading_dates(bars):
    duplicated = [*bars, bars[-1]]

    result = validate_bars("600519", duplicated, as_of=date(2026, 9, 4))

    assert result.is_valid is False
    assert "duplicate_trade_date" in result.issue_codes
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_quality_checks.py::test_quality_gate_rejects_duplicate_trading_dates -v`

Expected: FAIL because `validate_bars` does not exist.

**Step 3: Write minimal implementation**

Implement provider selection in this fixed order: configured primary provider, configured fallback provider, then fail. Do not silently substitute partial data. Validate chronological ordering, duplicate dates, missing/negative OHLC values, high lower than low, close outside low/high, stale last trading date, and minimum historical bar count. Return typed `DataQualityResult` containing every issue and whether the stock may enter analysis. Cache raw provider responses only after redacting credentials; cache expiration must be configurable.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_quality_checks.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/stock_daily_report/providers src/stock_daily_report/quality config/settings.yaml tests/test_quality_checks.py
git commit -m "feat: validate market data before analysis"
```

### Task 4: Implement deterministic indicators and trend labels

**Files:**
- Create: `src/stock_daily_report/indicators/technical.py`
- Create: `src/stock_daily_report/indicators/trend.py`
- Create: `tests/test_technical_indicators.py`
- Create: `tests/test_trend_labels.py`

**Step 1: Write the failing tests**

```python
def test_technical_metrics_calculates_known_moving_average(bars):
    metrics = calculate_technical_metrics(bars)
    assert metrics.ma20 is not None


def test_trend_label_is_waiting_when_history_is_insufficient(short_bars):
    assert classify_trend(short_bars).label == "等待确认"
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_technical_indicators.py tests/test_trend_labels.py -v`

Expected: FAIL because indicator functions do not exist.

**Step 3: Write minimal implementation**

Calculate MA5/10/20/60/120, MACD(12,26,9), RSI(14), 20/60/120-day returns, 20-day realized volatility, 60-day drawdown, volume ratio, and recent extrema from validated adjusted bars. Define trend labels from explicit rule predicates and emit their evidence, not just a score. Do not use future bars, centered windows, or unbounded forward fill.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_technical_indicators.py tests/test_trend_labels.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/stock_daily_report/indicators tests/test_technical_indicators.py tests/test_trend_labels.py
git commit -m "feat: add deterministic technical analysis"
```

### Task 5: Implement the simplified structural analyzer

**Files:**
- Create: `src/stock_daily_report/chan/common.py`
- Create: `src/stock_daily_report/chan/simplified.py`
- Create: `tests/test_simplified_chan.py`
- Create: `fixtures/chan/simplified_cases.json`

**Step 1: Write the failing test**

```python
def test_simplified_analyzer_marks_fractal_after_right_bar_confirms(case_bars):
    result = SimplifiedChanAnalyzer().analyze(case_bars)

    bottom = next(item for item in result.fractals if item.kind == "bottom")
    assert bottom.confirmed_at > bottom.formed_at
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_simplified_chan.py::test_simplified_analyzer_marks_fractal_after_right_bar_confirms -v`

Expected: FAIL because the analyzer does not exist.

**Step 3: Write minimal implementation**

Implement inclusion processing, three-bar candidate fractals, confirmation with the right-side bar, alternating confirmed strokes, simplified overlapping central-area candidates, structure state, and support/resistance candidates. Every structural item must include `formed_at`, `confirmed_at`, `tradable_at`, `status` (`candidate` or `confirmed`), and `rule_version="simplified-v1"`. Do not emit “一买/二买/三买”; use neutral observations such as `potential_central_breakout` and `pullback_holds_above_central_range`.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_simplified_chan.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/stock_daily_report/chan fixtures/chan tests/test_simplified_chan.py
git commit -m "feat: add confirmed simplified market structures"
```

### Task 6: Add risk rules and decision labels

**Files:**
- Create: `src/stock_daily_report/risk/rules.py`
- Create: `src/stock_daily_report/decision.py`
- Create: `tests/test_risk_rules.py`

**Step 1: Write the failing test**

```python
def test_overextension_marks_risk_increased(metrics, structure):
    decision = decide(metrics=metrics, structure=structure, quality=valid_quality())
    assert decision.label == "风险升高"
    assert "overextended_from_ma20" in decision.risk_codes
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_risk_rules.py::test_overextension_marks_risk_increased -v`

Expected: FAIL because the decision engine does not exist.

**Step 3: Write minimal implementation**

Define versioned risk rules for invalid/stale data, high realized volatility, extended price distance from MA20, large drawdown, incomplete structure, and adverse volume behavior. Produce one allowed decision label, evidence list, risk list, key price levels, and explicitly observable next conditions. Risk rules override bullish labels. Store thresholds in `config/settings.yaml` and include the configuration hash in outputs.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_risk_rules.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/stock_daily_report/risk src/stock_daily_report/decision.py tests/test_risk_rules.py
git commit -m "feat: add explainable risk-first decisions"
```

### Task 7: Render report artifacts and build the daily pipeline

**Files:**
- Create: `src/stock_daily_report/report/models.py`
- Create: `src/stock_daily_report/report/render.py`
- Create: `src/stock_daily_report/pipeline.py`
- Create: `src/stock_daily_report/cli.py`
- Create: `site/index.html`
- Create: `site/styles.css`
- Create: `tests/test_pipeline.py`
- Create: `tests/test_report_render.py`

**Step 1: Write the failing test**

```python
def test_daily_pipeline_writes_json_markdown_and_html(tmp_path, fixture_settings):
    outputs = run_daily_report(fixture_settings, output_root=tmp_path)

    assert outputs.json_path.exists()
    assert outputs.markdown_path.exists()
    assert outputs.html_path.exists()
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_pipeline.py::test_daily_pipeline_writes_json_markdown_and_html -v`

Expected: FAIL because the pipeline does not exist.

**Step 3: Write minimal implementation**

The CLI command is `python -m stock_daily_report.cli daily --date YYYY-MM-DD`. It must fetch/validate all watchlist data before publishing any artifact. Write `reports/YYYY-MM-DD/report.json`, `report.md`, and `index.html`, referencing `snapshots/YYYY-MM-DD/input.json`, provider metadata, data timestamp, config hash, and analyzer versions. Render a market-summary placeholder only from available validated inputs; do not invent breadth or sector figures. Create a static index that links dated reports.

Task 7 publication transactions acquire the stable per-date `.input.lock`
before the stable site-wide `site/.publication.lock`, and never remove either
lock file. Report, snapshot, and shared-site updates are rolled back while
those locks are held. Deferred cache responses are committed only after
publication finalization and are protected by a stable cache lock with
preimage restoration for partial failures.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pipeline.py tests/test_report_render.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/stock_daily_report/report src/stock_daily_report/pipeline.py src/stock_daily_report/cli.py site tests
git commit -m "feat: generate auditable daily report artifacts"
```

### Task 8: Implement WeCom and Feishu notification adapters

**Files:**
- Create: `src/stock_daily_report/notify/base.py`
- Create: `src/stock_daily_report/notify/wecom.py`
- Create: `src/stock_daily_report/notify/feishu.py`
- Create: `src/stock_daily_report/notify/service.py`
- Create: `tests/test_wecom_notify.py`
- Create: `tests/test_feishu_notify.py`
- Modify: `src/stock_daily_report/pipeline.py`

**Step 1: Write the failing test**

```python
def test_wecom_summary_contains_report_link_and_no_full_report():
    payload = WeComNotifier("https://example.invalid").build_payload(summary)
    content = payload["markdown"]["content"]
    assert summary.report_url in content
    assert len(content) < 4096
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_wecom_notify.py tests/test_feishu_notify.py -v`

Expected: FAIL because the notification adapters do not exist.

**Step 3: Write minimal implementation**

Use typed notifier interfaces and HTTPX timeouts. Build provider-specific payloads with title, data timestamp, counts by decision label, at most five focus items, all high risks, and the Pages report link. Read webhook URLs only from environment variables `WECOM_WEBHOOK_URL` and `FEISHU_WEBHOOK_URL`, never from configuration files or logs. Handle each enabled channel independently, collect channel-specific failures, retry only transient HTTP/network failures with bounded exponential backoff, and fail the workflow after recording outcomes if any configured channel cannot deliver.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_wecom_notify.py tests/test_feishu_notify.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/stock_daily_report/notify src/stock_daily_report/pipeline.py tests/test_wecom_notify.py tests/test_feishu_notify.py
git commit -m "feat: notify WeCom and Feishu daily summaries"
```

### Task 9: Add GitHub Actions and GitHub Pages publication

**Files:**
- Create: `.github/workflows/daily-report.yml`
- Create: `scripts/publish_site.py`
- Modify: `.gitignore`
- Modify: `README.md`

**Step 1: Write the workflow validation fixture**

Create `tests/test_publish_site.py` to verify that a dated HTML report and its JSON data are copied into the Pages staging directory without copying snapshots, `.env` files, or cache files.

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_publish_site.py -v`

Expected: FAIL because the publishing script does not exist.

**Step 3: Write minimal implementation**

The daily workflow runs on China trading weekdays after market close, with `workflow_dispatch` for controlled manual runs. Set `TZ=Asia/Shanghai`, use Python 3.11, install locked dependencies, run `daily`, publish static artifacts to GitHub Pages, then send notifications only after publication succeeds. Store webhook URLs as repository secrets. Set explicit timeouts and concurrency so overlapping reports cannot publish out of order. Retain no credentials in artifacts. Include a README table for required and optional secrets.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_publish_site.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add .github/workflows scripts/publish_site.py .gitignore README.md tests/test_publish_site.py
git commit -m "ci: schedule report publication and notification"
```

### Task 10: Specify and implement the strict-rule structural baseline

**Files:**
- Create: `config/strict_chan_rules.yaml`
- Create: `docs/chan-strict-profile.md`
- Create: `src/stock_daily_report/chan/strict.py`
- Create: `tests/test_strict_chan.py`
- Create: `fixtures/chan/strict_cases.json`

**Step 1: Write the failing test**

```python
def test_strict_analyzer_never_marks_signal_tradeable_before_confirmation(case_bars):
    result = StrictChanAnalyzer(load_strict_profile()).analyze(case_bars)

    assert all(item.tradable_at >= item.confirmed_at for item in result.all_items)
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_strict_chan.py::test_strict_analyzer_never_marks_signal_tradeable_before_confirmation -v`

Expected: FAIL because the strict analyzer and profile do not exist.

**Step 3: Write minimal implementation**

Before coding, document exact rules—not generic “original Chan theory”—for inclusion direction, equality handling, fractal confirmation, minimum stroke requirements, segment feature sequence and termination, central-area formation/extension, and structured buy/sell candidate confirmation. Version every profile change. The analyzer must have a distinct implementation path from `simplified.py`; do not make it a threshold mode of the same function. Its output must preserve all event timestamps and reason codes. The documentation must explicitly state this is a reproducible strict interpretation, not a claim of unique orthodoxy.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_strict_chan.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add config/strict_chan_rules.yaml docs/chan-strict-profile.md src/stock_daily_report/chan/strict.py fixtures/chan tests/test_strict_chan.py
git commit -m "feat: add versioned strict structural baseline"
```

### Task 11: Build no-lookahead replay and structure-comparison metrics

**Files:**
- Create: `src/stock_daily_report/backtest/replay.py`
- Create: `src/stock_daily_report/backtest/structure_metrics.py`
- Create: `src/stock_daily_report/backtest/report.py`
- Create: `tests/test_replay.py`
- Create: `tests/test_structure_metrics.py`

**Step 1: Write the failing test**

```python
def test_replay_only_exposes_bars_available_on_current_date(case_bars):
    events = replay(case_bars, analyzer=SimplifiedChanAnalyzer())

    assert all(event.max_input_date <= event.observed_at for event in events)
```

**Step 2: Run tests to verify they fail**

Run: `pytest tests/test_replay.py tests/test_structure_metrics.py -v`

Expected: FAIL because replay and metrics do not exist.

**Step 3: Write minimal implementation**

Replay bars one at a time, rerunning each analyzer only against the prefix available at that timestamp. Record immutable events, current visible state, and later revisions. Measure fractal/stroke/central-area match precision, recall, and F1 with an explicitly configured time/price tolerance; confirmation-lag distribution; and rewrite rate for items that were previously marked confirmed. Write results under `reports/backtests/YYYY-MM-DD/` with snapshot and analyzer profile hashes. This task establishes structural fidelity metrics only; it must not calculate strategy performance yet.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_replay.py tests/test_structure_metrics.py -v`

Expected: PASS.

**Step 5: Commit**

```bash
git add src/stock_daily_report/backtest tests/test_replay.py tests/test_structure_metrics.py
git commit -m "feat: replay structures without future data"
```

### Task 12: Add execution-aware out-of-sample comparison backtest

**Files:**
- Create: `config/backtest.yaml`
- Create: `src/stock_daily_report/backtest/execution.py`
- Create: `src/stock_daily_report/backtest/evaluate.py`
- Create: `tests/test_backtest_execution.py`
- Create: `.github/workflows/structural-backtest.yml`
- Modify: `README.md`

**Step 1: Write the failing test**

```python
def test_signal_executes_at_next_eligible_bar_after_tradable_time(signal, bars):
    trade = execute_signal(signal, bars, costs=Costs(commission_bps=3, slippage_bps=5))

    assert trade.entry_date > signal.tradable_at.date()
    assert trade.entry_price > 0
```

**Step 2: Run test to verify it fails**

Run: `pytest tests/test_backtest_execution.py::test_signal_executes_at_next_eligible_bar_after_tradable_time -v`

Expected: FAIL because execution logic does not exist.

**Step 3: Write minimal implementation**

Use only signals emitted by the replay engine after `tradable_at`. Execute at the configured next eligible daily-bar price; model commission, slippage, suspended symbols, and A-share limit-up/limit-down restrictions according to explicit configuration. Split dates into calibration and untouched out-of-sample windows. Compare simplified, strict, equal-weight watchlist, and a declared index baseline on 1/5/20-day returns, hit rate, maximum drawdown, turnover, and risk-adjusted return. Publish uncertainty: sample count, confidence intervals or bootstrap intervals, and a “not enough evidence” state. The weekly workflow is separate from the daily reporting workflow.

**Step 4: Run tests to verify they pass**

Run: `pytest tests/test_backtest_execution.py -v && pytest -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add config/backtest.yaml src/stock_daily_report/backtest .github/workflows/structural-backtest.yml README.md tests/test_backtest_execution.py
git commit -m "feat: compare structural analyzers out of sample"
```

## Verification Checklist

1. Run `pytest -q` and `ruff check .` locally before enabling workflows.
2. Run `python -m stock_daily_report.cli daily --provider fixture --date 2026-09-04` and inspect that JSON, Markdown, HTML, snapshot hashes, and rule versions agree.
3. Run `python -m stock_daily_report.cli replay --provider fixture` and verify no event consumes a bar dated after its observation time.
4. Run `python -m stock_daily_report.cli backtest --provider fixture` and verify execution occurs only after `tradable_at`.
5. Trigger the daily GitHub Actions workflow manually with test webhooks, then confirm Pages publication precedes both summary notifications.
6. Force a fixture data-quality error and a temporary webhook error; confirm both produce explicit failed outcomes and no success-shaped report/notification.

## Future Upgrade Gate

Do not change simplified structural rules merely because a historical chart appears visually different. Open an optimization task only when a versioned out-of-sample report shows one or more configured material gaps versus the strict baseline, and the gap persists across multiple symbols and market regimes. Each upgrade must include a new fixture or manually reviewed annotation, a no-lookahead regression test, a profile/version bump, and a before/after comparison artifact.
