# Phase DB2 実装計画書

作成日: 2026-07-17 JST
対象仕様ID: `v13_database_prerequisite_20260716_v2`
前提: `DB1=PASS`
状態: **IMPLEMENTED / RUNTIME GATE PASS**

## 1. 目的

検証済み69 CSVを変更せず、全source fileを台帳登録し、各source rowを用途別のdatabase layerへ再現可能に移行する。import後に件数、期間、checksum、lineageを照合し、`2024-06-28T23:59:59Z`以前だけを`saxo_research_v13`へ物理snapshotとして凍結する。

DB2はデータ移行gateであり、Saxo API増分更新、4H/1D派生生成、read API、一般backup運用、戦略研究を実施しない。

## 2. 入力分類

| 保存先 | CSV | source row | 内容 |
|---|---:|---:|---|
| `raw.market_bar_revision` | 44 | 636,629 | intraday 1H/4HとSaxo daily reference |
| `curated.etf_total_return_daily` | 1 | 54,285 | 調整済みETF total-return |
| `raw.reference_observation` | 24 | 90,894 | collection summary、instrument master、ETF外部原本、macro、RA0 baseline |
| 合計 | 69 | 781,808 | inventory全source row |

1Hの394,992行はrawとcurated latestの両方へ保存する。うち完成足394,979行を`PASS`、未完成13行を`NOT_EVALUATED`とする。raw 4H 130,389行はarchiveだけに保存し、EURUSD/USDJPYの既知品質FAILを修正・削除しない。Saxo daily 111,248行は`horizon_minutes=1440`のlegacy referenceとしてrawに保持する。

## 3. 実装成果物

- `db/migrations/0009_db2_import_support.sql`
- `market_db/import_legacy.py`
- `market_db/research_snapshot.py`
- `market_db/validate.py`のDB2 gate
- `tests/test_db2_import.py`
- `tests/test_db2_integration.py`
- `manifests/db2_research_snapshot_content.json`
- `manifests/db2_research_snapshot_dump.json`
- `manifests/db2_implementation_manifest.json`
- `docs/db2_implementation_result.md`
- `docs/database_operations_runbook.md`のDB2追記

## 4. Schema補完

`0009`では全CSV rowを損失なく登録するため、JSONB payloadを持つ`raw.reference_observation`を追加する。`curated.etf_total_return_daily`へ`source_file_id`を追加し、source fileまで追跡できるようにする。

inventoryは既存market inventoryとreference inventoryを統合する。lineageはsource file単位でraw/reference、curated、derived件数を返す。session calendarはDB3で登録するため、DB2のcoverageは`NOT_EVALUATED`を維持する。

## 5. Import手順

1. `manifests/import_file_inventory.csv`と69 CSVのsize/SHA-256を全件再検証する。
2. 6 source datasetとSaxo instrumentをcatalogへidempotentに登録する。
3. 1 source fileにつき1 ingestion runを作成し、repository相対path、hash、size、row countを登録する。
4. Psycopg COPYで各fileを分類先へ投入する。
5. 1Hだけをcurated latestへ複製し、raw 4H/dailyはcuratedへ送らない。
6. quality summaryの既知FAIL 5件をOPEN eventとして記録する。
7. file単位transactionとadvisory lockにより、中断後の同一hash再実行を安全なskipにする。既存pathのhash差異は拒否する。

## 6. Research snapshot

market DBの受理済み内容から、market barは`time_utc <= 2024-06-28T23:59:59Z`、total-return/referenceは同日以前に限定してresearch DBへcopyする。RA0 baselineはこのcutoffで作成済みのresearch metadataとして全件copyする。

copyはdatabase間linkを使わず、2つの独立接続間でCOPYする。snapshot内容manifestのSHA-256を`ops.research_snapshot`へ記録し、research databaseのdefault transactionをread-onlyへ変更する。その後custom-format `pg_dump`を作成し、SHA-256と`pg_restore --list`を検証する。restore smoke testとretentionはDB4までLOCKEDとする。

## 7. Gate

- 69 source file、781,808 source rowが全件分類・照合される。
- raw 636,629、curated 1H 394,992、total-return 54,285、reference 90,894が一致する。
- raw 4Hの品質FAIL原本が保持される。
- source fileごとのlineage件数がinventory row countと一致する。
- session calendar未登録のcoverageは`NOT_EVALUATED`である。
- research snapshotの全market timestamp/dateがcutoff以下である。
- research readerはmarket DBへ接続できず、research DBでwriteできない。
- snapshot dump hashと`pg_restore --list`がPASSする。
- source CSV、credential、Saxo API、注文、戦略計算の副作用が0である。

全項目が実環境で成功した場合だけDB2をPASSとする。DB2 PASSで解放できるのはDB3だけである。

## 8. 実施結果

2026-07-17 JSTに本計画を実コンテナへ適用し、DB2をPASSと判定した。

- 69 CSV、781,808 source row、160,403,659 bytesを再検証し、missing・size mismatch・SHA-256 mismatchは0件だった。
- market DBへraw market bar 636,629行、reference 90,894行、curated 1H 394,992行、ETF total-return 54,285行を登録した。
- 69 ingestion runと69 source fileを登録し、source file単位のlineage不一致は0件だった。
- 既知品質FAILは修正せず、ERROR/OPENのquality event 5件として保持した。
- coverageはsession calendar未登録のため、仕様どおり`NOT_EVALUATED`を維持した。
- research DBへcutoff以前だけをcopyし、raw 544,397行、curated 329,745行、total-return 54,285行、reference 83,978行を凍結した。cutoff超過行は0件である。
- research DBはdefault read-onlyで、snapshot content manifestと約52MBのcustom-format dumpを作成した。dump SHA-256と`pg_restore --list`はPASSした。
- importとsnapshot作成の再実行は既存内容を変更せず、それぞれ69 file skip、snapshot skipとなった。
- 全統合testは`39 passed`、DB2 validatorは`PASS`だった。

実測値と未実施範囲は`docs/db2_implementation_result.md`、機械可読証跡は`manifests/db2_implementation_manifest.json`を正本とする。DB3はNEXT、DB4とRT0はLOCKEDのままである。
