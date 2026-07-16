# Phase DB1 実装結果

実施日: 2026-07-16 JST
対象仕様ID: `v13_database_prerequisite_20260716_v2`
対象研究線: `v13categoryintraday`
総合判定: **PASS**

## 1. 結論

Docker Compose上にPostgreSQL 18.4の空データ基盤を構築し、3つの物理database、用途別role、schema、table、view、procedure、checksum付きmigration、管理CLI、validator、運用runbookを実装した。初回適用、同内容再実行、checksum不一致拒否、失敗migrationのtransaction rollback、権限境界、通常restart後の永続性を実コンテナで検証し、全必須gateがPASSした。

Phase DB1の境界どおり、市場CSVのimport、Saxo API接続、派生足生成、research snapshotへのデータ投入、戦略計算は実施していない。市場データtableは全て0件である。

## 2. 実行環境

| 項目 | 実測値 |
|---|---|
| Git branch / HEAD | `main` / `0fd87072e931a9f171ef65309d974a9adbe48624` |
| Docker | `29.3.0` |
| Docker Compose | `5.1.0` |
| image | `postgres:18.4-bookworm` |
| resolved digest | `postgres@sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296` |
| image ID | `sha256:1961f96e6029a02c3812d7cb329a3b03a3ac2bb067058dec17b0f5596aca9296` |
| image OS / architecture | `linux` / `arm64` |
| PostgreSQL server | `18.4 (Debian 18.4-1.pgdg12+1)` |
| timezone | `UTC` |
| host bind | `127.0.0.1:54329` |
| service health | `healthy` |
| named volume / mount | `saxo_pg18_data` / `/var/lib/postgresql` |

password値、接続URL、host固有pathはmanifestへ保存していない。Composeのfile secretにはbootstrap用passwordだけを渡し、用途別roleのpasswordはhost側の`.secrets/`からPsycopgへ個別parameterとして渡す。

## 3. 作成したdatabaseとobject

| database | schema | base table | view | procedure | migration履歴 |
|---|---:|---:|---:|---:|---:|
| `saxo_market` | 8 | 16 | 7 | 4 | 6 |
| `saxo_research_v13` | 8 | 16 | 4 | 0 | 5 |
| `saxo_forward_v13` | 4 | 6 | 0 | 1 | 3 |

`saxo_market`には`catalog`、`ops`、`raw`、`staging`、`curated`、`derived`、`quality`、`analytics`を作成した。research DBはDB2の物理snapshot受入れに必要な同じ8 schemaを空で作成し、forward DBは`catalog`、`ops`、`raw`、`quality`だけを作成した。

`postgres`を含む9 roleを確認した。`saxo_db_owner`はNOLOGINかつ非superuserで、全業務objectのownerである。他の用途別LOGIN roleも非superuser、NOCREATEDB、NOCREATEROLE、NOREPLICATION、NOBYPASSRLSである。

## 4. Migration結果

| No. | file | SHA-256 | target |
|---|---|---|---|
| 0001 | `0001_bootstrap.sql` | `4061c335c47942b6bbca872b79d9d0574f7fef08b101c571e7c80fd4bd9c87f6` | 3 DB |
| 0002 | `0002_market_schema.sql` | `bdb7a0692ea4d60991d4731adf179305f9f8a10b2a618b712c3c8768067b28e6` | market, research |
| 0003 | `0003_research_schema.sql` | `c6397ba5d445bc20c2b44033e1a442b10b838c79ca3354499d44c820d57d17c3` | research |
| 0004 | `0004_forward_schema.sql` | `78ba4e5914fd5c7e8c358e9e338be4ee24ab6a1c205ae01d233931ee3eceb016` | forward |
| 0005 | `0005_grants_and_forward_append.sql` | `3e30e9c24e3bbb227f07297b4d4a890114c9b7f85db8e2f627e6672ac30a0f5f` | 3 DB |
| 0006 | `0006_operational_views.sql` | `de5da692874cdeaada5256d587dc179e03a3c545e895670e51d78fdd31c607f6` | market, research |
| 0007 | `0007_operational_procedures.sql` | `16ef3f5f648db3a71d62bf84762f0a3f68fe7b7c1ca0aa9cff49cd9ec1a88a14` | market |
| 0008 | `0008_quality_privilege_hardening.sql` | `6a0dd0ef768b6cea882008f149ae9e79af901bc9e2e6aaac2a91378bc74960d2` | market |

初回構築とDB1内の追加hardening適用後の履歴は合計14件である。再実行は対象migrationを全て`skipped`とし、履歴を増やさなかった。追跡fileを変更せずに作った一時copyでchecksum不一致を発生させ、DDL前に拒否した。さらに一時migration内でDDL後にゼロ除算を発生させ、probe tableとmigration履歴がともにrollbackされることを確認した。

