# Portfolio Backtest Implementation Plan

> **For Copilot:** Use executing-plans skill to implement this plan task-by-task.

**Goal:** Add an offline, auditable portfolio backtest that evaluates the existing market-scan scoring rules against an equal-weight buy-and-hold baseline.

**Architecture:** Recompute each watchlist member's existing `market-scan-v1` score from bars available as of each historical close, rank the configured profile, and rebalance an equal-weight portfolio at the next session's open. Keep the simulator deterministic and separate from signal generation; emit a versioned JSON report containing assumptions, trade/equity data, baseline metrics, and evidence status.

**Tech Stack:** Python 3.11, Pydantic, existing market-scan scoring/filter modules, normalized DailyBar CSV, pytest, Ruff.

---

### Task 1: Define portfolio backtest assumptions

**Files:**
- Modify: `src/stock_daily_report/models.py`
- Modify: `src/stock_daily_report/config.py`
- Create: `config/portfolio_backtest.yaml`
- Test: `tests/test_config.py`

**Steps:**
1. Add failing tests for accepted portfolio settings and invalid position count, profile, cadence, capital, and costs.
2. Run the targeted tests and verify they fail because the model/config loader is missing.
3. Add a strict `PortfolioBacktestSettings` model with profile, maximum holdings, session-based rebalance interval, initial cash, holdout start, minimum sample count, and the existing backtest cost model.
4. Add `load_portfolio_backtest_settings` and a conservative example configuration.
5. Run the targeted config tests and Ruff.

### Task 2: Implement deterministic portfolio simulation

**Files:**
- Create: `src/stock_daily_report/backtest/portfolio.py`
- Test: `tests/test_portfolio_backtest.py`

**Steps:**
1. Add failing tests for next-session execution, equal-weight sizing in 100-share lots, cost deductions, rebalancing, blocked buys at limit-up, blocked sells at limit-down/suspension, equity marking, baseline returns, and invalid/misaligned inputs.
2. Run the focused test file and verify failures occur at the missing simulator API.
3. Implement a pure simulator over precomputed date-to-target mappings and per-symbol bars. Keep blocked positions until executable; never fabricate fills for missing/suspended bars.
4. Report daily portfolio equity and equal-weight buy-and-hold baseline equity, returns, annualized volatility/Sharpe, drawdown, turnover, and executed/blocked orders.
5. Run the focused tests, then Ruff.

### Task 3: Generate point-in-time rankings and report

**Files:**
- Create: `src/stock_daily_report/backtest/portfolio_runner.py`
- Test: `tests/test_portfolio_backtest_runner.py`

**Steps:**
1. Add a failing integration test that supplies short deterministic bars and proves rankings on date `t` never include bars later than `t`, trades can only begin on the next available session, and the fixture report is marked `not_enough_evidence`.
2. Run the test and verify it fails because no portfolio runner exists.
3. Reuse `filter_universe_quote`, `filter_history`, `score_candidate`, and `rank_scores` for each as-of date; pass only each symbol's historical prefix.
4. Add an auditable report with settings, input hash, point-in-time signal dates, portfolio and baseline metrics, evidence status, and explicit warnings that current watchlists can have survivorship bias and the engine does not model dividends/corporate actions.
5. Run the integration test and existing backtest tests.

### Task 4: Expose the workflow and document boundaries

**Files:**
- Modify: `src/stock_daily_report/cli.py`
- Modify: `README.md`
- Test: `tests/test_portfolio_backtest_cli.py`

**Steps:**
1. Add a failing CLI test for `portfolio-backtest` argument parsing and report-path output.
2. Run the targeted test and verify it fails because the command is not registered.
3. Wire `portfolio-backtest` to the settings loader, watchlist, fixture provider, market-scan settings, and portfolio runner. Keep the command offline and do not fetch live prices.
4. Document the command, required normalized CSVs, next-open assumption, equal-weight baseline, and data limitations.
5. Run the CLI and all portfolio/backtest tests.

### Task 5: Verify and commit

**Files:** all files above.

**Steps:**
1. Run `pytest -q`.
2. Run `ruff check .` and `git diff --check`.
3. Inspect the report generated from the committed fixture and verify it makes no performance claim.
4. Commit the implementation with the required Copilot co-author trailer.
