# Phase DB0 データベース実装仕様書

作成日: 2026-07-16
対象研究線: `v13categoryintraday`
仕様ID: `v13_database_prerequisite_20260716_v2`
状態: **SPEC RE-FROZEN / DB1-DB3 PASS / DB4 NEXT**

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

### 1.1 v2再凍結の目的

v1には、格納データを追跡するtableと将来のread API方針はあったが、運用者が「何のデータが、どの期間、何件、どの品質で格納されているか」を一貫した方法で確認するためのdataset台帳、read-only view、CLI、品質eventの解決状態、backup実績台帳、runbookが不足していた。

v2では次を追加し、DB1–DB4の責務を再凍結する。

- `catalog.source_dataset`によるdataset正本
- `ops.backup_run`によるbackup・restore実績
- `quality.event`の未解決・確認済み・解決済み状態
- inventory、coverage、freshness、lineage、run、quality、storage、backupのread-only view
- `python3 -m market_db.inspect`による運用CLI
- `saxo_ops_operator`と`python3 -m market_db.operate`によるprocedure-only状態更新
- `docs/database_operations_runbook.md`による起動、確認、障害対応、backup/restore手順

この再凍結はデータ管理・可観測性を補うものであり、既存CSV、研究cutoff、価格basis、canonical 1H、戦略候補、研究ゲートを変更しない。

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
| `saxo_ops_operator` | Yes | 許可済みquality/backup procedureの実行専用 |
| `v13_research_reader` | Yes | 凍結研究DBだけのread-only |
| `v13_forward_writer` | Yes | forward append procedure実行だけ |
| `postgres` | Yes | bootstrap・緊急作業だけ |

アプリケーションからsuperuserやownerを使用しない。schemaの`PUBLIC`権限をrevokeし、必要なschema/table/functionだけを明示grantする。Flaskのconnectionにはread-only transactionと30秒の`statement_timeout`を設定する。`saxo_ops_operator`にはtableの直接INSERT/UPDATE/DELETEを与えず、監査対象のSECURITY DEFINER procedureだけをEXECUTEさせる。

## 5. Secretと認証情報

