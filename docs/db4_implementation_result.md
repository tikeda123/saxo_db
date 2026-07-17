# Phase DB4 実装結果

実施日: 2026-07-17 JST

判定: **PASS**

次工程: **RT0 NEXT**

## 1. Read API

- `market_db.read_api`を`127.0.0.1:8766`だけへbindするFlask APIとして実装した。
- DB接続は`saxo_app_reader`、read-only transaction、statement timeout 30秒、pool最大5接続である。
- write route、任意SQL、DB管理UI、Saxo token入力は存在しない。
- healthはHTTP 200、database=`saxo_market`、role=`saxo_app_reader`、transaction read-only=`on`を確認した。
- inventory、coverage、freshness、runs、quality、lineage、storage、backupsの8 viewは、時計依存の`freshness_seconds`と`age_seconds`を除く安定列がCLIと一致した。
- dataset 7件、snapshot 1件、1H 480,355行、4H 128,469行、1D 47,784行をAPIで参照した。
- IWMの2026-07-15から2026-07-17までの1H bar 13行を期間指定で取得した。

## 2. Backup・restore

3 DBをPostgreSQL 18のcustom formatでbackupし、すべてSHA-256、size、`pg_restore --list`、`ops.backup_run`をPASSにした。

| DB | backup run | size bytes | 検証 |
|---|---:|---:|---|
| `saxo_market` | 26 | 92,693,684 | SHA-256 / list / restore smoke PASS |
| `saxo_research_v13` | 27 | 53,397,996 | SHA-256 / list PASS |
| `saxo_forward_v13` | 28 | 20,542 | SHA-256 / list PASS |

market backupはランダム名の一時DBへ実restoreし、migration、instrument、ingestion、raw、curated、derived、snapshotの件数、snapshot cutoff、主キー重複0を元DBと一致させた。一時DBは照合後に削除され、`ops.record_restore_smoke`を通じてPASSを記録した。

## 3. Retention

daily 7世代・weekly 4世代をDBごとに独立して保持する。最初のdrillで全DB共通に世代計算していた問題を検出し、DB別計算へ修正した。削除されたresearch・forward dumpは再作成し、修正後のdry-runとapplyはいずれも削除候補0・削除0でPASSした。命名規則外のDB2 snapshot dumpは対象外である。

## 4. Parquet export

`market_db.export_parquet`はread APIと同じinstrument/layer/start/end境界を使い、`exports/parquet/`だけへ出力する。IWM 1Hの13行をZSTD Parquetへ出力し、SHA-256、size、DuckDB read-back 13行を確認した。Parquet本体とruntime manifestはGit管理外であり、機械可読DB4 manifestから相対pathで参照する。

## 5. Security・非実施範囲

- access token永続化0、account identifier永続化0
- database write route 0、任意SQL 0
- Saxo write/order/precheck request 0
- raw、curated、research snapshotの手動変更0
- strategy、signal、PnL、WFO、Holdout、portfolio計算0

## 6. 総合検証

- `SAXO_DB_INTEGRATION=1 .venv/bin/python -m pytest -q`: **90 passed in 569.16s**
- `.venv/bin/python -m market_db.validate --phase db4`: **PASS**
- migration 0013 checksum、DB3 offline/live、69 CSV inventory、3 DB health、research cutoff/read-onlyを回帰確認した。

DB4はデータ基盤と運用のPASSであり、戦略の有効性を意味しない。DB1〜DB4の完了により次にRT0だけを解放する。
