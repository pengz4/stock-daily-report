# Daily Report Readability Redesign

## Status

Approved design for implementation planning.

## Goal

Improve the readability of the self-contained HTML daily report without
changing the report data model, scan artifact, ranking algorithm, or Markdown
audit output.

The primary use case is balanced daily reading: the user should be able to
understand the market scan and identify a small set of actionable research
items quickly, while retaining access to complete ranking evidence and audit
metadata.

## User-validated choices

- Use the **重点卡片 + 紧凑排名** layout.
- Show Trend and Balanced Top 10 by default.
- Keep ranks 11-30 available in an expandable full-ranking region.
- Show consensus stocks as highlighted cards.
- Use detail cards for Components, Evidence, Risks, and source metadata.
- Keep the page usable as a local static HTML file.

## Information architecture

The HTML report will be organized into five layers:

1. **Header**
   - Report date and latest trading date.
   - Link back to the dated-report index.
   - Quality status badge.
   - Operational metadata such as generated time, providers, hashes, and
     analyzer version moved into a collapsible runtime-information region.

2. **Market summary**
   - Existing one-sentence market summary.
   - Summary metric cards for coverage, valid/eligible, universe, and consensus
     count where the values are available.
   - Explicit warning styling for unavailable scans, low coverage, or failed
     data. The renderer must not infer or fabricate missing metrics.

3. **Consensus highlights**
   - Show up to the first five consensus records as highlighted cards.
   - Each card contains code, name, Trend and Balanced ranks, relevant scores,
     component summaries, evidence highlights, risks, trade date, and provider.
   - Clicking a card reveals its detail card without changing the underlying
     report values.

4. **Strategy rankings**
   - Render Trend Top 10 and Balanced Top 10 as compact single-column rows.
   - Each default row contains rank, code/name, score, consensus marker, and a
     short risk indication.
   - Place ranks 11-30 in an explicit “view full ranking” expandable region.
   - A full row detail card exposes the same audit-relevant fields as a
     consensus card.

5. **Watchlist**
   - Preserve all watchlist content and semantics.
   - Present each stock as a readable card with decision, structure, evidence,
     risks, key levels, next conditions, source timestamp, provider, and bar
     count.
   - Empty collections render an explicit `None` state.

## Component and code boundaries

The existing `render.py` remains the renderer entry point. The implementation
will split HTML generation into focused helpers for:

- report header and runtime metadata;
- summary metric cards and market-summary state;
- consensus highlight cards;
- compact ranking rows;
- ranking detail cards;
- expandable full-ranking regions;
- watchlist cards.

All dynamic values continue to pass through the existing HTML escaping helper.
The implementation must not duplicate or reinterpret ranking calculations.
Existing Markdown helpers remain responsible for complete Markdown tables and
are not changed solely to serve the visual layout.

`site/styles.css` owns the presentation system:

- constrained desktop content width;
- card, badge, row, and warning styles;
- readable spacing and typography;
- responsive summary-card and highlight-card grids;
- narrow-screen layout that keeps the primary content readable without
  requiring horizontal scrolling.

The report may include a small inline or self-contained JavaScript behavior
layer for opening and closing detail cards and full-ranking regions. The page
must remain understandable if JavaScript is unavailable: critical content must
still exist in the rendered HTML, and controls must not replace the only copy
of a value.

## Data flow and compatibility

The rendering flow remains:

```text
ReportDocument
  -> render_html()
  -> focused HTML helpers
  -> self-contained report page
```

No changes are made to:

- `ReportDocument` or `MarketRankings` schema;
- `report.json`;
- market-scan checkpoint or scan artifact formats;
- ranking algorithms or source-provider selection;
- Markdown output semantics;
- report publication paths.

The existing unavailable state is preserved. If rankings are unavailable, the
page shows the reason and any valid audit metadata, but does not render empty
or misleading ranking cards. If the consensus intersection is empty, the page
shows an explicit no-intersection state.

## Error and empty-state behavior

- **Unavailable scan:** warning panel with the existing unavailable reason and
  available metadata.
- **Insufficient coverage:** warning styling and no ranking claims.
- **No consensus:** explicit “No exact intersection for this scan” state.
- **Empty evidence or risks:** display `None`; do not silently remove the
  section.
- **Missing optional metadata:** display the existing em dash placeholder.
- **Untrusted text:** escape all dynamic values exactly as before.

## Testing and acceptance criteria

Extend `tests/test_report_render.py` and preserve the existing renderer and
schema coverage. Tests should verify that:

- available reports contain summary metrics, consensus cards, default Top 10
  content, full-ranking expansion content, and detail-card fields;
- unavailable and no-consensus states remain explicit and correctly styled;
- evidence, risks, provider names, and other dynamic values are HTML-escaped;
- Markdown and JSON-oriented assertions remain unchanged;
- legacy reports without market rankings still render their supported fallback
  state.

The implementation is accepted when:

1. Existing targeted and full test suites pass.
2. `ruff check .` and `git diff --check` pass.
3. The regenerated `reports/2026-09-12/index.html` has no primary-content
   horizontal scrolling at a narrow viewport.
4. The default view shows the market summary, consensus highlights, and
   Trend/Balanced Top 10 without exposing the current wide evidence tables.
5. Complete ranking data and audit fields remain accessible from the page and
   in the unchanged Markdown/JSON artifacts.