## 5. データ管理・運用機能

read-only入口として`market_db.inspect`を実装した。market DBでは次の8 subcommandを実接続で確認した。

| subcommand | DB1空DBでの結果 | role |
|---|---|---|
| `inventory` | 0件 | `saxo_app_reader` |
| `coverage` | 0件 | `saxo_analyst_reader` |
| `freshness` | 0件 | `saxo_app_reader` |
| `runs` | 0件 | `saxo_app_reader` |
| `quality` | 0件 | `saxo_app_reader` |
| `lineage` | 0件 | `saxo_analyst_reader` |
| `storage` | schema/table使用量を返却 | `saxo_analyst_reader` |
| `backups` | 3 DB、未実施状態を返却 | `saxo_app_reader` |

research DBではinventory、coverage、lineage、storageだけを`v13_research_reader`で確認した。forward DB、任意database、任意SQL、writer/migrator/superuserへのfallbackは受け付けない。table/JSON形式と`--fail-on-alert`のexit code contractもtestした。

状態更新入口として`market_db.operate`を実装し、次のSECURITY DEFINER procedureだけを`saxo_ops_operator`へ公開した。

- `quality.acknowledge_event`
- `quality.resolve_event`
- `ops.start_backup_run`
- `ops.finish_backup_run`

4 procedureは`saxo_db_owner`所有、固定`search_path=pg_catalog`、PUBLIC EXECUTE剥奪済みである。qualityのOPEN→ACKNOWLEDGED→RESOLVEDとbackupのRUNNING→PASSをtransaction内fixtureで実行し、直接DML拒否を確認後に全fixtureをrollbackした。`0008`では`saxo_ingest`のquality権限も`SELECT/INSERT`だけへ縮小し、ACK/RESOLVEの直接UPDATEを拒否した。forward append procedureも同様に成功経路と直接SELECT/DML拒否を確認し、rollbackした。

運用手順は`docs/database_operations_runbook.md`へ固定した。DB1では起動、停止、restart、health、inspect、migration、checksum不一致、secret rotation方針、port/Docker/disk障害、禁止操作を実行可能な範囲で記載した。import、実backup/restore、retention、read APIは後続Phaseとして明示的にLOCKEDとした。

## 6. 検証結果

最終test command:

```bash
SAXO_DB_INTEGRATION=1 .venv/bin/python -m pytest -q
```

結果は`28 passed`、failure 0、skip 0である。unit/staticだけでなく、実PostgreSQL接続を必要とするintegration markerも全件実行した。

最終validator command:

```bash
.venv/bin/python -m market_db.validate --phase db1
```

結果は`PASS`。69 CSV、781,808行、160,403,659 bytesをinventoryに対して再計算し、missing、size mismatch、SHA-256 mismatchは全て0だった。inventory file自身のSHA-256は`72abbdcedd75b290b46d4ca8396125ebe99863e16e8c570c0f06fdf8440282db`である。

通常restart後に再度health、3 DB、schema数、14 migration checksum、市場table 0件を検証し、named volumeのmountを確認した。repository内の非CSV text 54 fileを、実secret 8値との完全一致で検査し、検出0件だった。

## 7. 仕様整合性の修正

実装前レビューで、`catalog.session_interval`のHOLIDAY行はopen/closeをNULLにする仕様である一方、旧primary keyが`open_time_utc`を含み暗黙にNOT NULLとなる矛盾を検出した。市場データを扱う前に、人間向け・機械可読仕様を次へ整合させた。

- primary key: `(session_calendar_id, session_date, interval_sequence)`
- `interval_sequence`を非負の必須列として追加
- HOLIDAYはopen/closeともNULL
- OPEN/SHORT_SESSIONはopen必須かつclose > open

実装migrationとtestは修正後の仕様に一致する。

## 8. DB1で実施していないこと

- 69 CSVのdatabase import
- Saxo OpenAPI接続、token入力、market data request
- order/precheck request
- watermarkの業務更新
- 4H・1D派生系列生成
- research snapshotへの市場データ投入
- read API、実backup、restore、retention
- feature、signal、position、cost、PnL、WFO、Holdout、portfolio計算

Saxo API call数、order/precheck数、戦略計算数はいずれも0である。

## 9. Gate

```text
DB0 v2  RE-FROZEN
DB1     PASS
DB2     NEXT
DB3     LOCKED
DB4     LOCKED
RT0     LOCKED UNTIL DB4 PASS
```

次に解放できるのはDB2だけである。DB2のimportは本結果とは別のユーザー指示を受けるまで開始しない。
