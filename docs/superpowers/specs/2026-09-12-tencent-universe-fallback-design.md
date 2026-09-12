# Tencent Full-Market Universe Fallback

## Status

Approved design for implementation planning.

## Goal

Improve full-market scan availability when the AkShare Eastmoney universe
endpoint is unavailable, without weakening the canonical historical-bar
contract or fabricating missing historical fields.

The change is limited to the full-market quote snapshot used to build the
scan candidate universe. The historical-bar provider chain remains unchanged
until a provider can return all fields required by `DailyBar`.

## Current Context

The scan currently obtains the live A-share universe through
`AkShareUniverseProvider`. Its primary endpoint is AkShare's Eastmoney-backed
`stock_zh_a_spot_em`; its quote fallback is the Sina-backed
`stock_zh_a_spot`. AkShare's `stock_zh_a_spot_tx` is already used as an
expected-code fallback, but it is not a complete quote-snapshot fallback.

Tencent's rank endpoint was tested successfully on 2026-09-12:

```text
https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList
```

It returned 5,562 A-share records in 28 pages when requested with a page size
of 200. The response includes the fields needed to construct the current
universe quote: code, name, latest price, volume, turnover amount, and
turnover rate.

Tencent's historical adjusted-K-line endpoint was also tested, but returns
only date, open, close, high, low, and volume. It does not return historical
amount or turnover rate. Because `DailyBar` requires both fields, this design
does not use that endpoint for production history and does not synthesize
missing values.

## Architecture

### Tencent universe adapter

Add a focused `TencentUniverseProvider` (or equivalent provider-scoped
adapter) responsible only for retrieving and normalizing Tencent's paginated
full-market snapshot.

The adapter will use the following stable request contract, matching the
working AkShare Tencent implementation:

- endpoint:
  `https://proxy.finance.qq.com/cgi/cgi-bin/rank/hs/getBoardRankList`;
- query parameters: `_appver=11.17.0`, `board_code=aStock`,
  `sort_type=price`, `direct=down`, `offset`, and `count`;
- `count` is fixed at 200 and `offset` starts at zero and increases by 200;
- the response envelope is `data.total` plus `data.rank_list`;
- each page is requested once with a 15-second timeout; retrying the scan job
  remains the workflow's responsibility.

The adapter will:

- request the `aStock` board with a page size of 200;
- read the reported total and fetch all pages;
- normalize exchange-prefixed codes (`sh`, `sz`, `bj`) to six-digit codes;
- normalize name, latest price, volume, amount, and quote date;
- deduplicate codes and reject malformed or incomplete responses;
- expose failures using the existing availability/data error distinction.

Unit conversion must preserve the canonical universe semantics:

- Tencent `volume` is quoted in lots and is converted to shares;
- Tencent `turnover` is quoted in ten-thousand CNY and is converted to CNY;
- Tencent `hsl` is not persisted because `UniverseQuote` has no turnover-rate
  field and the market-scan checkpoint schema must remain unchanged. Its
  presence is not required for eligibility; malformed values do not affect the
  canonical quote because they are not consumed by this pipeline.

The adapter must not silently treat missing numeric values as zero when that
would make a quote eligible for scanning.

Required source fields are `code`, `name`, `zxj`, `volume`, and `turnover`.
Blank numeric values normalize to `None` using the existing universe
normalization rules and are subsequently rejected or marked ineligible by the
existing filters. Numeric strings are accepted; booleans, non-numeric values,
and negative required measures are data errors. Non-finite values normalize to
`None` and cannot pass eligibility.

The reported `total` must be an integer in the range 1 through 10,000. The
adapter must issue at most 50 page requests and must reject a response if the
reported total changes during pagination, a non-final page is short, a page
repeats progress, or the final unique-record count differs from the reported
total.

### Provider selection

Use an ordered full-market quote selector with Tencent between the AkShare
Eastmoney attempt and the existing Sina attempt:

```text
AkShare Eastmoney -> Tencent -> Sina
```

The selector may extend `AkShareUniverseProvider` or introduce a small
ordered selector, but it must preserve the current provider error semantics,
retain the provider name `akshare` for the existing path, and avoid changing
historical provider configuration. A provider that returns a data error stops
selection; an availability error permits the next provider. The final error
must retain the ordered provider failures without raw response bodies.

The expected-code completeness check must continue to operate. The selected
quote snapshot itself provides the expected code set when it is the Tencent
snapshot: after normalization, it must contain exactly the reported total,
at least the existing minimum universe size, no duplicate codes, and only
supported A-share codes. AkShare and Sina keep their existing independent
expected-code check because their current snapshot path already relies on it.

## Data flow

1. Request the AkShare Eastmoney snapshot.
2. If it fails with an availability error, request the Tencent snapshot.
3. If Tencent also fails with an availability error, request the existing Sina
   snapshot.
4. Normalize the selected response into `UniverseQuote` values.
5. Validate duplicate codes, supported A-share prefixes, numeric fields,
   snapshot completeness, and quote date.
6. Apply the existing eligibility filters and send eligible candidates to the
   resumable history scan.

Provider metadata and error details must identify the selected source without
including credentials or raw response bodies.

## Error handling

The Tencent adapter must classify the following as availability failures that
permit the next provider:

- connection, timeout, and transport failures;
- invalid JSON or an HTML/non-JSON upstream response;
- an absent `data` envelope or absent `rank_list`;
- an empty page while the reported total is positive.

Once a valid response envelope is decoded, the following are data errors and
must be surfaced rather than silently recovered:

- unsupported or mismatched exchange codes;
- duplicate normalized codes;
- non-numeric required quote fields;
- a zero, negative, non-integer, or over-limit total;
- a response that changes its total during pagination;
- a short non-final page, repeated page progress, or a final count that does
  not equal the reported total;
- a snapshot that has fewer than the configured minimum universe size.

Pagination must have a bounded page count derived from the reported total and
must reject repeated page progress or unexpected page growth. A malformed JSON
body is an availability failure; a decoded JSON body with malformed records is
a data failure.

Because Tencent's endpoint is a current snapshot and does not return a
trade-date field, `quote_date` is derived with the existing
`_date_from_clock()` helper using the Asia/Shanghai clock. Weekend dates
therefore use the most recent weekday, matching the existing provider
behavior; exchange holidays are not inferred by this adapter.

## Testing and validation

Add unit tests using captured, sanitized response fixtures for:

- exchange-code normalization;
- Tencent field and unit conversion;
- one-page and multi-page response assembly;
- duplicate and unsupported codes;
- empty, malformed, truncated, changing-total, repeated-progress, and
  inconsistent pagination responses;
- availability-error fallback ordering;
- preservation of the existing minimum latest amount eligibility threshold.

Run an integration probe outside the unit suite using:

- a single known security (`600519`);
- one full Tencent snapshot;
- a normalized record count, supported-code check, total/count equality, and
  latest-quote field check.

The live probe is manual and must not run in CI, persist credentials, or
commit live responses. Existing provider, market-scan, resume, and lint tests
must remain green.

## Explicit non-goals

- Do not change the `DailyBar` schema.
- Do not add Tencent adjusted history until amount and turnover-rate coverage
  is verified.
- Do not add `financial-api` in this change; it requires an `X-api-key` and
  should be evaluated separately as an optional authenticated provider.
- Do not remove AkShare or Sina.
- Do not modify the 80% coverage threshold or the resumable checkpoint format.
