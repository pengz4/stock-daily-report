# Daily Report Readability Redesign Implementation Plan

> **For agentic workers:** REQUIRED: Use superpowers:subagent-driven-development (if subagents available) or superpowers:executing-plans to implement this plan. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the wide, text-dense HTML ranking tables with a readable static report composed of summary metrics, consensus highlight cards, compact Top 10 rows, native expandable details, and responsive watchlist cards.

**Architecture:** Keep `ReportDocument`, market-scan artifacts, ranking algorithms, JSON, and Markdown unchanged. Extend the existing HTML renderer with focused helpers that emit semantic cards and native `<details>/<summary>` controls; keep all dynamic values escaped. Move the presentation system into `site/styles.css`, with warning and empty states driven only by validated report fields.

**Tech Stack:** Python 3.11+, Pydantic report models, deterministic HTML strings, static CSS, native HTML `<details>/<summary>`, pytest, Ruff.

---

## File map

| File | Responsibility |
| --- | --- |
| `src/stock_daily_report/report/render.py` | Add HTML-only helpers for header metadata, summary metrics, consensus cards, compact rankings, ranking details, full-ranking disclosure, and watchlist details. Preserve Markdown helpers and escaping. |
| `site/styles.css` | Define constrained layout, metric/highlight/detail cards, compact rows, warning/empty states, and narrow-screen behavior. |
| `tests/test_report_render.py` | Add renderer fixtures and assertions for the new HTML structure, ordering, edge states, escaping, and retained watchlist fields. |
| `reports/2026-09-12/index.html` | Regenerated local artifact for manual visual acceptance; do not hand-edit. |

## Chunk 1: Add failing renderer tests

Before editing, require the implementation target paths to be clean so
path-scoped staging cannot capture unrelated pre-existing hunks:

```bash
git diff --cached --quiet
git diff --quiet -- src/stock_daily_report/report/render.py \
  tests/test_report_render.py site/styles.css
```

If either check fails, stop and preserve those changes rather than attempting
to combine them with this redesign.

### Task 1: Extend the market-ranking fixture for multiple rows and edge states

**Files:**
- Modify: `tests/test_report_render.py:18-80`

- [ ] **Step 1: Add a fixture builder for ordered market rankings**

Create a helper that starts from `_market_rankings()` and constructs explicit
valid profile tuples with thirty Trend records and five Balanced records.
Use two shared codes for consensus records and three distinct Balanced-only
codes so the exact intersection remains valid. Include:

- two consensus records with different `trend_rank`/`balanced_rank` values;
- Trend-only and Balanced-only records so non-consensus rendering is covered;
- ranks 1-30 in Trend so the full 11-30 disclosure can be tested;
- only ranks 1-5 in Balanced so fewer-than-ten behavior can be tested;
- a nonempty `failure_counts` entry;
- evidence and risk codes containing text that must be escaped.

Keep the model values valid: consensus records must match the shared profile
codes, names, ranks, scores, both component sets, evidence/risk unions,
latest-trade dates, and provider names required by the model validator.
Preserve the existing fixture's JSON assertions.

- [ ] **Step 2: Add an unavailable-ranking fixture**

Create a helper that returns a `MarketRankings` object with
`status="unavailable"`, reason `"scan_rankings_unavailable"`, and complete
metadata copied from the existing fixture, but explicitly set
`trend=()`, `balanced=()`, and `consensus=()`. This represents low coverage
after the pipeline has validated the scan state and ensures no ranking claims
can be rendered. Keep its `scan_date` populated and assert the renderer uses
the watchlist date, not this unavailable scan date, for the header fallback.

- [ ] **Step 3: Add a no-consensus fixture**

Create a helper whose Trend and Balanced tuples use disjoint stock codes, then
set `consensus=()`. Keep both profiles populated and set valid ranks/scores.
This satisfies the exact-intersection validator without weakening schema
validation.

- [ ] **Step 4: Run the focused test file**

Run:

```bash
PYTHONPATH=src pytest tests/test_report_render.py -q
```

