# saxo_db 独立データ管理プロジェクト計画

作成日: 2026-07-16
状態: **DB1 PASS / DB2 PASS / DB3 PASS / DB4 NEXT**

## 1. 目的

`saxo_api`の責務を、Saxo認証・データ取得・既存研究の監査元に限定する。データの永続管理、増分更新、品質履歴、研究snapshot、分析query、格納データの棚卸し、freshness・lineage監視、backup/restoreは独立した`saxo_db`で実施する。

元の`saxo_api`ファイルは移動・削除・変更せず、検証済みのcopyだけを本プロジェクトへ置く。戦略実装・PnL・WFOは本データ基盤の完成後に別プロジェクトから実行する。

## 2. 引き継いだ仕様

- `v13_phase_db0_database_implementation_spec.md`
- `v13_phase_db0_database_spec.json`
- `v13_category_specific_intraday_strategy_research_plan.md`
- `v13_revised_strategy_research_plan.json`
- `v13_phase_ra0_market_characterization_spec.json`
- `v12_intraday_collection_result_20260716.md`
- Saxo intraday、日次、ETF日次、total-return、RA0分析のsource manifest

bootstrap時点のv1仕様とhashはGit履歴および既存manifestで監査可能なまま保持する。現在の正本は、ユーザー指示によりデータ管理運用を追加して再凍結したv2仕様とする。移管済みCSV、source manifest、研究成果物は変更せず、新しい相対path、import区分、dataset台帳、運用確認要件は`specs/saxo_db_import_spec.json`へ明示する。

## 3. import bundle

| グループ | CSV数 | CSV行数 | 用途 |
|---|---:|---:|---|
| `intraday/normalized` | 26 | 525,381 | Saxo 13銘柄 × 1H/4H raw-normalized |
| `intraday/collection_summary.csv` | 1 | 26 | import照合・品質metadata |
| `daily/saxo_multi_asset` | 8 | 52,692 | 旧6市場の日足、instrument master、summary |
| `daily/saxo_etf_raw` | 14 | 58,592 | Saxo ETF日足12系列、master、summary |
| `daily/etf11_sources` | 14 | 90,727 | ETF total-returnとmacroの外部原本 |
| `daily/curated_etf_total_return` | 1 | 54,285 | 分配・分割調整済みETF統合日次 |
| `analysis_baseline` | 5 | 105 | Phase RA0記述統計・解釈 |

CSV総数は69、行数合計は781,808。raw、curated、分析出力を合算した値なので、同じ市場観測の派生データを含み、unique market bar数を意味しない。

## 4. DB import区分

### 4.1 Raw market data

- `intraday/normalized/*.csv`
- `daily/saxo_multi_asset/*_daily.csv`
- `daily/saxo_etf_raw/*_daily.csv`

価格barは`raw.market_bar_revision`へ取り込み、品質合格・最新値だけを`curated.market_bar`へ送る。`collection_summary.csv`と`instrument_master.csv`は価格barとしてimportしない。

### 4.2 External reference / total return

- `daily/etf11_sources/etf/*.csv`
- `daily/etf11_sources/macro/*.csv`
- `daily/curated_etf_total_return/etf_daily.csv`

Saxo raw OHLCとはsource、price basis、tableを分ける。調整済み日次の値でraw Saxo価格を上書きしない。

### 4.3 Analysis baseline

`analysis_baseline`は市場bar tableへ入れず、RA0再現照合用のresearch metadataとして登録する。DB移行後に同じcutoff・同じ入力で再集計し、件数とSHA-256を比較する。

## 5. Import順序

1. source inventoryとmanifestを検証
2. source datasetとinstrument masterを`catalog`へ登録
3. ingestion runとsource fileを`ops`へ登録し、source datasetへ関連付ける
4. Saxo intraday 1H/4Hを`raw`へCOPY
5. Saxo日次を`raw`へCOPY
6. 品質合格1Hを`curated`へ登録
7. ETF external sourceとtotal-return日次を専用tableへ登録
8. 2024-06-28以前だけで研究DBを物理作成
9. RA0 baselineをresearch metadataとして登録
10. snapshot用pg_dump、SHA-256、`pg_restore --list`、manifestを作成

4H・1D生成、RA0再計算はDB3、一般backup/restore smoke testはDB4で実施する。DB2のsnapshot dump作成とDB4の汎用backup/restore運用を混同しない。

