# Tencent Full-Market Universe Fallback Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add Tencent as a deterministic full-market quote fallback between AkShare Eastmoney and Sina without changing historical-bar providers or checkpoint schema.

**Architecture:** Keep `AkShareUniverseProvider.get_quotes() -> list[UniverseQuote]` as the public facade used by both scan entry points. Split its source attempts behind a small ordered selector: AkShare Eastmoney, direct Tencent pagination, then Sina; each source owns one quote request and its expected-code completeness check. Add a focused Tencent HTTP adapter that returns canonical source records, while the existing universe normalizer remains responsible for `UniverseQuote`, quote dates, supported-code checks, and existing filter semantics.

**Tech Stack:** Python 3.11+, standard-library `urllib`/`json` for the Tencent HTTP adapter, dataclasses and typing protocols, pytest, ruff, existing `ProviderAvailabilityError`/`ProviderDataError` contracts.

---

## Chunk 1: Tencent adapter and ordered universe fallback

### Task 0: Preserve the existing dirty worktree

**Files:**
- No production files; inspect the current worktree before implementation

- [ ] **Step 1: Record the pre-existing change set**

Run:

```bash
cd /home/zpeng/stock-daily-report
git status --short
git diff --stat
git ls-files --others --exclude-standard
```

Keep the complete output as the baseline. Do not reset, checkout, stash, or
revert the existing resumable-scan/date-fix changes.

- [ ] **Step 2: Isolate every implementation commit**

If any planned path—tracked or untracked—is dirty or already exists as a
pre-existing file, stop before writing implementation code. This includes the
untracked `tests/test_market_scan_resume.py`. The owner must first commit
those resumable/date-fix changes separately or provide a clean worktree whose
base commit already contains them. Do not create a temporary commit that mixes
the baseline with Tencent work, and do not reset, checkout, stash, or revert
the baseline. In a clean implementation worktree, stage only explicit plan
files, inspect `git diff --cached`, and never use `git add .` or `git commit
-a`. After each commit, rerun the baseline status/diff commands and confirm no
raw response or credential files were introduced.

- [ ] **Step 3: Run the pre-change compatibility baseline**

Run:

```bash
cd /home/zpeng/stock-daily-report
PYTHONPATH=src pytest tests/test_universe_provider.py tests/test_market_scan_resume.py -q
```

Expected: the existing provider and checkpoint tests pass before Tencent code
is added. Preserve this result for comparison after implementation.

### Task 1: Create the Tencent adapter contract and failing tests

**Files:**
- Create: `src/stock_daily_report/providers/tencent_universe.py`
- Create: `tests/test_tencent_universe.py`

- [ ] **Step 1: Define the injected HTTP requester and snapshot record shape in the test**

Write tests against an injected requester with the exact signature
`(params: Mapping[str, str], timeout_seconds: float) -> tuple[int, bytes]`.
Define the adapter's public result shape before the tests:

```python
Requester = Callable[
    [Mapping[str, str], float],
    tuple[int, bytes],
]

@dataclass(frozen=True, slots=True)
class TencentUniverseRecord:
    code: str
    name: str
    latest_price: float | None
    volume: float | None
    amount: float | None

@dataclass(frozen=True, slots=True)
class TencentUniverseSnapshot:
    total: int
    records: tuple[TencentUniverseRecord, ...]

class TencentUniverseProvider:
    def __init__(
        self,
        *,
        requester: Requester | None = None,
    ) -> None: ...

    def fetch_snapshot(self) -> TencentUniverseSnapshot: ...
```

Keep fixtures as in-memory JSON bytes; do not call the live endpoint in unit
tests.

- [ ] **Step 2: Write the failing one-page normalization test**

Cover the exact request parameters:
`_appver=11.17.0`, `board_code=aStock`, `sort_type=price`,
`direct=down`, `offset=0`, and `count=200`. Assert that a response with
`data.total=1` and one `rank_list` row yields:

```text
sh600519 -> 600519
volume "34801.00" -> 34801.0 lots
turnover "443084" -> 4430840000.0 CNY
zxj -> latest_price
```

Run:

```bash
cd /home/zpeng/stock-daily-report
PYTHONPATH=src pytest tests/test_tencent_universe.py::test_fetches_and_normalizes_one_page -q
```