Expected: existing tests pass before new assertions are added.

### Task 2: Write failing assertions for the new HTML contract

**Files:**
- Modify: `tests/test_report_render.py` near the existing market-render test

- [ ] **Step 1: Test the header and summary metrics**

Add a test that renders an available report and asserts the HTML contains:

- a report header with a quality badge;
- a `<details class="runtime-metadata">` block;
- `.summary-metrics`, `.metric-card[data-metric="coverage"]`,
  `.metric-card[data-metric="valid"]`, `.metric-card[data-metric="universe"]`,
  and `.metric-card[data-metric="consensus"]`;
- the scan date as the primary latest-trading-date source.

Assert the metric values are escaped/rendered from the model, not hard-coded.
Assert the runtime metadata exposes report generated/source timestamps,
providers, stock count, snapshot path/hash, report config hash, analyzer,
scan date/generated time, rule version, ranking providers, ranking config and
input hashes, universe/eligible/valid counts, coverage, every exclusion code
and count, and every failure code and count.
Use a fixture where scan date and watchlist date differ and assert both dates
are visible. Use an unavailable-ranking fixture with a watchlist date and
assert that date becomes the fallback latest-trading-date source.

- [ ] **Step 2: Test deterministic consensus ordering and cards**

Assert consensus cards use `.consensus-card`, contain no more than five
records, and order records by `trend_rank`, then `balanced_rank`, then code.
Assert each card exposes both Trend and Balanced ranks and scores through
`.consensus-trend` and `.consensus-balanced` elements.

- [ ] **Step 3: Test compact Top 10 and full-ranking disclosure**

Assert `.ranking-profile[data-profile="trend"] .ranking-visible` contains
exactly ten `.ranking-row` elements with ranks 1-10, and its `.full-ranking`
`<details>` contains exactly ranks 11-30. Assert the Balanced profile's
`.ranking-visible` preserves its five available rows. Assert the full-ranking
control is omitted when there are no rows beyond rank 10; do not count rows
globally because closed details remain in the DOM.

- [ ] **Step 4: Test ranking detail contents and failure warning**

Assert the ranking detail markup contains all five component names
(`trend`, `momentum`, `volume`, `structure`, `risk`), evidence, risks, date,
provider, and consensus rank/score data. Assert nonempty `failure_counts`
produces `.ranking-warning` and exposes the actual failure code and count in
the `.runtime-metadata` audit details without suppressing available rankings.

- [ ] **Step 5: Test unavailable and no-consensus states**

Assert an unavailable scan renders the existing reason and warning state
without ranking claims or empty ranking cards. Assert an empty consensus
intersection renders an explicit no-consensus message.

- [ ] **Step 6: Test watchlist field retention**

Update the fixture with nonempty group, quality issues, metrics, structure
levels/observations, key levels, and next conditions. Assert every
`StockReport` field, every metric field, and every `StructureLevel` kind, price,
and source appears in HTML. Keep code/name, decision, structure state/status,
trade date, source timestamp, provider, and primary metrics in the visible
stock-card summary; keep the remaining fields in grouped details. Verify
empty quality issues, levels, observations, evidence, risks, key levels, and
next conditions render `None`.

- [ ] **Step 7: Test escaping and static fallback**

Keep the existing escaping tests and add assertions that malicious values in
ranking names, evidence codes, and watchlist detail strings are escaped. Add
assertions that the HTML contains native `<details>/<summary>` controls and
does not require a script tag for the new interaction. Also assert empty
evidence and risks render `None`. For the legacy fallback, explicitly set
`schema_version=1` before removing `market_rankings`, then assert the existing
unavailable fallback is rendered. Capture `document.model_dump(mode="json")`
and `render_markdown(document)` before HTML rendering, then assert both remain
identical after the visual renderer changes so schema/JSON/Markdown behavior
is not coupled to the redesign.

- [ ] **Step 8: Run the focused tests to confirm failure**

Run:

```bash
PYTHONPATH=src pytest tests/test_report_render.py -q
```