## 6. Phase計画

### SDB0 — 独立プロジェクト準備（完了）

- ディレクトリ作成
- 仕様書・計画書・manifestコピー
- 69 CSVコピー
- source/copy SHA-256照合
- DB実装・接続・API呼出しはゼロ

### DB1 — Docker/PostgreSQL基盤（PASS）

- Docker Composeとsecret
- PostgreSQL 18.4
- database、role、schema、migration
- source dataset、session calendar、backup run、quality lifecycleの空table
- inventory、coverage、freshness、lineage、run、quality、storage、backupのread-only view/CLI
- procedure-only ops operatorとquality/backup状態更新CLI
- database operations runbook
- localhost-only接続とhealthcheck

### DB2 — データ移行（PASS）

- raw 636,629行、reference 90,894行、curated 1H 394,992行、ETF total-return 54,285行を区分import
- 69 source file、781,808 source row、最小最大時刻、SHA-256を照合
- source dataset台帳6件、instrument 18件、lineage不一致0件を確認
- known quality FAIL 5件をOPEN eventとして保持
- research snapshotの物理分離・read-only化、dump SHA-256、`pg_restore --list`を検証

### DB3 — 増分更新（PASS）

- SIM限定GET client、canonical 13 detail/schedule照合、token redactionを実装
- Etf 20・FxSpot 72実バーoverlap、revision、idempotent upsertを実装
- quality失敗時にcurated/derived/watermarkをrollbackし、raw artifactと失敗runを保持
- DataVersion変化を`STALE_DATA_VERSION`で停止し、対象1銘柄だけのguard付き全履歴refetchを実装
- full-refetch限定で、過去FX High/Low交差を最大10件かつ0.01%以下だけ無補正隔離し、raw原本・`rejected_rows`・解決済みWARNへ監査記録
- localhost限定・token非保存・固定reconcile専用のoperator UIでAI側運用を可能にする
- 受理済み完成1Hから4H 128,469行・1D 47,784行を生成
- US ETF holiday・短縮取引・DST・例外休場を登録。FXはlive schedule照合までprovisional
- missingとout-of-sessionを分けたcoverage、calendar基準freshness、watermark、failed run監視を実装
- offline/live test・総合validator PASS、token永続化ゼロ、注文APIゼロ
- canonical 13 watermark `ACTIVE`、通常run 104・105連続PASS

### DB4 — 分析・運用（NEXT）

- 運用CLIとread-only query APIの一致
- inventory・coverage・freshness・lineage・quality・storage監視
- backup/restore
- backup実績台帳、retention、runbook drill
- Parquet/DuckDB read-only export

DB4がPASSするまで、元計画のRT0戦略検証は再開しない。

## 7. データ管理運用

標準の確認入口は`python3 -m market_db.inspect`とし、任意SQLを入力させない。運用者は次をread-onlyで確認できるようにする。

- 何のdataset・銘柄・layer・price basis・horizonが格納されているか
- 件数、min/max時刻、最新完成時刻、freshness
- 完成/未完成、重複、欠損、品質状態
- source dataset/fileからingestion run、raw、curated/derivedまでのlineage
- failed ingestion runとOPEN/ACKNOWLEDGED quality event
- database/schema/table別のstorage使用量
- 最終backup、SHA-256検証、restore smoke test、retention状態

品質eventのacknowledge/resolveとbackup runの状態更新は、`python3 -m market_db.operate`から`saxo_ops_operator`の許可済みprocedureだけを実行する。operatorにtable直接DML、任意function、市場データ変更を許可しない。

起動、停止、通常restart、確認、migration、secret rotation、backup/restore、障害対応は`docs/database_operations_runbook.md`へ固定する。DB1では空DBで安全に動くこと、DB2ではimport後の実件数、DB3ではfreshnessと更新状態、DB4ではAPI・backup・runbook drillまでをgateにする。

## 8. 現在の禁止事項

- 移管CSV、source file台帳、raw履歴、凍結済みresearch snapshotの手動変更・再初期化
- token・口座情報保存
- strategy signal、PnL、WFO、portfolio計算

Saxo APIはDB3のSIM限定GET allow-listだけを許可する。DB1・DB2・DB3は実環境でPASSした。次に許可する作業はDB4のread API、backup/restore、retention、runbook運用ゲートだけであり、RT0はDB4 PASSまでLOCKEDとする。