Expected: FAIL because the Tencent adapter and test target do not exist yet.

- [ ] **Step 3: Add failing pagination and total-consistency tests**

Use a requester that records offsets and returns two or more pages. Assert
`page_count = (total + 199) // 200`, offsets `0, 200, ...`, no extra request
after the final page, repeated totals, exact non-final page length, and support
for a final page of exactly 200 rows when `total % 200 == 0`. Add failures for
changing totals, a short non-final page, no newly seen code, duplicate codes,
and final unique-count mismatch.

- [ ] **Step 4: Add failing input-classification tests**

Cover:

- `OSError` and `TimeoutError` becoming `ProviderAvailabilityError("tencent", ...)`;
- non-2xx status, invalid JSON, non-object top-level JSON, missing/incorrect
  `data`, `total`, or `rank_list` becoming availability errors;
- an empty page while `total > 0` becoming an availability error;
- `total == 0`, negative/non-integer totals, and totals above `10_000`
  becoming data errors;
- exact Tencent code grammar: trimmed, case-insensitive `sh`/`sz`/`bj` plus
  six digits, no separators, and matching market prefix;
- missing key, JSON `null`, empty string, whitespace, `"NaN"`, `"inf"`, and
  `"-inf"` becoming `None`;
- booleans, other non-numeric strings, finite negatives, blank names, and
  unsupported/mismatched codes becoming `ProviderDataError`;
- missing/incorrect `code` or `name` fields and non-object rows becoming
  `ProviderDataError`;
- zero price/volume/amount remaining numeric values, while malformed optional
  `hsl` is ignored and does not reject the snapshot.

- [ ] **Step 5: Run the focused tests to verify the red state**

Run:

```bash
cd /home/zpeng/stock-daily-report
PYTHONPATH=src pytest tests/test_tencent_universe.py -q
```

Expected: FAIL only because the adapter implementation is missing.

### Task 2: Implement the Tencent adapter minimally

**Files:**
- Modify: `src/stock_daily_report/providers/tencent_universe.py`
- Test: `tests/test_tencent_universe.py`

- [ ] **Step 1: Implement constants and the injectable requester**

Define the endpoint, fixed page size `200`, maximum total `10_000`, maximum
page requests `50`, and timeout `15.0`. Implement the production requester
with `urllib.request` and return `(status, body)`; allow tests to inject the
requester. Convert only `OSError` (including `TimeoutError`) into availability
errors and let programmer errors surface.

- [ ] **Step 2: Implement envelope validation and bounded pagination**

Decode each 2xx body as JSON, validate the object/data/total/rank-list
envelope, calculate the exact page count, and request only the calculated
offsets. Require repeated totals, exactly 200 rows on non-final pages, 1-200
rows on the final page, and at least one new code per page. Reject duplicates,
changing totals, and count mismatches as data errors. Never include raw bodies
or credentials in error details.

- [ ] **Step 3: Implement field and unit normalization**

Normalize the accepted exchange-prefixed code grammar. Retain Tencent
`volume` as lot count and multiply `turnover` by `10_000` for canonical CNY
amount. Apply the deterministic missing/non-finite/negative rules from the
spec, including preserving numeric zero and ignoring `hsl`. Return records
without `quote_date`; the facade will apply the existing Asia/Shanghai
`_date_from_clock()` behavior.

- [ ] **Step 4: Run the focused tests to verify green**

Run:

```bash
cd /home/zpeng/stock-daily-report
PYTHONPATH=src pytest tests/test_tencent_universe.py -q
```

Expected: all Tencent adapter tests PASS.

- [ ] **Step 5: Commit the isolated adapter**

```bash
cd /home/zpeng/stock-daily-report
git add src/stock_daily_report/providers/tencent_universe.py tests/test_tencent_universe.py
git diff --cached --check
git commit -m "feat: add Tencent universe adapter"
```

### Task 3: Refactor the universe facade to select AkShare, Tencent, then Sina

**Files:**
- Modify: `src/stock_daily_report/providers/universe.py`
- Modify: `tests/test_universe_provider.py`
- Do not modify CLI or settings files; `AkShareUniverseProvider()` remains the
  existing construction point and builds the production default source order

- [ ] **Step 1: Add failing fallback-order tests**