Expected: the new contract tests fail because the current renderer still
emits wide tables and flat watchlist sections.

## Chunk 2: Implement semantic HTML rendering

### Task 3: Add shared HTML formatting helpers

**Files:**
- Modify: `src/stock_daily_report/report/render.py` near existing HTML helpers

- [ ] **Step 1: Add safe display helpers**

Implement small helpers for:

- `_format_html_optional(value) -> str`, returning escaped text or the existing
  em dash for `None`;
- `_format_html_float(value, digits=2) -> str`, returning a fixed-precision
  escaped numeric string or the existing em dash when `value is None`;
- `_render_html_tags(values, *, kind) -> str`, returning escaped tag spans or
  a `None` span for an empty sequence;
- `_render_html_field(label, value, *, class_name="detail-field") -> str`,
  returning a labeled escaped value pair;
- `_render_html_details(summary, body, *, class_name) -> str`, where `summary`
  is escaped text and `body` is already-rendered trusted markup from renderer
  helpers; emit a closed native `<details>` element by default.

Do not bypass `_html()`; every model-derived string must pass through it.
Use these stable helper contracts and class names when wiring the renderer:

| Helper | Input | Output |
| --- | --- | --- |
| `_render_report_header(report, rankings)` | `ReportDocument`, `MarketRankings` | `<header class="report-header">` with title, dates, status, and runtime details |
| `_render_summary_metrics(rankings)` | `MarketRankings` | `<div class="summary-metrics">` containing `.metric-card[data-metric=...]` |
| `_render_consensus_cards(consensus)` | `Sequence[MarketConsensusRanking]` | `.consensus-section` with `.consensus-card` details or `.empty-state` |
| `_render_ranking_profile(profile, records, consensus_by_code)` | profile name, `Sequence[MarketRanking]`, lookup mapping | `.ranking-profile[data-profile=...]` with `.ranking-visible` and optional `.full-ranking` |
| `_render_ranking_detail(record, consensus)` | `MarketRanking`, optional `MarketConsensusRanking` | closed `<details class="ranking-detail">` with escaped audit fields |
| `_render_watchlist_card(stock)` | `StockReport` | `<article class="stock-card">` with visible summary and grouped details |

Unavailable and available-with-failures output must use the stable
`.ranking-warning` class; the class is applied by the ranking-state helper,
not inferred by CSS from message text.

- [ ] **Step 2: Add report metadata and summary helpers**

Implement helpers that render:

- header date and quality status;
- latest-trading-date precedence: `market_rankings.scan_date` when
  `rankings.status == "available"`, otherwise the maximum
  `stock.latest_trade_date` from validated watchlist records; if an available
  scan date and any watchlist dates differ, render both with explicit labels;
- a closed `<details class="runtime-metadata">` containing report generated
  time, report latest source timestamp, report provider names, stock count,
  snapshot path/hash, report config hash, analyzer version, and available
  ranking scan date, generated time, rule version, provider names, config hash,
  input hash, universe/eligible/valid counts, coverage, exclusion counts, and
  failure counts;
- summary cards for coverage, valid/eligible, universe, and consensus count,
  omitting unavailable values rather than inventing them;
- a warning class when rankings are unavailable or available rankings have
  nonempty failure counts.

Use `rankings.status` and `rankings.failure_counts` as the only ranking-state
signals. Do not load configuration or recompute coverage thresholds.

- [ ] **Step 3: Run the new header and summary tests**

Run:

```bash
PYTHONPATH=src pytest tests/test_report_render.py -q -k "summary or metadata"
```

Expected: these tests pass once the helpers are wired into `render_html()`;
other new ranking tests may still fail.

### Task 4: Render consensus and ranking cards

**Files:**
- Modify: `src/stock_daily_report/report/render.py` in the current market-ranking HTML helpers

- [ ] **Step 1: Implement deterministic consensus selection**

Sort consensus records by `(trend_rank, balanced_rank, code)` and slice to five.
Render all available records when fewer than five exist. Render an explicit
no-consensus state when the sequence is empty.

- [ ] **Step 2: Implement consensus highlight cards**

