# FX 1H gap classification summary

- Calendar: `SBFX_24X5` / `verified_complete_hour_v1`
- Price basis: `bid_ask_mid`
- Detail SHA-256: `f30d68553dc6a66ac6ce40964acd0aa9131827074ac916269159688fae5a1471`
- Price interpolation: **not performed**
- Orders / prechecks: **0 / 0**

| Instrument | Missing | Blocking | Unclassified | Cause counts |
|---|---:|---:|---:|---|
| EURUSD | 576 | 0 | 0 | QUARANTINED_VALUE_ANOMALY=6, SAXO_RAW_NO_SAMPLE=570 |
| USDJPY | 405 | 0 | 0 | QUARANTINED_VALUE_ANOMALY=3, SAXO_RAW_NO_SAMPLE=402 |

## Coverage reconciliation

- EURUSD: classified_missing=576, curated_duplicate_rows=0, curated_incomplete_rows=1
- USDJPY: classified_missing=405, curated_duplicate_rows=0, curated_incomplete_rows=1

## Cross-instrument overlap

- Common: 402
- EURUSD only: 174
- USDJPY only: 3

Historical coverage warnings remain separate from freshness, current content quality, and interface status.
A missing source observation is retained as source coverage evidence and is never synthesized.
