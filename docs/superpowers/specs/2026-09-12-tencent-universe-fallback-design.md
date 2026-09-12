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

For deterministic unit tests, the adapter accepts an injected requester with
this contract:

```text
request(params: Mapping[str, str], timeout_seconds: float) -> (status: int, body: bytes)
```

Transport and timeout exceptions are raised by the requester. The production
requester performs the HTTP request and leaves JSON decoding to the adapter.

Every successful response must be an HTTP 2xx JSON object whose `data` value is
an object, whose `total` is a non-boolean integer, and whose `rank_list` is a
list of objects. Every page must repeat the same `total`. A non-final page
must contain exactly 200 rows; the final page must contain 1 through 200 rows.
Offsets are exactly `0, 200, 400, ...`; the adapter must not follow a
server-provided next link or issue an offset beyond the calculated page count.

The adapter will:

- request the `aStock` board with a page size of 200;
- read the reported total and fetch all pages;
- normalize exchange-prefixed codes (`sh`, `sz`, `bj`) to six-digit codes;
- normalize name, latest price, volume, amount, and quote date;
- deduplicate codes and reject malformed or incomplete responses;
- expose failures using the existing availability/data error distinction.

The existing `UniverseQuote` convention is preserved: `volume` is the
exchange-reported lot count and `amount` is CNY. Unit conversion must preserve
that convention:

- Tencent `volume` is quoted in lots and is retained as lots;
- Tencent `turnover` is quoted in ten-thousand CNY and is converted to CNY;
- Tencent `hsl` is not persisted because `UniverseQuote` has no turnover-rate
  field and the market-scan checkpoint schema must remain unchanged. Its
  presence is not required for eligibility; malformed values do not affect the
  canonical quote because they are not consumed by this pipeline.

The adapter must not silently treat missing numeric values as zero when that
would make a quote eligible for scanning.

Required source fields are `code`, `name`, `zxj`, `volume`, and `turnover`.
Tencent codes must be exactly eight characters after trimming, with a
case-insensitive `sh`, `sz`, or `bj` prefix followed by six digits. No
separators are accepted, and the prefix must match the supported market
derived from the six-digit code. Names must be non-empty strings; a blank or
malformed name rejects the entire snapshot.

For each required numeric field, a missing key, JSON `null`, empty string, or
whitespace-only string becomes `None` and produces a quote that the existing
filters mark ineligible. Numeric strings and JSON numbers are accepted.
`"NaN"`, `"inf"`, and `"-inf"` become `None` and are ineligible. Booleans,
other non-numeric strings, and negative values reject the entire Tencent
snapshot as a data error. Zero latest price, volume, or amount remains
numeric but is ineligible under the existing positive-measure filters.
Tencent-specific parsing performs the negative-value check before calling the
existing optional-number helper.

The reported `total` must be an integer in the range 1 through 10,000. The
adapter must issue at most 50 page requests and must reject a response if the
reported total changes during pagination, a non-final page is short, a page
contains no previously unseen code, or the final unique-record count differs
from the reported total. All duplicate normalized codes are data errors.

### Provider selection

Refactor the current `AkShareUniverseProvider` into a compatibility facade
over an ordered full-market quote selector. The selector owns three concrete
single-source adapters and uses Tencent between the AkShare Eastmoney attempt
and the existing Sina attempt:

```text
AkShare Eastmoney -> Tencent -> Sina
```

The selector must be the only layer that performs
`AkShare Eastmoney -> Tencent -> Sina`; no source adapter may call another
source internally. The public `AkShareUniverseProvider.get_quotes()` API must
remain `list[UniverseQuote]` for the CLI and resumable scanner. Source names
are runtime-only: they appear in structured errors and diagnostic logging,
not in `UniverseQuote`, the return value, or checkpoint JSON. The historical
provider registry and configuration are unchanged.

The source ownership and completeness rules are:

| Source | Quote request | Expected-code request | Failure identity |
|---|---|---|---|
| `akshare` | `stock_zh_a_spot_em` | `stock_info_a_code_name`, then the existing `stock_zh_a_spot_tx` code-list fallback | `akshare` |
| `tencent` | `getBoardRankList` pages | The reported `data.total` and unique normalized codes | `tencent` |
| `sina` | `stock_zh_a_spot` | `stock_info_a_code_name`, then the existing `stock_zh_a_spot_tx` code-list fallback | `sina` |

The `stock_zh_a_spot_tx` call in the table is only an expected-code fallback;
it is not a second quote attempt and does not transfer ownership of the
selected source. An expected-code availability failure is an availability
error for the selected source; an expected-code data/schema failure is a data
error for the selected source. A source data error stops selection; an
availability error permits the next source. If all sources are unavailable,
raise one `ProviderAvailabilityError` with provider `universe`, code
`all_sources_unavailable`, and ordered detail strings containing each source
name and error code, but never raw response bodies. Each adapter and
normalizer must construct errors with its own source name rather than the
facade's name.

The expected-code completeness check must continue to operate. The selected
quote snapshot itself provides the expected code set when it is the Tencent
snapshot: after normalization, it must contain exactly the reported total,
at least 4,000 records (the existing
`_DEFAULT_MINIMUM_UNIVERSE_SIZE`), no duplicate codes, and only supported
A-share codes. AkShare and Sina use the expected-code requests in the table.
A Tencent snapshot with fewer than 4,000 records is a data error and does not
fall through to Sina.

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

Structured errors and diagnostic logging identify the selected source without
including credentials or raw response bodies. The successful return value
does not expose source identity.

## Error handling

The Tencent adapter must classify the following as availability failures that
permit the next provider:

- connection, timeout, and transport failures;
- non-2xx HTTP responses;
- invalid JSON or an HTML/non-JSON upstream response;
- a top-level JSON value that is not an object;
- an absent or incorrectly typed `data`, `total`, or `rank_list` envelope;
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
- a snapshot that has fewer than 4,000 records.

Pagination uses exactly
`page_count = (total + page_size - 1) // page_size`. It requests offsets
`page_index * page_size` for `page_index` in `range(page_count)` and makes no
request after the final page. A final page of exactly 200 rows is valid when
`total` is divisible by 200. A non-2xx HTTP response, malformed JSON body,
top-level JSON value that is not an object, or invalid `data`/`total`/
`rank_list` envelope is an availability failure. A decoded envelope with
malformed rows, changing totals, duplicate codes, or a count mismatch is a
data failure.

Because Tencent's endpoint is a current snapshot and does not return a
trade-date field, `quote_date` is derived with the existing
`_date_from_clock()` helper using the Asia/Shanghai clock. Weekend dates
therefore use the most recent weekday, matching the existing provider
behavior; exchange holidays are not inferred by this adapter.

The selected source name is runtime-only. It is not added to `UniverseQuote`,
`manifest_hash_for()`, or checkpoint JSON. Resuming a saved checkpoint first
loads and validates the stored quotes and manifest, then skips live universe
provider selection entirely. A fresh scan hashes only normalized quote
contents, as it does today; if two sources produce identical normalized
quotes, they intentionally share the same manifest. This preserves
checkpoint schema version 1 and avoids a source-identity migration.

## Testing and validation

Add unit tests using captured, sanitized response fixtures for:

- exchange-code normalization;
- Tencent field and unit conversion;
- one-page and multi-page response assembly;
- duplicate and unsupported codes;
- empty, malformed, truncated, changing-total, repeated-progress, and
  inconsistent pagination responses;
- availability-error fallback ordering;
- exact request parameters and offsets, source names, and aggregated error
  codes;
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
