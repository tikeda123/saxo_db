# Phase DB0 データベース実装仕様書

作成日: 2026-07-16  
対象研究線: `v13categoryintraday`  
仕様ID: `v13_database_prerequisite_20260716_v1`  
状態: **SPEC FROZEN / IMPLEMENTATION NOT STARTED**

## 1. 目的と現在のゲート

戦略検証に入る前に、既存の1H・4H・1Dデータを再現可能に保管し、Saxo APIから最新1Hデータを重複なく追加できるデータ管理基盤を構築する。

本Phase DB0は実装仕様の凍結だけである。Docker imageのpull、コンテナ起動、volume作成、Python dependency追加、データ投入、Saxo API呼出し、戦略PnL計算は行わない。

従来の次Phase `RT0` は一時的に閉じる。新しい順序は次のとおりとする。

```text
DB0 仕様凍結（本書・完了）
  → DB1 Docker/PostgreSQL・role・schema構築
  → DB2 既存データ移行・研究snapshot凍結
  → DB3 増分更新・revision・4H/1D生成
  → DB4 読取API・backup/restore・運用ゲート
  → RT0 戦略ルール・コスト・trial凍結
```

DB1–DB4がすべてPASSするまで、戦略のパラメータ探索、PnL、WFO、portfolio allocationを開始しない。

## 2. 採用アーキテクチャ

### 2.1 永続DBはPostgreSQLを採用する

Dockerコンテナで動作する永続DBにはPostgreSQLを採用する。DuckDBは組込み型であり、複数のホストプロセスから更新する常駐DBサーバーとして扱うより、Parquet分析やread-only exportへ限定する方が役割に合う。

