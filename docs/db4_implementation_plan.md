# Phase DB4 実装計画書

作成日: 2026-07-17 JST

前提: `DB1=PASS / DB2=PASS / DB3=PASS`

状態: **IMPLEMENTATION COMPLETE / DB4 PASS / RT0 NEXT**

## 1. 目的

DB4は、DB3までに作成した市場データ正本を変更せず、運用者と後続分析processが安全に参照・backup・restore検証できる状態を完成させるPhaseである。任意SQL、DB管理UI、Saxo write API、特徴量、signal、PnL、WFO、Holdoutは実装しない。

## 2. 実装範囲

1. Flask read-only API
   - bindは`127.0.0.1`だけ
   - `saxo_app_reader`と最大5 connection pool
   - parameterized SQLと固定relation allow-listだけ
   - inventory、coverage、freshness、lineage、runs、quality、storage、backup status
   - dataset/snapshot manifest、1H/4H/1D件数
   - bar取得はinstrument、layer、UTC start/end必須、最大10,000行
   - write method、任意SQL、DB管理操作、token入力を実装しない
2. Backup・restore
   - PostgreSQLコンテナ内の`pg_dump -Fc`を使用
   - repository相対`backups/postgres/`だけへatomic保存
   - SHA-256、size、`pg_restore --list`をmanifestと`ops.backup_run`へ記録
   - ランダムな`saxo_db4_restore_*`一時DBだけへrestoreし、source/restore件数を照合後に削除
   - restore結果はSECURITY DEFINER procedure経由で記録し、operatorの直接DMLを許可しない
3. Retention
   - daily 7世代、weekly 4世代
   - dry-runを既定とし、明示的なapply時だけ対象dumpと対応manifestを削除
   - backup root外、命名規則外、最新保持対象は削除しない
4. Parquet/DuckDB export
   - `exports/parquet/`だけへ出力しGit管理外
   - bar APIと同じinstrument/layer/start/end境界
   - read-only role、最大100,000行、ZSTD Parquet
   - SHA-256 manifestとDuckDB read-back row countを検証

## 3. Migration

`0013_db4_read_api_and_restore_smoke.sql`を`saxo_market`だけへ適用する。

- `ops.record_restore_smoke`を追加
- ownerは`saxo_db_owner`
- `search_path=pg_catalog`固定
- PUBLIC EXECUTE剥奪、`saxo_ops_operator`だけへgrant
- Flaskに必要な既存viewとbar tableのSELECTだけを`saxo_app_reader`へ追加
- raw、staging、quality table、backup tableへの直接DMLは追加しない

## 4. Runtime drill

1. migration checksumとDB healthを確認する。
2. APIとCLIの同一view結果を件数・先頭rowで照合する。
3. `saxo_market`、`saxo_research_v13`、`saxo_forward_v13`のcustom-format backupを作る。
4. 各dumpのSHA-256と`pg_restore --list`を確認する。
5. 少なくともmarket DBを別名一時DBへrestoreし、主要table件数と主キー重複0件をsourceと照合する。
6. retention dry-runとapplyを実行し、保持対象が削除されないことを確認する。
7. 短い期間のParquet exportを作り、DuckDB read-back件数とSHA-256を確認する。
8. DB4 validator、unit/static/integration testを実行する。

## 5. PASS条件

- read APIがloopback・read-only・allow-list・期間必須で動作する。
- APIとinspect CLIの運用view結果が一致する。
- 3 DBのbackupがPASSで、dump SHA-256と`pg_restore --list`が検証済みである。
- 一時DB restore smokeがPASSし、試験DBが後始末されている。
- retentionがdaily 7・weekly 4を保持し、root外を変更しない。
- Parquet exportがread-onlyで再読込できる。
- migration checksum、DB1–DB3 gate、research read-only/cutoffを壊していない。
- token/account情報保存0、Saxo write/order/precheck 0、戦略計算0である。

全条件PASS後だけDB4をPASSとし、RT0を次工程として解放できる。

2026-07-17の実施結果は`docs/db4_implementation_result.md`、機械可読証跡は`manifests/db4_implementation_manifest.json`を正本とする。全条件をPASSし、RT0を次工程として解放した。