Docker Compose secretsを使用し、`.secrets/`以下のファイルからpasswordを渡す。Compose secretsはserviceへ明示的にgrantでき、sourceをfileまたはhost environmentにできる。[Docker Compose secrets](https://docs.docker.com/reference/compose-file/secrets/)

DB1で次を実装する。

- `.secrets/`、`.env`、`backups/postgres/`を`.gitignore`へ追加
- secret file modeを`0600`に設定
- password生成scriptは値を標準出力・logへ表示しない
- `.env.example`には非機密のhost、port、database名だけを置く
- password入り`DATABASE_URL`をcommitしない
- Saxo 24時間token、AccountKey、ClientKey、account identifierをDBへ保存しない
- Saxo tokenはlocal operator request/job memoryまたは取得process environmentだけで使用する

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

### 7.0 `catalog.source_dataset`

import、API取得、外部total-return、分析baselineをdataset単位で識別する正本とする。`ops.source_file.source_dataset_id`と`curated.etf_total_return_daily.source_dataset_id`はこのtableを参照する。

主な列:

- `source_dataset_id TEXT PRIMARY KEY`
- `dataset_name TEXT`
- `provider TEXT`
- `environment TEXT`
- `dataset_kind TEXT`
- `price_basis TEXT`
- `canonical_horizon_minutes SMALLINT NULL`
- `expected_update_interval_seconds BIGINT NULL`
- `freshness_grace_seconds BIGINT NULL`
- `authoritative_layer TEXT`
- `research_eligibility TEXT`
- `active BOOLEAN`
- `source_manifest_relative_path TEXT NULL`
- `source_manifest_sha256 CHAR(64) NULL`
- `created_at_utc TIMESTAMPTZ`
- `metadata_json JSONB`

`dataset_kind`は少なくとも`raw_market`、`external_reference`、`total_return`、`analysis_baseline`を区別する。manifest pathはrepository相対pathだけを許可し、host固有の絶対pathを保存しない。

`expected_update_interval_seconds`と`freshness_grace_seconds`が未登録のdatasetは、freshnessを`NOT_EVALUATED`とする。閾値を暗黙の既定値でPASSにしない。

### 7.0.1 `catalog.session_calendar` / `catalog.session_interval`

coverageと完成足判定の正本として、calendar定義と日別session intervalを分離して保持する。

`catalog.session_calendar`の主な列:

- `session_calendar_id TEXT PRIMARY KEY`
- `provider TEXT`
- `exchange_id TEXT NULL`
- `asset_type TEXT`
- `timezone_name TEXT`
- `schedule_version TEXT`
- `effective_from DATE`
- `effective_to DATE NULL`
- `metadata_json JSONB`

`catalog.session_interval`の主な列:

- `session_calendar_id TEXT`
- `session_date DATE`
- `interval_sequence SMALLINT`
- `open_time_utc TIMESTAMPTZ NULL`
- `close_time_utc TIMESTAMPTZ NULL`
- `session_status TEXT`
- `source_sha256 CHAR(64) NULL`

主キーは`(session_calendar_id, session_date, interval_sequence)`とする。`HOLIDAY`はopen/closeをNULL、`OPEN`と`SHORT_SESSION`はopen/closeを必須かつclose > openとする。holiday、短縮取引、DSTを明示し、単純な平日24時間として補完しない。

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
- `session_calendar_id TEXT NULL`
- `active_from_utc TIMESTAMPTZ`
- `active_to_utc TIMESTAMPTZ NULL`

unique keyは`(provider, environment, uic, asset_type)`とする。

### 7.2 `raw.market_bar_revision`

各取得runで受信したbarをappend-onlyで保存する。過去barがprovider側で修正されても旧値を消さない。

主キー:

```text
(ingestion_run_id, instrument_id, horizon_minutes, time_utc, price_basis)
```

`source_file_id`、OHLC、Bid/Ask OHLC、Volume、MarketTradingState、DataVersion、DelayedByMinutes、is_complete、retrieved_at、payload SHA-256を保持する。CSV importとAPI raw artifactは先に`ops.source_file`へ登録し、raw rowから原本fileへ追跡できるようにする。raw tableではBid > Ask等をCHECK constraintで拒否せず、`quality.event`へ記録する。

### 7.2.1 `raw.reference_observation`

価格barへ変換しないcollection summary、instrument master、ETF外部原本、macro、RA0 baselineをsource row単位で損失なく保持する。主キーは`(source_file_id, row_number)`とし、`reference_kind`、`reference_key`、`layer`、`observation_time_utc`、元CSV rowの`payload_json`、canonical JSONのSHA-256を保存する。`layer`は監査原本の`raw`と、再現比較用の`research_metadata`を区別する。

### 7.3 `curated.market_bar`

分析可能な最新barを1つだけ保持する。

主キー:

```text
(instrument_id, horizon_minutes, time_utc, price_basis)
```

初期運用で許可するhorizonは60分だけである。価格は`NUMERIC(24,12)`、時刻は`TIMESTAMPTZ`、volumeは`NUMERIC(30,8)`とする。

同じkeyが再取得された場合、incoming `retrieved_at_utc`が新しいときだけupsertする。PostgreSQLの`INSERT ... ON CONFLICT DO UPDATE`を使用し、更新前後はraw revisionに残す。[PostgreSQL INSERT](https://www.postgresql.org/docs/18/sql-insert.html)

### 7.4 `curated.etf_total_return_daily`

ETFの日次total return系列をSaxo raw OHLCと混ぜない。source dataset、source file、adjusted close、total return index、dividend、split factorを明示する。

主キー:

```text
(source_dataset_id, ticker, date)
```

1Hから生成するraw 1D risk barと、分配調整済みtotal returnは別table・別price basisとする。`source_file_id`は`ops.source_file`を参照し、各curated rowから入力CSVへ追跡できなければならない。

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
- `quality.event`: rule ID・severity・対象bar・action・status・解決記録
- `ops.research_snapshot`: cutoff・件数・manifest・dump hash
- `ops.backup_run`: 対象DB・dump相対path・SHA-256・size・検証・restore smoke test結果

`quality.event.status`は`OPEN`、`ACKNOWLEDGED`、`RESOLVED`を許可し、`resolved_at_utc`、`resolved_by`、`resolution_note`を保持する。重大品質違反の元のobserved valueやactionは解決時にも上書きしない。

`ops.backup_run`は少なくとも次を保持する。

- `backup_run_id BIGINT GENERATED ... PRIMARY KEY`
- `database_name TEXT`
- `started_at_utc TIMESTAMPTZ`
- `finished_at_utc TIMESTAMPTZ NULL`
- `status TEXT`
- `relative_path TEXT`
- `sha256 CHAR(64) NULL`
- `size_bytes BIGINT NULL`
- `pg_restore_list_pass BOOLEAN NULL`
- `restore_smoke_tested_at_utc TIMESTAMPTZ NULL`
- `restore_smoke_test_status TEXT NULL`
- `error_code TEXT NULL`

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

HTTP 429とtimeout・接続切断等の一時的network例外は、GETだけを1/2/4秒待機・最大4attemptで有限retryする。4回失敗時はそれぞれ`BLOCKED_RATE_LIMIT`または`FAILED_NETWORK`で停止する。注文API、precheck、portfolio endpointは呼び出さない。

DB3実装では上記をcanonical 13全体の単一transactionとして固定した。取得済みraw JSONはtransaction外で先にatomic保存し、DB失敗時にも監査原本として残すが、token、AccountKey、ClientKey、TradableOn、Authorizationは保存しない。通常runはEtf 20本・FxSpot 72本の実バーoverlapを使い、Saxoの境界包含を前提に重複排除する。

`DataVersion`がwatermarkと異なる場合は対象instrumentを`STALE_DATA_VERSION`へ移し、通常run全体をrollbackする。復旧は対象1銘柄の`manual_db3_full_refetch`だけに限定する。`Mode=UpTo`で既存最古時刻以前まで取得できたことを確認し、`STALE_DATA_VERSION`・RUNNING・専用triggerを検査するsecurity-definer procedureだけがcurated置換を許可する。old raw revisionと削除observationsのquality auditは保持する。

専用full-refetchに限り、過去FxSpotの`High`または`Low`極値にだけ`Bid > Ask`があるunique rowを、最大10件かつ全unique観測rowの0.01%以下で隔離できる。最新形成中sample、Open/Close交差、欠損、OHLC違反、受理rowとのtimestamp競合、重複page間の値矛盾は対象外とし、全runをFAILする。隔離時もswap・interpolate・clamp・上書きをせず、raw JSON、SHA-256、相対path、時刻、元Bid/Askを保持する。該当rowは`raw.market_bar_revision`、curated、derivedへ入れず、`ops.ingestion_run.rejected_rows`と`db3_fx_crossed_extrema_quarantine`の解決済みWARNへ監査記録する。いずれかの上限・scopeを外れた場合、DB barとwatermarkを変更しない。

## 10. データ管理・参照インターフェース

### 10.1 運用view

次のread-only viewを作成する。viewは明示列だけを公開し、password、token、Authorization header、account identifier、host固有絶対pathを含めない。

| view | 用途 |
|---|---|
| `analytics.v_data_inventory` | dataset・銘柄・layer・price basis・horizon別の件数、期間、最新完成時刻、品質 |
| `analytics.v_data_coverage` | 期待期間に対する実件数、完成・未完成、重複、欠損の集計 |
| `analytics.v_data_freshness` | watermark、次のsession slot、data status、鮮度判定 |
| `analytics.v_data_lineage` | source dataset/file、ingestion run、raw、curated/derivedの追跡 |
| `ops.v_ingestion_status` | run状態、件数、error code、watermark、遅延 |
| `quality.v_open_event` | OPEN/ACKNOWLEDGEDの品質event |
| `ops.v_storage_usage` | database/schema/table別のrelation sizeとpartition再検討閾値 |
| `ops.v_backup_status` | database別の最終成功backup、検証、restore smoke test、経過時間 |

`analytics.v_data_inventory`は最低限、`source_dataset_id`、`instrument_id`、symbol、category、layer、price_basis、horizon、row_count、min/max time、latest complete time、quality status、latest ingestion run、freshnessを返す。

coverageの「欠損」は単純な連続時刻差だけで判定せず、instrumentの取引session/calendarと完成足規則に基づく。calendarが未実装のDB2時点では`NOT_EVALUATED`を返し、0件やPASSへ偽装しない。DB3では期待slotに一致した行、missing、out-of-sessionを分ける。既存履歴にcalendar外バーがあっても削除せず`WARN`として表面化し、派生足からだけ除外する。FX calendarはSaxo live schedule照合まで`PROVISIONAL`とし、coverage/freshnessを`NOT_EVALUATED`に保つ。

`saxo_market`には7 viewすべてを作る。`saxo_research_v13`にはinventory、coverage、lineage、storageだけを作り、`v13_research_reader`で参照する。`saxo_forward_v13`は評価ゲート前にreaderを与えないため運用CLIの参照対象外とし、cross-database linkや集約用の例外権限を作らない。

### 10.2 運用CLI

管理GUIを作らず、次のread-only CLIを標準運用入口とする。

```bash
python3 -m market_db.inspect inventory
python3 -m market_db.inspect coverage
python3 -m market_db.inspect freshness
python3 -m market_db.inspect runs
python3 -m market_db.inspect quality --fail-on-alert
python3 -m market_db.inspect lineage
python3 -m market_db.inspect storage
python3 -m market_db.inspect backups
```

- default出力は人間向けtable、`--format json`で機械可読出力を提供する。
- 時刻はUTC、pathはrepository相対pathで表示する。
- queryは固定済みparameterized SQLだけを使い、任意SQL入力を許可しない。
- marketのinventory/freshness/runs/quality/backupsは`saxo_app_reader`、marketのinventory/coverage/lineage/storageは`saxo_analyst_reader`、researchのinventory/coverage/lineage/storageは`v13_research_reader`を使用する。
- `--database`は`saxo_market`と`saxo_research_v13`のallow-listに限定し、subcommandとroleの許可matrixに一致しなければ接続前に拒否する。forwardは評価ゲートまで指定不可とする。
- superuser、owner、migrator、writerへfallbackしない。
- password入りURL、secret値、Saxo token、account情報を出力しない。
- 接続・設定・SQL実行失敗はexit 1、`--fail-on-alert`指定時のfreshness/quality閾値違反はexit 2、それ以外の正常照会はexit 0とする。
- DB1では空DBに対して安全に0件または`NOT_EVALUATED`を返し、DB2以降で実データの期待値をgate化する。

### 10.3 制御された運用更新

状態変更を伴う操作をread-only inspectへ混ぜない。`market_db.operate`は`saxo_ops_operator`を使い、allow-list済みprocedureだけを実行する。

```bash
python3 -m market_db.operate quality acknowledge --event-id <id> --operator-label <label>
python3 -m market_db.operate quality resolve --event-id <id> --operator-label <label>
```

resolution noteは対話入力またはstdinから受け取り、shell history、process argument、logへ残さない。operator labelは監査用の非機密labelであり、Saxo AccountKeyや口座識別子を使用しない。

許可するprocedureは次に限定する。

- `quality.acknowledge_event`
- `quality.resolve_event`
- `ops.start_backup_run`
- `ops.finish_backup_run`

procedureは状態遷移、対象存在、必須note、時刻を検査し、元のobserved valueとactionを変更しない。SECURITY DEFINER procedureは`saxo_db_owner`所有、`search_path=pg_catalog`固定、全object名をschema修飾し、PUBLICのEXECUTEをrevokeして`saxo_ops_operator`だけへgrantする。`saxo_ops_operator`の直接DML、任意function実行、raw/curated変更、forward接続は拒否する。DB1ではtransaction rollbackを使った権限・状態遷移testだけを行い、永続的なquality/backup実績は作らない。

### 10.4 Flask・分析インターフェース

DB3 live gateでは、一般Flask read APIに先行せず、固定`market_db.incremental_update reconcile`だけを起動するlocalhost operator UIを許可する。これはDB管理UIではなくsession-only token bridgeである。ユーザーがpassword欄へtokenを入力し、AIは値を読まずにjob開始とsanitized statusを操作する。tokenをURL、argv、file、DB、log、cookie、browser storageへ保存せず、子process環境だけへ渡す。loopback bind、exact Origin/Host、CSRF、no-store、CSP、single job、`shell=False`を強制する。

Flask request threadから直接DB更新しない。「最新データ取得」操作は単一ingest processへjobを登録し、画面はrun IDと状態だけをpollする。

必要なread API:

- 銘柄・時間足・期間を指定したbar取得
- 最新完成時刻・遅延時間
- ingestion run履歴
- 品質event一覧
- dataset/snapshot manifest
- 1H・derived 4H・derived 1Dの件数比較
- inventory、coverage、freshness、lineage、storage、backup status

Flaskは`psycopg_pool`を最大5 connectionで使用し、parameterized SQLだけを許可する。大規模bar queryはstart/end指定を必須とする。DB管理UIは作らない。

### 10.5 運用runbook

`docs/database_operations_runbook.md`を正本とし、少なくとも次を記載する。

- 安全な起動、停止、通常restart、health確認
- inventory、coverage、freshness、failed run、open quality eventの確認
- import/増分更新の開始前確認と完了後照合
- migrationの適用前確認、checksum不一致時の停止手順
- backup作成、SHA-256、`pg_restore --list`、restore smoke test
- secret rotationと漏えい疑い時の対応
- port競合、Docker停止、disk逼迫、rate limit、token失効、品質gate失敗への対応
- 禁止操作と、破壊操作にユーザー承認が必要なこと

## 11. Backup・restore

Docker named volumeはbackupではない。次の時点で`pg_dump -Fc`を作成する。

- 初期移行完了後
- schema migrationの前後
- 成功した増分更新後、1日最大1回

保存先は`backups/postgres/`、保持はdaily 7世代・weekly 4世代とする。各dumpにSHA-256 manifestを付け、`pg_restore --list`を実行する。月1回、一時databaseへrestoreして件数・主キー・snapshot cutoffをsmoke testする。

backup開始時に`ops.backup_run`をRUNNINGで作成し、dump、SHA-256、`pg_restore --list`の成功後にPASSへ更新する。失敗時はsanitized error codeを残し、未検証dumpを成功扱いしない。`ops.v_backup_status`と`market_db.inspect backups`はこの台帳を参照する。

raw CSV/JSONも監査原本として残るが、DB privilege、migration状態、watermark、revision履歴を復元するにはpg_dumpが必要である。

## 12. Migration方針

`db/migrations/`に番号付きSQLを置き、Psycopg transaction runnerで適用する。

予定:

```text
0001_bootstrap.sql
0002_market_schema.sql
0003_research_schema.sql
0004_forward_schema.sql
0005_grants_and_forward_append.sql
0006_operational_views.sql
0007_operational_procedures.sql
0008_quality_privilege_hardening.sql
0009_db2_import_support.sql
0010_db3_incremental_support.sql
0011_db3_coverage_refinement.sql
0012_db3_full_refetch_guard.sql
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
- migration runner、空schema、source dataset/session calendar/backup/quality table、運用view作成
- 空DBで`market_db.inspect`の全subcommandが安全に動作
- ops operatorとquality/backup procedureの直接DML禁止・状態遷移test
- infrastructure運用runbook作成
- localhost以外から接続不能
- restart後もnamed volumeのschemaが保持される

### DB2 — 既存データ移行・研究snapshot（PASS）

- 525,381 intraday行のraw照合
- 1H curatedと4H raw archiveの役割分離
- daily reference移行
- `catalog.source_dataset`を登録し、source fileとの参照整合性を確認
- inventory、coverage、lineageを実データ件数・期間・checksumと照合
- `saxo_research_v13`をcutoff以前だけで物理作成・read-only化
- dump・manifest・SHA-256作成

実装では全69 CSV rowを損失なく追跡するため、`raw.reference_observation`とtotal-returnの`source_file_id`を`0009`で追加した。market DBの実測値はraw market bar 636,629行、reference 90,894行、curated 1H 394,992行、ETF total-return 54,285行で、source file単位lineage不一致は0件である。research DBはcutoff以前だけを物理copyしてdefault read-only化し、content/dump manifest、dump SHA-256、`pg_restore --list`を検証済みである。restore smoke testはDB4までLOCKEDとする。

### DB3 — 最新データ増分更新（PASS）

- overlap取得・idempotent upsert
- historical revision保存
- quality failure時rollback
- 1Hから4H・1D再生成
- session calendar、holiday、短縮取引、DSTを登録し、coverageと完成足判定を有効化
- freshness、watermark、failed run、open quality eventを運用CLIで確認
- 中断再開・429 retry・単一writer lock
- token永続化ゼロ、注文APIゼロ

実装・実DB検証では、canonical 13 watermark、ETF 11 verified calendar、FX 2 provisional calendarを登録し、完成・PASS 1Hから4H 128,469行、1D 47,784行を生成した。coverageはETF 11 WARN・FX 2 NOT_EVALUATED・FAIL 0、staging 0、research DBへの0010〜0012適用0を確認した。live smoke、canonical 13のDataVersion復旧、通常run 104・105連続PASS、総合validator PASSによりDB3を完了し、DB4を次工程として解放した。

### DB4 — 参照・運用ゲート

- Flask read-only query
- 運用CLIとread APIのinventory/coverage/freshness/lineage一致
- 更新job状態表示
- backup/restore smoke test
- backup status、storage usage、retention、runbook drill
- 研究コードが`v13_research_reader`以外で実行できないことを検査
- 全DBテスト・既存全体回帰PASS

DB4 PASS後だけRT0を再度unlockする。

## 14. DB0成果物と禁止事項

v2再凍結成果物:

- `docs/v13_phase_db0_database_implementation_spec.md`
- `specs/v13_phase_db0_database_spec.json`
- `specs/saxo_db_import_spec.json`
- `docs/saxo_db_project_plan.md`
- `docs/db1_implementation_plan.md`
- `manifests/db0_spec_amendment_v2_manifest.json`

DB0では次を実行しない。

- Docker image pull、container/volume/database作成
- dependency install
- CSV/JSONの移動・削除・書換え
- Saxo API呼出し
- credential保存
- strategy signal、PnL、WFO、Holdout、portfolio calculation

以上をPhase DB0の凍結仕様とする。実装進捗はDB1・DB2・DB3 PASSであり、現在次に許可する作業はPhase DB4のread API、backup/restore、retention、runbook運用ゲートだけである。RT0はDB4 PASSまでLOCKEDとする。