採用バージョンは公式Docker image `postgres:18.4-bookworm` とする。PostgreSQL 18は現在サポート対象で、18.4は現行minor releaseである。[PostgreSQL versioning policy](https://www.postgresql.org/support/versioning/)、[Docker Official Image](https://hub.docker.com/_/postgres)

Docker Hub上の可変tag `latest`、`18`は使わない。DB1で`18.4-bookworm`をpullした後、Apple Silicon用に解決されたimage digestとimage IDをmanifestへ記録する。`platform`を固定せず、公式multi-architecture imageの`linux/arm64`を利用する。

Python接続は`psycopg[binary,pool]==3.3.4`へ固定する。Psycopg 3はPython 3.13をサポートし、COPYとconnection poolを利用できる。[Psycopg package](https://pypi.org/project/psycopg/)、[Psycopg documentation](https://www.psycopg.org/psycopg3/docs/basic/index.html)

### 2.2 TimescaleDBとpartitionは初期導入しない

現在は26 intraday CSV、525,381行であり、通常のPostgreSQL tableと複合主キー・indexで十分である。PostgreSQL公式文書も、partitionの効果はtableが非常に大きい場合に現れ、目安としてtableがDB serverの物理memoryを超える場合を挙げている。[PostgreSQL partitioning](https://www.postgresql.org/docs/18/ddl-partitioning.html)

次のいずれかに達した場合だけpartition設計を再評価する。

- `curated.market_bar`が1,000万行以上
- relation合計が8GB以上
- 代表的な期間検索のp95が2秒超

初期段階で月次partitionやTimescaleDBを入れて、migrationとunique constraintを複雑にしない。

### 2.3 Docker構成

DB1で作成する`compose.yaml`の固定仕様は次のとおりである。

| 項目 | 仕様 |
|---|---|
| Compose project | `saxo-market-data` |
| service | `postgres` |
| image | `postgres:18.4-bookworm` |
| host bind | `127.0.0.1:54329` |
| container port | `5432` |
| named volume | `saxo_pg18_data` |
| volume mount | `/var/lib/postgresql` |
| timezone | UTC |
| restart | `unless-stopped` |
| healthcheck | `pg_isready`で`saxo_market`を確認 |
| public exposure | 禁止 |
| pgAdmin/Adminer | 導入しない |

PostgreSQL 18公式imageは`PGDATA`とvolume配置が17以前から変更されているため、named volumeは`/var/lib/postgresql`へmountする。[PostgreSQL Docker image PGDATA note](https://hub.docker.com/_/postgres)

Flaskアプリとデータ更新プログラムは当面macOS側で実行し、DBだけをDocker化する。Composeのhealthcheckがhealthyになってからmigrationやアプリ接続を開始する。Composeはhealthcheckを条件に依存サービスを待機できる。[Docker Compose startup order](https://docs.docker.com/compose/how-tos/startup-order/)

## 3. データを3データベースへ物理分離する

同じtableに最新データと研究cutoffを置いてviewだけで分ける方式は採用しない。誤った接続やfilter漏れで将来データを読む可能性が残るためである。

同一PostgreSQL cluster内に、次の3 databaseを作る。

### 3.1 `saxo_market`

- 全取得履歴と最新barを保存する更新可能DB
- Saxoからの増分取得はここだけへ書き込む
- Flaskはread-only roleで参照する
- 既存の2024-06-29以後のデータもここには保存できる
- 戦略研究の合否判定には直接接続しない

### 3.2 `saxo_research_v13`

- `2024-06-28T23:59:59Z`以前だけを物理コピーした研究用DB
- 凍結後はdatabase default read-only
- `v13_research_reader`だけが接続可能
- `saxo_market`への`dblink`、foreign data wrapperを導入しない
- 最大時刻、件数、元manifest、dump SHA-256を記録する
- 既存2024–2026データを後から未見Holdoutへ変更しない

### 3.3 `saxo_forward_v13`

- RF0の最終仕様凍結後に取得する新しいforwardデータ用
- writerは`SECURITY DEFINER`のappend procedureだけを実行できる
- 評価ゲートまでは一般readerへSELECT権限を付与しない
- `saxo_market`とcross-database接続しない

DBが同じDocker volumeにあること自体はbackupや災害分離を意味しない。論理権限と接続先を分けるための構成であり、backupは別途必要である。

## 4. Role・権限

| role | Login | 用途 |
|---|---:|---|
| `saxo_db_owner` | No | schema/table所有者 |
| `saxo_migrator` | Yes | versioned migration専用 |
| `saxo_ingest` | Yes | `saxo_market`のingestion DML専用 |
| `saxo_app_reader` | Yes | Flask用read-only |
| `saxo_analyst_reader` | Yes | 最新市場DBの分析view用read-only |
| `v13_research_reader` | Yes | 凍結研究DBだけのread-only |
| `v13_forward_writer` | Yes | forward append procedure実行だけ |
| `postgres` | Yes | bootstrap・緊急作業だけ |

アプリケーションからsuperuserやownerを使用しない。schemaの`PUBLIC`権限をrevokeし、必要なschema/table/functionだけを明示grantする。Flaskのconnectionにはread-only transactionと30秒の`statement_timeout`を設定する。

## 5. Secretと認証情報

Docker Compose secretsを使用し、`.secrets/`以下のファイルからpasswordを渡す。Compose secretsはserviceへ明示的にgrantでき、sourceをfileまたはhost environmentにできる。[Docker Compose secrets](https://docs.docker.com/reference/compose-file/secrets/)

DB1で次を実装する。

- `.secrets/`、`.env`、`backups/postgres/`を`.gitignore`へ追加
- secret file modeを`0600`に設定
- password生成scriptは値を標準出力・logへ表示しない
- `.env.example`には非機密のhost、port、database名だけを置く
- password入り`DATABASE_URL`をcommitしない
- Saxo 24時間token、AccountKey、ClientKey、account identifierをDBへ保存しない
- Saxo tokenはFlask session memoryまたは取得process environmentだけで使用する

DB passwordはローカル永続secretとして管理できるが、Saxo 24時間tokenとは用途と寿命が異なる。両者を同じtableや環境ファイルへ置かない。

## 6. Schema構成

各databaseは必要な範囲で次のschemaを持つ。

| schema | 用途 |
|---|---|
| `catalog` | instrument・source master |
| `ops` | ingestion run、source file、watermark、migration |
| `raw` | providerから受け取ったrevisionのappend-only保存 |
| `staging` | transaction内の検査用 |
| `curated` | 品質合格済みの最新1Hとreference daily |
| `derived` | 合格1Hから決定論的に生成する4H・1D |
| `quality` | 品質eventとgate結果 |
| `analytics` | Flask・研究用read-only view |

`raw`では異常データも失わず保存する。`curated`には品質gateを通過したデータだけを入れる。これにより、FX raw 4HのBid/Ask交差を監査用に保持しながら、戦略入力から除外できる。

## 7. 主要table仕様

### 7.1 `catalog.instrument`

銘柄をsymbol文字列だけで識別しない。provider、environment、UIC、AssetTypeをmaster化する。

主な列:

- `instrument_id BIGINT GENERATED ... PRIMARY KEY`
- `provider TEXT`
- `environment TEXT`
- `market_key TEXT`
- `symbol TEXT`
- `uic BIGINT`
- `asset_type TEXT`
- `category TEXT`
- `currency CHAR(3)`
- `exchange_id TEXT`
- `active_from_utc TIMESTAMPTZ`
- `active_to_utc TIMESTAMPTZ NULL`

unique keyは`(provider, environment, uic, asset_type)`とする。

### 7.2 `raw.market_bar_revision`

各取得runで受信したbarをappend-onlyで保存する。過去barがprovider側で修正されても旧値を消さない。

主キー:

```text
(ingestion_run_id, instrument_id, horizon_minutes, time_utc, price_basis)
```

OHLC、Bid/Ask OHLC、Volume、MarketTradingState、DataVersion、DelayedByMinutes、is_complete、retrieved_at、payload SHA-256を保持する。raw tableではBid > Ask等をCHECK constraintで拒否せず、`quality.event`へ記録する。

### 7.3 `curated.market_bar`

分析可能な最新barを1つだけ保持する。

主キー:

```text
(instrument_id, horizon_minutes, time_utc, price_basis)
```

初期運用で許可するhorizonは60分だけである。価格は`NUMERIC(24,12)`、時刻は`TIMESTAMPTZ`、volumeは`NUMERIC(30,8)`とする。

同じkeyが再取得された場合、incoming `retrieved_at_utc`が新しいときだけupsertする。PostgreSQLの`INSERT ... ON CONFLICT DO UPDATE`を使用し、更新前後はraw revisionに残す。[PostgreSQL INSERT](https://www.postgresql.org/docs/18/sql-insert.html)

### 7.4 `curated.etf_total_return_daily`

ETFの日次total return系列をSaxo raw OHLCと混ぜない。source dataset、adjusted close、total return index、dividend、split factorを明示する。

主キー:

```text
(source_dataset_id, ticker, date)
```

1Hから生成するraw 1D risk barと、分配調整済みtotal returnは別table・別price basisとする。

### 7.5 `derived.market_bar_4h`

- sourceは`curated.market_bar`の完成済み1Hだけ
- raw Saxo 4Hを入力にしない
- FX・ETFとも同じ時間境界関数を使用
- incomplete 4Hは研究viewへ出さない
- `derivation_version`と入力run範囲を保存

### 7.6 `derived.market_bar_1d_risk`

完成済み1Hからsession calendarに従って生成する。用途は前日までのregime、volatility、tail、risk capだけであり、方向entry/exitへ使用しない。

### 7.7 運用・品質table

- `ops.ingestion_run`: 開始・終了・trigger・成功/失敗・件数・error code
- `ops.source_file`: 相対path・SHA-256・size・row count
- `ops.watermark`: 銘柄・horizonごとの最新取得時刻・最新完成時刻
- `ops.schema_migration`: migration番号・checksum・適用時刻
- `quality.event`: rule ID・severity・対象bar・action
- `ops.research_snapshot`: cutoff・件数・manifest・dump hash

## 8. 初期移行対象

DB2では次を入力原本として登録する。原本CSV/JSONは移行後も削除しない。

| データ | 行数 | 取扱い |
|---|---:|---|
| intraday 1H | 394,992 | rawとcurated latestへ全件。未完成13件を除く394,979件だけが分析対象 |
| intraday raw 4H | 130,389 | raw archiveだけ |
| ETF adjusted daily | 54,285 | total-return専用table |
| EURUSD legacy daily | 14,474 | reference archive |
| USDJPY legacy daily | 8,597 | reference archive |

intraday全体は26 series・525,381行である。1Hは13/13 seriesが品質PASS。raw 4HはETF 11 seriesがPASS、EURUSDとUSDJPYはBid/Ask交差によりFAILしている。4HのFAIL行を削除・修正せずrawへ保持し、`derived.market_bar_4h`には使用しない。

DB2の照合条件:

- source manifest SHA-256一致
- source file単位の行数・SHA-256一致
- raw DB件数とCSV件数一致
- curated 1Hの主キー重複ゼロ
- max/min timestamp一致
- 13銘柄・4カテゴリー一致
- access token・account identifier検出ゼロ
- `saxo_research_v13`の最大時刻がcutoff以下

大量投入はtransaction内でPostgreSQL `COPY`を使用する。[PostgreSQL COPY](https://www.postgresql.org/docs/18/sql-copy.html)

## 9. 増分更新アルゴリズム

DB3ではSaxo Chart APIからcanonical 1Hだけを更新する。raw 4Hの増分取得は停止し、4Hと1Dは1Hから生成する。

更新手順:

1. `pg_advisory_lock`で単一ingest writerを確保
2. `ops.ingestion_run`をRUNNINGで作成
3. 銘柄・price basisごとに`latest_complete_time_utc`を取得
4. ETFは20 bars、FXは72 bars戻して重複取得
5. API payloadを既存方針でファイルへatomic保存しSHA-256登録
6. `staging`へCOPY
7. OHLC、Bid/Ask、重複、時刻、complete flagを検査
8. 受信行を`raw.market_bar_revision`へappend
9. 品質合格1Hを`curated.market_bar`へupsert
10. historical revisionを`quality.event`へ記録
11. 影響期間の4H・1Dを再生成
12. watermarkと件数を更新してcommit
13. 失敗時はcurated・derived・watermarkをrollbackしrunをFAILEDで記録

分析viewは`is_complete = true AND quality_status = 'PASS'`だけを返す。最新未完成barはDBへ保存できるが、研究入力には出さない。

HTTP 429は既存collectorと同じ有限回の指数backoffを使う。注文API、precheck、portfolio endpointは呼び出さない。

## 10. Flask・分析インターフェース

Flask request threadから直接DB更新しない。「最新データ取得」操作は単一ingest processへjobを登録し、画面はrun IDと状態だけをpollする。

必要なread API:

- 銘柄・時間足・期間を指定したbar取得
- 最新完成時刻・遅延時間
- ingestion run履歴
- 品質event一覧
- dataset/snapshot manifest
- 1H・derived 4H・derived 1Dの件数比較

Flaskは`psycopg_pool`を最大5 connectionで使用し、parameterized SQLだけを許可する。大規模bar queryはstart/end指定を必須とする。DB管理UIは作らない。

## 11. Backup・restore

Docker named volumeはbackupではない。次の時点で`pg_dump -Fc`を作成する。

- 初期移行完了後
- schema migrationの前後
- 成功した増分更新後、1日最大1回

保存先は`backups/postgres/`、保持はdaily 7世代・weekly 4世代とする。各dumpにSHA-256 manifestを付け、`pg_restore --list`を実行する。月1回、一時databaseへrestoreして件数・主キー・snapshot cutoffをsmoke testする。

raw CSV/JSONも監査原本として残るが、DB privilege、migration状態、watermark、revision履歴を復元するにはpg_dumpが必要である。

## 12. Migration方針

`db/migrations/`に番号付きSQLを置き、Psycopg transaction runnerで適用する。

予定:

```text
0001_bootstrap.sql
0002_roles_and_grants.sql
0003_market_schema.sql
0004_research_snapshot_schema.sql
0005_forward_append_procedure.sql
```

`ops.schema_migration`へfilename、SHA-256、適用時刻を記録する。適用済みSQLのchecksumが変わった場合はFAILする。実データに対する破壊的down migrationは実行せず、backupからrestoreする。

## 13. Phase別実装ゲート

### DB0 — 仕様凍結（本Phase）

- 本仕様書と機械可読JSONを作成
- Docker/PostgreSQL/schema/role/増分更新/backupを固定
- 実装・DB作成・データ変更はゼロ
- 次はDB1だけをunlock

### DB1 — Docker/PostgreSQL基盤

- `compose.yaml`、secret生成、healthcheck
- PostgreSQL 18.4 image digest記録
- 3 databaseとrole/grant作成
- migration runnerと空schema作成
- localhost以外から接続不能
- restart後もnamed volumeのschemaが保持される

### DB2 — 既存データ移行・研究snapshot

- 525,381 intraday行のraw照合
- 1H curatedと4H raw archiveの役割分離
- daily reference移行
- `saxo_research_v13`をcutoff以前だけで物理作成・read-only化
- dump・manifest・SHA-256作成

### DB3 — 最新データ増分更新

- overlap取得・idempotent upsert
- historical revision保存
- quality failure時rollback
- 1Hから4H・1D再生成
- 中断再開・429 retry・単一writer lock
- token永続化ゼロ、注文APIゼロ

### DB4 — 参照・運用ゲート

- Flask read-only query
- 更新job状態表示
- backup/restore smoke test
- 研究コードが`v13_research_reader`以外で実行できないことを検査
- 全DBテスト・既存全体回帰PASS

DB4 PASS後だけRT0を再度unlockする。

## 14. DB0成果物と禁止事項

成果物:

- `docs/v13_phase_db0_database_implementation_spec.md`
- `config/v13_phase_db0_database_spec.json`
- `validate_v13_phase_db0.py`
- `tests/test_v13_phase_db0.py`
- `data/v13/db0/phase_db0_20260716_v1/manifest.json`

DB0では次を実行しない。

- Docker image pull、container/volume/database作成
- dependency install
- CSV/JSONの移動・削除・書換え
- Saxo API呼出し
- credential保存
- strategy signal、PnL、WFO、Holdout、portfolio calculation

以上をPhase DB0の凍結仕様とし、次に許可する作業はPhase DB1だけとする。
