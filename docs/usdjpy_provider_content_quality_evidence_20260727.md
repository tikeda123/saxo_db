# USDJPY Saxo Chart provider content-quality evidence

## Purpose

This is a sanitized support and audit brief for Saxo SIM Chart data. It contains
no OAuth token, refresh credential, AppKey, AccountKey, ClientKey, portfolio
identifier, order, or precheck data.

Current decision: `BLOCKED_PROVIDER_CONTENT_QUALITY`.

The failed full-refetch transaction did not change curated bars, derived bars,
or the USDJPY watermark. Raw responses and SHA-256 evidence were retained.

## Reproduction contract

- Environment: SIM
- HTTP method: `GET`
- Endpoint: `/chart/v3/charts`
- Instrument: UIC `42`, `AssetType=FxSpot`, symbol `USDJPY`
- Parameters:
  - `Horizon=60`
  - `Count=1200`
  - `FieldGroups=Data,DisplayAndFormat,ChartInfo`
  - `Mode=UpTo`
  - `Time=<UTC page cursor>`
- DataVersion returned on all 86 pages: `29738069`
- Previous accepted DataVersion: `29738065`

The parameter contract follows the Saxo [Chart endpoint reference](https://www.developer.saxo/openapi/referencedocs/chart/v3/charts/get__chart).
`DataVersion` change is treated as a revision warning and audit identity. It
does not by itself authorize a full-history replacement or an online stop.

## Raw integrity

- acquisition run: `20260727T070513Z-5786a3c7`
- database ingestion run: `824`
- chart pages: `86 / 86` size and SHA-256 verified
- raw observations: `102,643`
- unique sample timestamps: `102,562`
- run manifest SHA-256:
  `c18e72f3c5896c9aeafceeb7752729d70642fc41fc845b73db94fd7de789ece3`
- sanitized JSON evidence SHA-256:
  `8ea85a6f47f2e7443d41229ac285f43b2777c794dc5f69cf336e57473d6574b6`
- sensitive raw keys detected: `0`
- Saxo write requests / prechecks / orders: `0 / 0 / 0`

Machine-readable evidence:
`manifests/fx_extrema_evidence/usdjpy_dv29738069_summary.json`.

## Provider raw anomaly

The new DataVersion contains 245 unique historical rows where one interval
extreme has `Bid > Ask`.

| Check | Result |
|---|---:|
| HighBid > HighAsk | 123 |
| LowBid > LowAsk | 122 |
| OpenBid > OpenAsk | 0 |
| CloseBid > CloseAsk | 0 |
| Bid-side OHLC violations | 0 |
| Ask-side OHLC violations | 0 |
| bid/ask-mid OHLC violations | 0 |
| null or nonpositive rows | 0 |
| latest/forming row affected | no |

- anomaly period: `2010-06-25T13:00:00Z` through
  `2026-07-01T20:00:00Z`
- median crossed difference: `0.001 JPY`
- rows equal to the `0.001 JPY` tick size: `164`
- maximum crossed difference: `0.294 JPY`
- verified calendar expected slots: `197`
- outside verified expected slots: `48`

The anomaly occurs across all years from 2010 to 2026 and across all New York
hours. Removing only the latest bar, daily maintenance boundary, or
out-of-session samples cannot resolve it.

At the same 245 timestamps, 236 rows exist in the previous curated DataVersion
and none has crossed Bid/Ask extremes. The remaining 9 timestamps match the
previous bounded quarantine evidence. All eight Bid/Ask OHLC fields changed on
the 236 overlapping rows, but the same UTC timestamp remains the closest match;
there is no systematic one-hour timestamp shift.

Largest reproducible example:

- time: `2015-10-28T18:00:00Z`
- field: `Low`
- LowBid: `120.332`
- LowAsk: `120.038`
- difference: `0.294 JPY`
- raw artifact:
  `data/acquisition/runs/20260727T070513Z-5786a3c7/instruments/usdjpy/chart_0058.json`
- artifact SHA-256:
  `a3479dff6ba6201a7722db790c17f4744385f9c9f22aae0c61374329b6d2a3c1`

Nine additional large-difference examples and their raw artifact hashes are in
the machine-readable evidence.

## Separate local pagination defect

Inclusive `Mode=UpTo` pagination returned 81 duplicate boundary timestamps.
The second occurrence had the same Open but different High/Low/Close values,
consistent with a partial boundary interval. This did not create or remove any
of the 245 crossed rows: first-seen and last-seen anomaly sets are identical.

The local merge previously overwrote the first full sample with the later
partial duplicate. It has been corrected to retain the first-seen sample while
preserving both original raw pages unchanged. A regression test covers this
case.

An offline replay through the corrected production normalizer and merge path
produced 102,317 accepted unique rows, 245 rejected unique rows, one latest
forming row, and the same `FX_EXTREMA_QUARANTINE_ROW_LIMIT_EXCEEDED` decision.
This confirms that the local boundary defect did not cause the provider crossed
extrema blocker.

## Quarantine decision

Frozen policy `db3_bounded_fx_extrema_quarantine_v1` permits only historical
High/Low rows, at most 10 unique rows and no more than 0.01% of unique
observations. It forbids swap, interpolation, clamp, overwrite, and mixing old
and new DataVersions.

Observed 245 rows exceed both bounds. The correct action is therefore to reject
the whole full-refetch transaction and keep USDJPY unpublished as current.
The threshold has not been relaxed to fit the observation.

## Questions for Saxo support

1. Is DataVersion `29738069` for SIM USDJPY UIC 42 expected to contain
   `HighBid > HighAsk` or `LowBid > LowAsk` for the timestamps supplied above?
2. If not, will a corrected Chart DataVersion be issued?
3. When paging backward with `Mode=UpTo` and `Time` equal to the previous
   page's first sample time, is the duplicated final sample intentionally a
   partial interval? Which occurrence is authoritative for a completed 60-minute
   bar?
4. Does `DisplayAndFormat Decimals=2, Format=Normal` intentionally accompany
   three-decimal USDJPY Chart values, while instrument TickSize is `0.001`?

Do not send an OAuth token, refresh credential, AppKey, account identifier, or
the complete raw archive to support unless a separately approved secure channel
is established. The timestamps, parameters, hashes, and representative values
in this brief are sufficient for initial triage.

## Retry and publication condition

### 2026-07-28 minimal version watch

At `2026-07-28T00:08:46Z`, the isolated `Count=1` watch observed provider
DataVersion `29749254`, which differs from quarantined version `29738069`.
The single response was retained at
`data/acquisition/runs/20260728T000846Z-e815c9e4/instruments/usdjpy/data_version_probe.json`
with SHA-256
`2b459a1ac427779770978f7ae98e91b8f1cadad2b0d4542aebf17d764a39b9b4`.

This is evidence of a new provider revision identity, not evidence that the 245
historical crossed extrema have been corrected. The watch performed one GET,
zero Saxo write requests, zero orders/prechecks, zero DB mutations, and did not
start a full-refetch. USDJPY remains excluded and unpublished. A guarded
single-instrument full-refetch is now eligible for a separate operator decision;
it has not been approved or executed by this observation.

Do not repeat the full-refetch while DataVersion remains `29738069`; it will
repeat the same bounded failure. The active scheduler excludes USDJPY while
continuing EURUSD and ETF 11. A minimal `Count=1` watch may be used to compare
only the provider DataVersion; it does not ingest or publish the returned bar.

```bash
.venv/bin/python -m market_db.usdjpy_version_watch status
.venv/bin/python -m market_db.usdjpy_version_watch probe --auth-mode keychain --callback-port 8765
```

The same quarantined version is discarded without creating another raw
artifact. A different version is retained as an isolated single-response
artifact and reported as `NEW_PROVIDER_DATA_VERSION_REVIEW_REQUIRED`; it only
makes a separately approved guarded full-refetch eligible for consideration.
Publication still requires the full-refetch to pass unchanged quality gates and
two subsequent normal runs to pass.