Add tests using injected fake source adapters so no test reaches the network.
Define the internal test/source protocol before writing the assertions:
`name: str` plus `fetch_snapshot() -> UniverseSnapshot`, where a snapshot
contains:

```python
@dataclass(frozen=True)
class UniverseSnapshot:
    records: tuple[Mapping[str, object], ...]
    expected_codes: frozenset[str] | None
```

The selector consumes only this protocol. Tencent's adapter-level
`TencentUniverseSnapshot` may use typed `TencentUniverseRecord` values and is
converted to `UniverseSnapshot` by its source wrapper.
Assert:

1. AkShare success prevents Tencent and Sina requests.
2. AkShare availability failure invokes Tencent.
3. Tencent availability failure invokes Sina.
4. Any source data error stops selection and is reported with that source name.
5. All availability failures raise
   `ProviderAvailabilityError("universe", "all_sources_unavailable", ...)`
   with ordered source names and error codes.

- [ ] **Step 2: Add failing expected-code ownership tests**

Verify that AkShare and Sina retain their existing
`stock_info_a_code_name -> stock_zh_a_spot_tx` expected-code behavior, while
Tencent uses its reported total and normalized unique codes without invoking
another quote source. Assert expected-code availability errors are attributed
to the selected source and permit the next source; expected-code data errors
stop selection.

- [ ] **Step 3: Add failing compatibility tests**

Preserve the public return type and existing behaviors:

- `AkShareUniverseProvider.get_quotes()` still returns `list[UniverseQuote]`;
- weekend quote dates still use the most recent weekday;
- `UniverseQuote` remains unchanged and immutable;
- existing code/name/market normalization and eligibility threshold inputs
  remain unchanged;
- no provider source identity is added to `UniverseQuote`, manifest hashes, or
  checkpoint JSON.

- [ ] **Step 4: Run the selected universe tests to verify red**

Run:

```bash
cd /home/zpeng/stock-daily-report
PYTHONPATH=src pytest tests/test_universe_provider.py -q
```

Expected: the new fallback-order tests fail before the selector is implemented;
existing tests should identify any compatibility adjustment needed.

- [ ] **Step 5: Implement source adapters and the ordered selector**

Refactor `AkShareUniverseProvider` into a compatibility facade over three
single-source attempts. Remove the current internal Eastmoney-to-Sina shortcut
from the selector path so it cannot bypass Tencent. Parameterize shared
normalization helpers with the source name so Tencent/Sina data errors are not
hard-coded as `akshare`. Implement an internal `_UniverseSource` protocol and
`UniverseSnapshot` value shape in `universe.py`; the Tencent adapter is wrapped
to that shape, while AkShare and Sina wrappers retain their existing
fetcher/expected-code hooks. Keep these constructor hooks compatible:
`fetcher` remains the AkShare quote override, `fallback_fetcher` remains the
Sina quote override, `expected_codes_fetcher` remains the primary expected-code
override, `clock` remains the quote-date clock,
`use_minimum_size_fallback` retains its legacy injected-snapshot meaning, and
`minimum_universe_size` remains validated and defaults to 4,000. Add a
keyword-only `sources: Sequence[_UniverseSource] | None` override for tests;
when supplied, the selector uses exactly that source sequence and performs no
live requests. When omitted, the facade builds the production
`akshare -> tencent -> sina` sequence, constructing the direct Tencent
adapter. The legacy fetcher hooks are used while constructing the AkShare and
Sina wrappers. `expected_codes_fetcher` is used by both wrappers when either
source is selected; its availability failure is attributed to the selected
wrapper's name.

For a Tencent-selected snapshot, enforce exactly the reported total, supported
codes, no duplicates, and at least `minimum_universe_size` records (the
production default is 4,000). The adapter-level one-page tests may use
`total=1`; this lower-level test bypasses the selector minimum and only tests
pagination/normalization. For AkShare/Sina, retain the existing independent
expected-code checks. Use `_date_from_clock()` only after the selected source
snapshot has passed source validation.

- [ ] **Step 6: Run the universe tests to verify green**

Run:

```bash
cd /home/zpeng/stock-daily-report
PYTHONPATH=src pytest tests/test_universe_provider.py tests/test_tencent_universe.py -q
```

Expected: all focused provider tests PASS.