Render each card with code/name, Trend rank/score, Balanced rank/score, both
component sets, evidence tags, risk tags, latest trade date, and provider.
Use a closed native `<details>` wrapper for the card detail so the card summary
is visible and the audit content is expandable.

- [ ] **Step 3: Implement compact profile rows**

For each profile, render records with rank <= 10 as compact rows containing
rank, code/name, score, consensus marker, and a short escaped risk summary.
If the profile sequence is empty, render a clear empty state defensively.

- [ ] **Step 4: Implement full-ranking disclosure**

If records with rank > 10 exist, render a closed `<details>` block containing
only ranks 11-30. Omit the control when no such records exist. Keep row order
deterministic and preserve the model's rank values.

- [ ] **Step 5: Implement ranking detail cards**

For each compact and full-ranking row, provide a closed native detail block
with rank, code, name, score, available cross-profile consensus ranks/scores,
all five components, evidence tags, risk tags, latest trade date, and provider.

- [ ] **Step 6: Preserve unavailable and failure states**

Keep the existing unavailable reason plus scan date, generated time,
rule/config/input hashes, provider names, counts, coverage, exclusions, and
failures in the runtime metadata. Add warning classes for unavailable and
available-with-failures states, expose each failure code/count, and never
render ranking claims in the unavailable branch. Keep Markdown output
unchanged and continue escaping every HTML dynamic value through `_html()`.

- [ ] **Step 7: Run ranking-focused tests**

Run:

```bash
PYTHONPATH=src pytest tests/test_report_render.py -q -k "market_scan or ranking or consensus"
```

Expected: all new consensus, compact-row, expansion, detail, unavailable, and
failure-warning assertions pass.

### Task 5: Render the header, summary, rankings, and watchlist in `render_html`

**Files:**
- Modify: `src/stock_daily_report/report/render.py:render_html`
- Modify: `src/stock_daily_report/report/render.py:_render_stock_html`

- [ ] **Step 1: Replace the flat metadata block**

Compose `render_html()` from the new header and summary helpers. Keep the
existing report title, index link, linked stylesheet, viewport metadata, and
escaped dynamic title. Add `class="report-page"` to the dated report
`<main>` so report-specific CSS cannot change the shared site index layout.

- [ ] **Step 2: Replace the market-ranking table call**

Call the new market-ranking card renderer while leaving
`_render_market_rankings_markdown()` and its table helpers unchanged. Do not
change `ReportDocument`, `MarketRankings`, or any JSON serialization behavior;
the HTML renderer consumes the existing models only.

- [ ] **Step 3: Convert watchlist articles to grouped details**

Keep visible decision, structure `state_label`, structure `status`, latest
trade date, latest source timestamp, provider, and primary metrics. Put group,
code, and name in the visible card identity, and keep
bar count, quality status/issues, the complete metrics set
(`close`, `ma20`, `ma60`, `return20`, `return60`, `realized_volatility20`,
`drawdown60`, `volume_ratio20`, `recent_high20`, `recent_low20`), structure
rule version/levels/observations, evidence, risks, key levels, and next
conditions into labeled native details. Render `None` for empty sequences and
escape every value. Extend the tests to assert every retained
`StockReport` field, every listed metric field, and every structure
field/level/observation appears in the generated HTML.

- [ ] **Step 4: Run all renderer tests**

Run:

```bash
PYTHONPATH=src pytest tests/test_report_render.py -q
```

Expected: PASS, including the existing Markdown and model-dump assertions
that prove schema and JSON behavior are unchanged.

## Chunk 3: Implement responsive presentation

### Task 6: Replace wide-table CSS with the card layout

**Files:**
- Modify: `site/styles.css`

- [ ] **Step 1: Define layout tokens and page shell**

Keep existing global index rules intact and scope report-specific shell rules
under `.report-page`: readable system font stack, background/text colors,
constrained width, spacing, and link treatment. Preserve the existing light
color scheme and verify `site/index.html` remains readable.

- [ ] **Step 2: Add semantic card and status styles**

