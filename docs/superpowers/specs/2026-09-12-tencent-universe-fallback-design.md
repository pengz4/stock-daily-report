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

The adapter will:

- request the `aStock` board with a page size of 200;
- read the reported total and fetch all pages;
- normalize exchange-prefixed codes (`sh`, `sz`, `bj`) to six-digit codes;
- normalize name, latest price, volume, amount, turnover rate, and quote date;
- deduplicate codes and reject malformed or incomplete responses;
- expose failures using the existing availability/data error distinction.

Unit conversion must preserve the canonical universe semantics:

- Tencent `volume` is quoted in lots and is converted to shares;
- Tencent `turnover` is quoted in ten-thousand CNY and is converted to CNY;
- Tencent `hsl` is a percentage and is converted to the canonical
  non-percent rate representation used by the project.

The adapter must not silently treat missing numeric values as zero when that
would make a quote eligible for scanning.

### Provider selection

Use Tencent as an additional full-market quote fallback after the AkShare
Eastmoney attempt and before the existing Sina attempt. The implementation
may extend the existing universe-provider selection mechanism or introduce a
small ordered selector, but it must preserve the current provider error
semantics and avoid changing historical provider configuration.

The expected-code completeness check must continue to operate. The selected
quote snapshot itself can provide the expected code set when it is the
Tencent snapshot, provided the same normalization and supported-code checks
are applied.

## Data flow

1. Request the primary AkShare Eastmoney snapshot.
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
- empty pages or an absent response payload;
- upstream responses that cannot be decoded as the expected schema.

The following are data errors and must be surfaced rather than silently
recovered:

- unsupported or mismatched exchange codes;
- duplicate normalized codes;
- non-numeric required quote fields;
- a response that claims a total but cannot produce the corresponding
  complete set of pages;
- a snapshot that is materially incomplete after all pages are fetched.

Pagination must have a bounded page count derived from the reported total and
must reject repeated page progress or unexpected page growth.

## Testing and validation

Add unit tests for:

- exchange-code normalization;
- Tencent field and unit conversion;
- one-page and multi-page response assembly;
- duplicate and unsupported codes;
- empty, malformed, truncated, and inconsistent pagination responses;
- availability-error fallback ordering;
- preservation of the existing minimum latest amount eligibility threshold.

Run an integration probe outside the unit suite using:

- a single known security (`600519`);
- one full Tencent snapshot;
- a normalized record count and supported-code check.

The probe must not persist credentials or commit live responses. Existing
provider, market-scan, resume, and lint tests must remain green.

## Explicit non-goals

- Do not change the `DailyBar` schema.
- Do not add Tencent adjusted history until amount and turnover-rate coverage
  is verified.
- Do not add `financial-api` in this change; it requires an `X-api-key` and
  should be evaluated separately as an optional authenticated provider.
- Do not remove AkShare or Sina.
- Do not modify the 80% coverage threshold or the resumable checkpoint format.
