# DMI2A Atomic series status 実装結果

更新日: 2026-07-20 JST

状態: **PASS / DMI2B NEXT / DMI3–DMI4 LOCKED**

## 実装

- `GET /api/v1/series-status`を追加した。
- 初期対象をcanonical 1Hに限定し、`instrument_key`、`layer`、`price_basis`を必須にした。
- identity、coverage、freshness、scope適合quality event、watermark、latest run、quality high-watermarkを1つの`REPEATABLE READ / READ ONLY` transactionで取得する。
- `read_at_utc`、snapshot marker、watermark data version、latest run ID、quality event high-watermarkをcomponent revisionとして返す。
- UNKNOWN ERROR/CRITICALをfail-closedでBLOCKEDにする。
- raw archiveなどlayer/horizon/price basisが異なるeventをcanonical 1H blockerへ混入させない。
- migration 0018でleast-privileged reader向けの`quality.v_event_high_watermark`を追加した。

## Current runtime evidence

SPY 1H / `native_ohlc`はHTTP 200で、quality blocker 0、UNKNOWN 0、historical unresolved 3、latest run 105 PASSを同一snapshotから返した。coverage WARNとfreshness STALEのため、現在の`eligibility_status`は正しくBLOCKEDである。

EURUSDへ誤った`native_ohlc`を指定したrequestはfallbackせず`SERIES_NOT_FOUND`となる。正しいprice basisは`bid_ask_mid`である。

## Security

- loopback bind、`saxo_app_reader`、read-only transactionを維持。
- write route、任意SQL、Saxo token入力を追加していない。
- AccountKey、口座識別子、tokenを保存していない。
- 注文・precheck・Saxo write requestは0件。