Add styles for exact renderer classes: `.report-header`,
`.runtime-metadata`, `.summary-metrics`, `.metric-card`, `.consensus-section`,
`.consensus-card`, `.ranking-profile`, `.ranking-visible`, `.ranking-row`,
`.full-ranking`, `.ranking-detail`, `.detail-field`, `.tag`,
`.tag--evidence`, `.tag--risk`, `.ranking-warning`, `.empty-state`, and
`.stock-card`. Include readable borders, spacing, alignment, and warning
contrast for the cards, rows, details, warnings, and watchlist.

- [ ] **Step 3: Add responsive rules**

Use a `640px` breakpoint. At a 375px viewport, make metric and highlight grids
one column, allow long labels/tags to wrap, keep scores aligned, and prevent
primary content from depending on horizontal scrolling. Use only the semantic
classes emitted by the renderer, not table-specific assumptions.

- [ ] **Step 4: Add keyboard focus and native-details states**

Add high-contrast `:focus-visible` styles for links and
`.report-page summary`, plus clear open/closed states for runtime metadata,
ranking details, full rankings, and stock details. Keep all critical values in
the HTML so controls remain usable without JavaScript.

- [ ] **Step 5: Check style syntax and focused tests**

Run:

```bash
PYTHONPATH=src pytest tests/test_report_render.py -q
ruff check src/stock_daily_report/report/render.py tests/test_report_render.py
git diff --check
```

Expected: all commands pass. Open `reports/2026-09-12/index.html` at a 375px
viewport and verify in the browser console that
`document.documentElement.scrollWidth <= document.documentElement.clientWidth`;
also verify metric/highlight cards wrap and no `.ranking-row` or `.detail-field`
has clipped text. Open `site/index.html` and verify its links retain the
existing readable layout.

## Chunk 4: Regenerate and validate the published artifact

### Task 7: Generate the dated report and inspect the final HTML

**Files:**
- Generate locally: `reports/2026-09-12/index.html`
- Verify unchanged: `reports/2026-09-12/report.md` and `reports/2026-09-12/report.json`
- Do not stage ignored generated reports unless the user explicitly asks to publish them

- [ ] **Step 1: Run the daily report generation**

Before running, require the existing snapshot and scan artifact and record
their hashes. Back up the pre-existing dirty `site/index.html` because the
daily command may regenerate the shared index. Run the command in a shell that
restores that file on exit:

```bash
set -euo pipefail
git diff --cached --quiet
test -f snapshots/2026-09-12/input.json
test -f market-scans/2026-09-12/scan.json
test -f reports/2026-09-12/report.md
test -f reports/2026-09-12/report.json
before_snapshot=$(sha256sum snapshots/2026-09-12/input.json | cut -d ' ' -f1)
before_scan=$(sha256sum market-scans/2026-09-12/scan.json | cut -d ' ' -f1)
before_md=$(sha256sum reports/2026-09-12/report.md | cut -d ' ' -f1)
before_json=$(sha256sum reports/2026-09-12/report.json | cut -d ' ' -f1)
before_site=$(sha256sum site/index.html | cut -d ' ' -f1)
cp site/index.html .git/report-readability-site-index.before.html
restore_site_index() {
  status=$?
  cp .git/report-readability-site-index.before.html site/index.html || status=$?
  test "$before_site" = "$(sha256sum site/index.html | cut -d ' ' -f1)" || status=$?
  rm -f .git/report-readability-site-index.before.html || status=$?
  trap - EXIT
  return "$status"
}
trap restore_site_index EXIT
PYTHONPATH=src python3 -m stock_daily_report.cli daily \
  --date 2026-09-12 \
  --settings config/settings.yaml \
  --watchlist config/watchlist.yaml \
  --output-root . \
  --reuse-existing-snapshot \
  --skip-notifications
test "$before_snapshot" = "$(sha256sum snapshots/2026-09-12/input.json | cut -d ' ' -f1)"
test "$before_scan" = "$(sha256sum market-scans/2026-09-12/scan.json | cut -d ' ' -f1)"
test "$before_md" = "$(sha256sum reports/2026-09-12/report.md | cut -d ' ' -f1)"
test "$before_json" = "$(sha256sum reports/2026-09-12/report.json | cut -d ' ' -f1)"
```