- [ ] **Step 7: Commit the selector integration**

```bash
cd /home/zpeng/stock-daily-report
git add src/stock_daily_report/providers/universe.py tests/test_universe_provider.py
git diff --cached --check
git commit -m "feat: add Tencent universe fallback"
```

### Task 4: Verify scan/checkpoint compatibility and document operations

**Files:**
- Modify: `tests/test_market_scan_resume.py`
- Modify: `README.md` to document the full-market fallback order and the fact
  that Tencent history is not used

- [ ] **Step 1: Add the checkpoint compatibility regression test**

Add the named regression test
`test_tencent_normalized_quotes_round_trip_without_provider_identity` to
`tests/test_market_scan_resume.py`. It must build a checkpoint from a
Tencent-normalized `UniverseQuote`, round-trip through `build_checkpoint()` and
`checkpoint_quotes()` without a provider field, and assert that
`manifest_hash_for()` depends only on normalized quote contents. Also assert
that the resumable runner reuses stored quotes before any live universe
provider call. This is a verification-only regression test: no production
checkpoint code is expected to change because the approved design preserves
schema version 1.

- [ ] **Step 2: Verify compatibility without changing the checkpoint schema**

Run the named checkpoint regression and the complete existing resume module
after selector implementation. The exact command is:

```bash
cd /home/zpeng/stock-daily-report
PYTHONPATH=src pytest tests/test_market_scan_resume.py -q
```

Expected: PASS, including
`test_tencent_normalized_quotes_round_trip_without_provider_identity`. Do not
change checkpoint schema version, `UniverseQuote`, manifest payload, or
historical provider configuration; keep production code unchanged for this
compatibility task.

- [ ] **Step 3: Update the README operational note**

Document:

```text
Full-market quote snapshot fallback:
AkShare Eastmoney -> Tencent -> Sina
```

State that Tencent is currently used for the universe snapshot only; its
historical K-line response lacks the amount and turnover-rate fields required
by `DailyBar`, so historical data remains AkShare -> Sina.

- [ ] **Step 4: Run the complete targeted regression suite**

Run:

```bash
cd /home/zpeng/stock-daily-report
PYTHONPATH=src pytest \
  tests/test_tencent_universe.py \
  tests/test_universe_provider.py \
  tests/test_market_scan_filters.py \
  tests/test_market_scan_runner.py \
  tests/test_market_scan_resume.py \
  tests/test_market_scan_cli.py -q
```

Expected: all selected tests PASS.

- [ ] **Step 5: Run static validation**

Run:

```bash
cd /home/zpeng/stock-daily-report
ruff check .
git diff --check
```

Expected: both commands exit successfully with no diagnostics.

- [ ] **Step 6: Run the manual live probe without persisting response data**

Run this exact probe from the repository root:

```bash
cd /home/zpeng/stock-daily-report
PYTHONPATH=src python3 - <<'PY'
import re

from stock_daily_report.providers.tencent_universe import TencentUniverseProvider

snapshot = TencentUniverseProvider().fetch_snapshot()
records = snapshot.records
assert snapshot.total == len(records)
assert snapshot.total >= 4000
assert all(
    re.fullmatch(r"(?:00[0-3]|30[012]|60[0135]|688|920)\d{3}", item.code)
    for item in records
)
record = next(item for item in records if item.code == "600519")
assert record.latest_price is not None and record.latest_price > 0
assert record.amount is not None and record.amount > 0
print(f"tencent universe: {snapshot.total} records; 600519={record.latest_price}")
PY
```

Expected: one line with a count of at least 4,000 and a positive 600519
price. Do not write the raw response to the repository or commit live data.

- [ ] **Step 7: Commit documentation and verification-only changes**

```bash
cd /home/zpeng/stock-daily-report
git add README.md tests/test_market_scan_resume.py
git diff --cached --check
git commit -m "docs: describe Tencent universe fallback"
```

## Final validation

- [ ] Run the full existing test suite:

```bash
cd /home/zpeng/stock-daily-report
PYTHONPATH=src pytest -q
```

Expected: all tests PASS.

- [ ] Confirm `git status --short` contains the user's pre-existing
  resumable-scan changes if they are still present, and no generated raw
  response, credential, cache, or probe-output files.
