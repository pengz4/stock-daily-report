# Task 7 Recovery and Transaction Hardening Implementation Plan

> **For Copilot:** Use executing-plans skill to implement this plan task-by-task.

**Goal:** Make cache recovery an explicit precondition, hold the shared cache lock through publication acknowledgement and cleanup, and prevent cross-output-root rollback.

**Architecture:** Use the existing date → site → cache lock order. Recovery and cache transaction operations will reuse an outer cache lock when the pipeline owns it. Recovery manifests will carry a canonical output-root owner token; mismatches are typed failures that retain artifacts without replaying them.

**Tech Stack:** Python 3.11, pytest, fcntl file locks, JSON recovery manifests.

---

### Task 1: Add recovery precondition regression

**Files:**
- Modify: `tests/test_pipeline.py`

**Step 1: Write the failing test**

Cover a pending cache recovery whose nonblocking publication/cache lock cannot be acquired. Assert the provider is never called, staged writes are discarded, and a typed cache recovery error is raised.

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/test_pipeline.py -k recovery_precondition`

Expected: FAIL because the current idle recovery helper silently continues into fetching.

### Task 2: Add cache-lock lifetime regression

**Files:**
- Modify: `tests/test_pipeline.py`

**Step 1: Write the failing test**

During cache commit, start a writer against the same cache and assert it cannot acquire the cache lock until publication commit/cleanup completes, then assert it eventually completes.

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/test_pipeline.py -k cache_lock_lifetime`

Expected: FAIL because the current cache commit releases its lock before publication durability and cleanup.

### Task 3: Add cross-output-root regression

**Files:**
- Modify: `tests/test_pipeline.py`

**Step 1: Write the failing test**

Create a committed cache recovery manifest owned by output root A, then attempt recovery from output root B. Assert a typed owner-mismatch error and preservation of both cache bytes and recovery artifacts.

**Step 2: Run test to verify it fails**

Run: `.venv/bin/pytest -q tests/test_pipeline.py -k cross_output_root`

Expected: FAIL because root mismatch currently falls through to destructive rollback.

### Task 4: Implement the recovery precondition

**Files:**
- Modify: `src/stock_daily_report/pipeline.py`

**Step 1: Implement minimal behavior**

Make pending cache recovery lock acquisition part of the initial run precondition. Raise a typed error on nonblocking lock failure or recovery verification failure, and place the precondition inside the existing cleanup boundary so staged writes are discarded. Preserve `KeyboardInterrupt` and existing typed recovery errors.

**Step 2: Run focused tests**

Run: `.venv/bin/pytest -q tests/test_pipeline.py -k recovery_precondition`

Expected: PASS.

### Task 5: Implement fixed cache-lock lifetime

**Files:**
- Modify: `src/stock_daily_report/providers/service.py`
- Modify: `src/stock_daily_report/pipeline.py`

**Step 1: Implement minimal behavior**

Add a re-entrant, thread-aware cache transaction lock context. Acquire it after date/site locks for both blocking publication and nonblocking recovery paths. Make cache recovery, staged commit, rollback, and cleanup reuse the outer lock. Keep the lock held until publication commit, cache cleanup, and publication cleanup/acknowledgement finish.

**Step 2: Run focused tests**

Run: `.venv/bin/pytest -q tests/test_pipeline.py -k "cache_lock_lifetime or recovery_precondition"`

Expected: PASS.

### Task 6: Implement canonical owner protection

**Files:**
- Modify: `src/stock_daily_report/providers/service.py`

**Step 1: Implement minimal behavior**

Persist a canonical output-root owner token in new cache recovery manifests. Validate the token before any replay action; reject mismatches with `CacheRollbackError` while retaining the manifest and cache contents. Keep legacy unbound manifests compatible with existing direct cache recovery tests.

**Step 2: Run focused tests**

Run: `.venv/bin/pytest -q tests/test_pipeline.py -k cross_output_root`

Expected: PASS.

### Task 7: Verify the complete change

**Files:**
- Verify: `src/stock_daily_report/pipeline.py`
- Verify: `src/stock_daily_report/providers/service.py`
- Verify: `tests/test_pipeline.py`
- Verify: `tests/test_quality_checks.py`

**Step 1: Run Task 7 focused suite**

Run: `.venv/bin/pytest -q tests/test_pipeline.py tests/test_quality_checks.py`

**Step 2: Run full validation**

Run: `.venv/bin/pytest -q && .venv/bin/ruff check . && git diff --check`

**Step 3: Inspect diff and commit**

Run: `git diff --stat && git diff`

Commit with:

```bash
git add docs/plans/2026-09-04-task-7-recovery-transaction.md src/stock_daily_report/pipeline.py src/stock_daily_report/providers/service.py tests/test_pipeline.py
git commit -m "fix: close Task 7 recovery ownership gaps"
```

Include the required `Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>` trailer.
