# Full-Market Dual Ranking Design

## Goal

Keep `config/watchlist.yaml` as the explicit source of stocks that receive a
full daily report, while adding an explainable full-market opportunity scan:

- trend ranking: top 30;
- balanced ranking: top 30;
- consensus group: stocks present in both rankings.

The rankings are research candidate lists, not buy instructions. Consensus is
described as agreement between two related rule sets, not as independently
verified higher certainty.

## Scope and boundaries

The scanner enumerates all currently listed supported A-share codes, applies a
cheap eligibility gate to every symbol, and then downloads sufficient history
for eligible symbols. It does not run the full watchlist report pipeline for
every stock.

The first version excludes:

- ST, `*ST`, delisting-consolidation, and unsupported security classes;
- suspended or invalid latest quotes;
- fewer than 120 valid daily bars;
- insufficient liquidity;
- stale or data-quality-rejected histories.

All exclusions and provider failures are counted by reason. Rankings are
published only when valid-history coverage reaches the configured threshold.
Otherwise, the daily report shows the scan as unavailable with coverage and
failure details.

## Data flow

1. Fetch one market-universe snapshot from an AkShare bulk quote endpoint.
2. Normalize code, name, market, latest price, volume, amount, and quote date.
3. Apply listing-class, name, trading-state, and current-liquidity filters.
4. Fetch bounded historical data for eligible symbols with limited concurrency,
   retry, local cache, and primary/fallback providers.
5. Validate every history independently. A single-symbol failure does not fail
   the scan.
6. Calculate technical metrics and simplified structural observations.
7. Produce deterministic trend and balanced scores.
8. Sort by score descending, then code ascending for stable tie-breaking.
9. Select top 30 from each profile and mark their intersection as consensus.
10. Write a versioned JSON scan artifact for the daily report to consume.

The market scan runs separately from the daily watchlist publication. The
daily report uses the latest successful same-date scan when available and
continues publishing the watchlist report if the scan is unavailable.

## Scoring

All components are deterministic values between 0 and 100 and include named
evidence. Inputs are clipped to documented ranges rather than normalized
against future observations.

### Trend profile

- trend alignment and moving-average slope: 45%;
- 20/60-day momentum: 30%;
- volume/price confirmation: 15%;
- risk adjustment: 10%.

### Balanced profile

- trend alignment and moving-average slope: 30%;
- 20/60-day momentum: 20%;
- volume/price confirmation: 15%;
- simplified structural evidence: 20%;
- volatility, drawdown, and overextension risk: 15%.

Risk is a positive safety component: lower volatility, controlled drawdown,
and limited MA20 extension receive higher scores. Extreme short-term momentum
is capped and penalized to avoid ranking solely by recent price spikes.

Each ranked item records total score, component scores, evidence codes, risk
codes, latest trade date, provider, and rank. Consensus items additionally
record both ranks and both scores.

## Report presentation

The report adds three sections:

1. **Trend Top 30**
2. **Balanced Top 30**
3. **Multi-strategy consensus**

Consensus rows are visually highlighted. They are labeled “multi-strategy
consensus,” not “high certainty.” The report explains that both profiles share
some inputs and are therefore correlated.

The JSON metadata includes:

- scan rule version;
- universe count;
- eligible count;
- valid-history count;
- coverage ratio;
- exclusion and failure counts;
- scan generation time;
- provider names;
- input/config hashes.

## Backtest gate

Replay stores daily membership for:

- trend-only;
- balanced-only;
- consensus.

The existing execution-aware evaluator compares each group over 1/5/20 trading
days, including sample count, hit rate, average return interval, drawdown, and
turnover. Consensus wording may only be strengthened after enough
out-of-sample observations show stable improvement. Until then it remains a
descriptive agreement label.

## Operational safety

- Bounded concurrency and retries prevent uncontrolled provider pressure.
- Per-symbol failures are explicit and aggregated.
- Coverage below threshold suppresses rankings.
- Same-date scan artifacts are immutable and reused on duplicate workflow
  execution.
- Scanner timeout does not block the watchlist report.
- No broker account, order placement, or portfolio holdings are accessed.