Expected: the command succeeds, reuses the existing snapshot, writes the
regenerated HTML under `reports/2026-09-12/`, and leaves the scan artifact,
Markdown, JSON, and pre-existing `site/index.html` content byte-for-byte
unchanged. If either input is absent, stop rather than fetching network data.

- [ ] **Step 2: Verify the generated HTML contract**

Run:

```bash
test -f reports/2026-09-12/index.html
rg -n "summary-metrics|consensus-card|ranking-row|<details|Top 10|failure" \
  reports/2026-09-12/index.html
if rg -n "ranking-table|ranking-table-wrap|<table" reports/2026-09-12/index.html; then
  echo "old wide-table markup found" >&2
  exit 1
else
  status=$?
  test "$status" -eq 1
fi
```

Expected: the generated page contains the new semantic sections, native
details controls, Top 10 regions, and no old wide ranking-table markup. Also
check representative escaped values and audit fields with:

```bash
rg -n "coverage|universe|eligible|valid|config_hash|input_hash|failure|sina" \
  reports/2026-09-12/index.html
```

- [ ] **Step 3: Check narrow-view content**

Serve the repository from WSL with `python3 -m http.server 8000` and open
`http://localhost:8000/reports/2026-09-12/index.html` using the existing WSL
browser workflow. Inspect the page at exactly 375px wide and confirm:

- the summary cards wrap without clipped text;
- consensus cards remain readable;
- ranking rows do not require horizontal scrolling;
- details open and close with keyboard-accessible native controls;
- warning and empty states are covered by the renderer tests using
  unavailable/no-consensus fixtures.
- In the browser console,
  `document.documentElement.scrollWidth <= document.documentElement.clientWidth`
  is true, and no `.ranking-row` or `.detail-field` has clipped text.

- [ ] **Step 4: Run the complete validation suite**

Run:

```bash
PYTHONPATH=src pytest -q
ruff check .
git diff --check
```

Expected: all tests pass, Ruff reports no violations, and the diff has no
whitespace errors.

- [ ] **Step 5: Review generated diff for scope**

Run:

```bash
git status --short
git diff --stat
git diff -- reports/2026-09-12/index.html site/styles.css \
  src/stock_daily_report/report/render.py tests/test_report_render.py
```

Confirm only the intended renderer/test/CSS changes are staged. Inspect
generated files directly because ignored `reports/` files do not appear in
ordinary `git diff`:

```bash
sha256sum market-scans/2026-09-12/scan.json reports/2026-09-12/report.md \
  reports/2026-09-12/report.json
git diff -- site/index.html
git diff -- .github/workflows/market-scan.yml src/stock_daily_report/market_scan \
  src/stock_daily_report/providers/universe.py tests/test_universe_provider.py
```

The scan hash and report Markdown/JSON hashes must match the values captured
before generation, and the unrelated resume/provider diffs must remain
untouched. Do not use reset, checkout, or broad cleanup commands.

- [ ] **Step 6: Commit the implementation in focused commits**

Stage only the renderer/tests and CSS paths; leave ignored generated reports
and all unrelated pre-existing changes uncommitted. Use separate commits for
renderer/tests and CSS when the diffs are independently reviewable:

```bash
git add src/stock_daily_report/report/render.py tests/test_report_render.py
test "$(git diff --cached --name-only)" = $'src/stock_daily_report/report/render.py\ntests/test_report_render.py'
git diff --cached --check
git commit -m "feat: improve report HTML readability" -m \
  "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
git add site/styles.css
test "$(git diff --cached --name-only)" = "site/styles.css"
git diff --cached --check
git commit -m "style: add responsive report cards" -m \
  "Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>"
```

Include the required trailer in each commit:

```text
Co-authored-by: Copilot <223556219+Copilot@users.noreply.github.com>
```
