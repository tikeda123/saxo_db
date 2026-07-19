# DMI2B Snapshot-bound read 実装結果

更新日: 2026-07-20 JST

状態: **PASS / DMI3 NEXT / DMI4 LOCKED**

## 実装

- `GET /api/v1/snapshots/{snapshot_id}/bars` を追加した。
- `saxo_research_v13` の `v13_research_reader` を current API と分離した専用固定poolで参照する。
- metadata、manifest、snapshot integrity、series identity、bar rowsを同一 `REPEATABLE READ / READ ONLY` transactionで取得する。
- 初期対応は検証済みsnapshot 1のcurated 1H native OHLCに限定する。
- 4H/1D、未知snapshot、未知series、未検証manifest、metadata・件数・cutoff・SHA不一致はfail-closedにする。
- current `saxo_market` へのfallback、FDW、dblink、cross-database link、write routeは追加していない。
- responseにrequested/resolved snapshot ID、snapshot SHA、cutoff、source/snapshot database、query、ordered content SHAを返す。

## 検証結果

- 非integration回帰: `91 passed, 34 deselected`
- DMI2A/DMI2B実DBテスト: `5 passed`
- 実機HTTP: 1H `200 / integrity PASS / row_count 7 / truncated false`
- 実機HTTP: 4H `409 SNAPSHOT_LAYER_NOT_AVAILABLE`
- 実機HTTP: 未知snapshot `404 SNAPSHOT_NOT_FOUND`
- 実機HTTP: POST `405 READ_ONLY_API`
- current DB側のsession-local temporary table更新前後で、snapshot SHA、row count、全row、ordered content SHAは不変。

## 次のゲート

DMI2BをPASSとして確定し、次はDMI3（stable total-return API）を開始できる。DMI4はDMI3完了までLOCKEDのままとする。
