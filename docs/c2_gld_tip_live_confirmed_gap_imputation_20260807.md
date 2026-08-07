# saxo_market_live GLD/TIP 確認済み4 slot補完

## 適用範囲

`saxo_market_live` の provider 欠損は、read-only coverage確認で次の4 slotだけと確定した。

- GLD: 2026-07-29 13:30Z、14:30Z
- TIP: 2026-07-29 13:30Z、14:30Z

補完元は両銘柄とも直前のactual provider barである2026-07-28 19:30Zとする。GLDはDataVersion 29749768、TIPは29759068であり、補完先sessionの15:30Z以降とterminal 19:30Zにも同一DataVersionのactual PASSがある。

## 安全境界

migration 0039は、この4 slot、2銘柄、session、DataVersion、source timestamp、ingestion run、payload SHA-256、artifact path、review IDの組合せだけを許可するDB外部キーを追加する。コード側plannerも同じscope以外を`BLOCKED_UNAPPROVED_IMPUTATION_SCOPE`として補完しない。

補完は`derived.c2_market_bar_1h_imputation`へappendする。`raw.market_bar_revision`、`curated.market_bar`、watermark、canonical 4H/1Dは更新・削除しない。OHLCは直前actual closeを4値へ入れ、volumeはnull、`source_kind=IMPUTED_PREVIOUS_VALID`、`quality_status=WARN`とする。official close、total return、execution priceのclaimは全てfalseである。

各slotにOPEN/WARNの`quality.event`を残す。`analytics.v_c2_confirmed_provider_gap_warning`と`GET /api/v1/c2/hourly-overlay`は欠損時刻、補完方法、元timestamp、DataVersion、ingestion run、payload SHA-256、artifact path、review ID、品質eventを表示する。canonical coverageは欠損を隠さないためGLD/TIPともWARNのまま維持される。

## 適用・検証

外部APIとschedulerを使わず、`SAXO_MARKET_DB=saxo_market_live`を明示してmigration 0039だけをforward適用する。適用前後でraw/curatedのrow countとordered digestが一致すること、overlay/allow-list/OPEN WARNが各4件であること、GLD/TIP以外または別時刻の補完が0件であることを確認する。
