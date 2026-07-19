# DMI3 Stable total-return API 実装結果

更新日: 2026-07-20 JST

状態: **PASS / DMI4 NEXT**

## 実装

- `GET /api/v1/total-return` を追加した。
- `catalog.series_instrument_mapping`でsource datasetとcatalog instrumentを明示的に対応付けた。
- symbol文字列だけの暗黙joinを禁止し、approved mapping以外は公開対象にしない。
- mappingとtotal-return rowsを同一 `REPEATABLE READ / READ ONLY` transactionで取得する。
- `eligibility=eligible`はPASS行だけ、`stored_complete`はWARN/NOT_EVALUATEDを含む可能性をwarningで明示する。
- responseの`value`はtotal-return index、`price_basis`は`etf_total_return`に固定し、native OHLCと分離した。
- `source_dataset_id`未指定で候補が複数ある場合は`SOURCE_DATASET_REQUIRED`で停止する。

## Mapping

- migration 0019を適用した。
- ETF11の11系列（EEM、EFA、GLD、IEF、IWM、LQD、SHY、SPY、TIP、TLT、VNQ）を承認済みmappingとして登録した。
- mappingのsource datasetは`20260712T135236Z`、承認者は`codex-dmi3-20260720`。
- 未承認mapping 0件、ambiguous mapping 0件。

## 検証結果

- 非integration回帰: `95 passed, 37 deselected`
- DMI3実DBintegration: `3 passed`
- IWMのAPI rowsと`curated.etf_total_return_daily`のdate/value/volume/quality parity: PASS
- 実機HTTP: `200`、`price_basis=etf_total_return`、`row_count=5`、`truncated=true`
- unknown series: `404 TOTAL_RETURN_MAPPING_NOT_FOUND`
- invalid eligibility: `400 INVALID_REQUEST`
- POST: `405 READ_ONLY_API`
- DB write route、Saxo write request、access token保存: 0 / false

## 次のゲート

DMI3をPASSとして確定し、DMI4（cursor・v1 contract kit）を開始できる。
