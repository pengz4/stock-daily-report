# Operational Optimization Implementation Plan

> **For Copilot:** Use executing-plans skill to implement this plan task-by-task.

**Goal:** Make project handoff facts current, make CI tool versions reproducible, and expose reliable per-provider scan telemetry without claiming unsupported backtest evidence.

**Architecture:** Keep runtime dependencies in `requirements.lock` and pin CI-only tools in a separate development lock. Record provider-attempt telemetry with each completed scan candidate so process-isolated workers and resumable checkpoints preserve the same measurements; aggregate it in scan logs without changing the immutable scan artifact schema. Refresh project status from repository configuration and verified Actions, and document data prerequisites before portfolio-level out-of-sample backtesting.

**Tech Stack:** Python 3.11, pytest, Ruff, Pydantic, GitHub Actions, JSON checkpoints.

---

### Task 1: Refresh project status

Update `PROJECT_STATUS.md` to reflect the merged `master` state, current candidate and coverage limits, dedicated index provider, CI gate, verified recent Actions, actual test count, and remaining data/backtest limitations. Derive facts from current config, code, and Actions instead of copying old status.

### Task 2: Pin development tooling

Add `requirements-dev.lock` with exact pytest/Ruff versions and all pytest runtime dependencies pinned. Update `.github/workflows/ci.yml` to install production dependencies, install the checkout editable without resolving floating extras, then install the development lock. Add a regression test that checks this workflow contract.

### Task 3: Add scan provider telemetry

Represent provider attempt outcome, elapsed milliseconds, cache hit, retry/fallback status, and safe error code in a typed value. Carry these values through `FetchedBars`, child-process responses, candidate results, and resumable checkpoint serialization with backward-compatible defaults. Aggregate request count, cache hits, retries, failures/failure rate, and latency by provider in structured scan logs. Do not add symbol names, credentials, raw URLs, or response data to telemetry.

### Task 4: Establish portfolio backtest data readiness

Document the actual available history and the data-quality/source prerequisites for a meaningful portfolio-level out-of-sample evaluation. Do not synthesize or silently commit unverified historical prices; defer portfolio claims until a licensed, sufficiently long, multi-symbol dataset and split are available.

### Verification

Run focused regressions first, then the full pytest suite, Ruff, and `git diff --check`. Validate checkpoint compatibility for old candidates lacking telemetry and verify aggregated telemetry with both provider fallback and cache-hit scenarios.
