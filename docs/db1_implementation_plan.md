# Phase DB1 実装計画書

作成日: 2026-07-16 JST
対象プロジェクト: `saxo_db`
対象研究線: `v13categoryintraday`
参照仕様ID: `v13_database_prerequisite_20260716_v2`
状態: **IMPLEMENTED / RUNTIME GATE PASS**

## 1. 目的

Phase DB1では、市場データを一切投入せず、Docker Compose上のPostgreSQL、3つの物理database、role・schema・table境界、checksum付きmigration、接続・権限検証を再現可能な形で構築する。

DB1はデータ基盤の実装・実行ゲートであり、戦略の収益性を判定するPhaseではない。成果物を作成しただけではPASSとせず、実コンテナを使ったhealthcheck、権限分離、migration再実行、restart persistenceまで成功した場合だけPASSとする。

## 2. 正本と優先順位

実装時は次の順に解釈する。

1. `README.md`の安全境界、現在ゲート、DB1合格条件
2. `specs/saxo_db_import_spec.json`の新プロジェクト相対pathと移管原本の扱い
3. `specs/v13_phase_db0_database_spec.json`の機械可読なdatabase・role・schema・table仕様
4. `docs/v13_phase_db0_database_implementation_spec.md`の詳細設計
5. `docs/saxo_db_project_plan.md`の全体工程

相違を実装者の判断で黙って変更しない。DB1に影響する差が見つかった場合は、実装を止め、差分・影響・推奨案を提示して仕様を再凍結する。

## 3. DB1のスコープ

### 3.1 実施すること

- 移管済み69 CSVの相対path、size、SHA-256再検証
- Gitの既存変更と追跡対象の確認
- secret生成・権限・Git除外の実装
- 固定仕様に従う`compose.yaml`の作成と検証
- PostgreSQL 18.4 imageのpull、digest・image ID・architecture記録
- named volume上でのPostgreSQL起動とhealthcheck
- 3 database、ownerとops operatorを含む8つの定義済みrole、8 schema、空table・constraintの作成
- source dataset、session calendar、backup run、quality lifecycleの空table作成
- inventory、coverage、freshness、lineage、run、quality、storage、backupのread-only view作成
- `market_db.inspect` read-only CLIと運用runbookの作成
- `market_db.operate`とprocedure-only quality/backup状態更新の作成
- database接続境界、reader/writer権限、PUBLIC権限剥奪
- checksum付きmigration runnerの実装
- 初回migration、同一内容再実行、checksum不一致拒否の検証
- container restart後のschema・migration履歴保持確認
- DB1 validator、unit test、integration testの実装と実行
- DB1 manifestと結果文書の作成

### 3.2 実施しないこと

- `data/import/`のCSV・JSONの変更、削除、整形
- CSVのstaging、raw、curatedへの本import
- Saxo API接続、token入力、instrument照合、増分取得
- watermarkの業務更新、raw revisionの投入
- 4H・1Dの生成
- research snapshotへの市場データ投入・凍結
- feature、signal、position、cost、PnL、WFO、Holdout、portfolio計算
- Flask read API、backup retention運用、Parquet/DuckDB export
- destructive down migration
- `docker compose down -v`またはvolume削除

DB2、DB3、DB4、RT0はDB1がPASSするまでLOCKEDのままとする。

## 4. 固定アーキテクチャ

### 4.1 Docker/PostgreSQL

| 項目 | DB1実装値 |
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
| healthcheck | `pg_isready -d saxo_market` |
| platform固定 | しない |
| 管理GUI | 作らない |
| Python driver | `psycopg[binary,pool]==3.3.4` |

設定、manifest、logにはhost固有の絶対pathを保存せず、repository相対pathを使用する。Python側のproject rootは実行時に`__file__`から解決する。

### 4.2 物理databaseと接続境界

| database | DB1で作る内容 | 通常接続を許可するrole |
|---|---|---|
| `saxo_market` | 全8 schemaと市場DB用の空table | `saxo_ingest`、`saxo_app_reader`、`saxo_analyst_reader` |
| `saxo_research_v13` | snapshot受入れ用の空schema・table | `v13_research_reader` |
| `saxo_forward_v13` | forward用の空tableとappend procedure | `v13_forward_writer` |

`postgres`はbootstrap/emergency、`saxo_migrator`はmigrationのため3 databaseへ接続できる。`PUBLIC`のdatabase `CONNECT`を剥奪し、上表のroleだけへ明示grantする。FDW、dblink、cross-database linkは作らない。

### 4.3 Role

| role | LOGIN | DB1での権限 |
|---|---:|---|
| `saxo_db_owner` | No | schema・table・procedure owner。通常接続不可 |
| `saxo_migrator` | Yes | versioned DDL専用。業務アプリから使用しない |
| `saxo_ingest` | Yes | `saxo_market`の許可済みDMLのみ。DDL不可 |
| `saxo_app_reader` | Yes | `curated`、限定した`ops` status、運用viewのSELECTのみ |
| `saxo_analyst_reader` | Yes | inventory、coverage、lineageを含む`analytics` viewのSELECTのみ |
| `saxo_ops_operator` | Yes | 許可済みquality/backup procedureのEXECUTEのみ。直接DML不可 |
| `v13_research_reader` | Yes | `saxo_research_v13`のSELECTのみ |
| `v13_forward_writer` | Yes | `saxo_forward_v13`のappend procedure実行のみ。直接DML・SELECT不可 |
| `postgres` | Yes | bootstrap/emergencyのみ |

reader roleは`default_transaction_read_only=on`、有限の`statement_timeout`、有限の`temp_file_limit`をdatabase単位で設定する。`saxo_app_reader`の`statement_timeout`は凍結仕様どおり30秒とし、他readerの具体値もmigration内で明示してテストする。

### 4.4 Schemaと空table

`saxo_market`には`catalog`、`ops`、`raw`、`staging`、`curated`、`derived`、`quality`、`analytics`を作成する。最低限、次の凍結tableを空で作成する。

- `catalog.instrument`
- `catalog.source_dataset`
- `catalog.session_calendar`
- `catalog.session_interval`
- `ops.ingestion_run`
- `ops.source_file`
- `ops.watermark`
- `ops.schema_migration`
- `ops.research_snapshot`
- `ops.backup_run`
- `raw.market_bar_revision`
- `curated.market_bar`
- `curated.etf_total_return_daily`
- `derived.market_bar_4h`
- `derived.market_bar_1d_risk`
- `quality.event`

運用viewは次を空DBの段階で作成する。

- `analytics.v_data_inventory`
- `analytics.v_data_coverage`
- `analytics.v_data_lineage`
- `ops.v_ingestion_status`
- `quality.v_open_event`
- `ops.v_storage_usage`
- `ops.v_backup_status`

`saxo_market`には7 viewすべて、`saxo_research_v13`にはinventory、coverage、lineage、storageを作る。`saxo_forward_v13`は評価ゲート前にreaderを与えないため運用viewを公開しない。

列、型、primary key、unique key、allowed horizon、SHA-256形式等は`specs/v13_phase_db0_database_spec.json`を正本とする。rawの異常値を失わせる業務品質CHECKはDB1で追加せず、構造上必要な型・null・key制約に限定する。

research/forward databaseには、後続Phaseで物理分離を維持できる最小限の対応schema・tableを作る。DB1終了時点では全市場tableの件数を0とする。

## 5. 成果物

### 5.1 固定の最低成果物

```text
compose.yaml
.env.example
.dockerignore
requirements.txt
scripts/create_local_db_secrets.py
db/migrations/0001_bootstrap.sql
db/migrations/0002_market_schema.sql
market_db/__init__.py
market_db/connection.py
market_db/migrate.py
market_db/validate.py
market_db/inspect.py
market_db/operate.py
tests/
manifests/db1_implementation_manifest.json
docs/db1_implementation_result.md
docs/database_operations_runbook.md
```

### 5.2 責務分離のため追加する成果物

```text
db/migrations/0003_research_schema.sql
db/migrations/0004_forward_schema.sql
db/migrations/0005_grants_and_forward_append.sql
db/migrations/0006_operational_views.sql
db/migrations/0007_operational_procedures.sql
db/migrations/0008_quality_privilege_hardening.sql
tests/test_secret_generation.py
tests/test_migration_runner.py
tests/test_db1_static.py
tests/test_db1_integration.py
tests/test_inspect_cli.py
tests/test_operate_cli.py
```

migrationの最終分割は、READMEで必須の`0001_bootstrap.sql`と`0002_market_schema.sql`を維持する。番号変更や適用済みfileの内容変更は行わず、新しい変更は新番号で追加する。

## 6. 実装方針

### 6.1 Secret

`scripts/create_local_db_secrets.py`は次を満たす。

- Python標準`secrets`で十分なentropyを持つpasswordを生成
- `.secrets/`を`0700`、各secret fileを`0600`で作成
- 既存fileを既定で上書きしない
- password値をstdout、stderr、exception、logへ出さない
- `postgres`および各LOGIN roleのpasswordを別fileにする
- Saxo token、AccountKey、ClientKey、口座識別子を扱わない

Composeにはbootstrap用postgres passwordだけをCompose secretとして明示grantする。host側migrationは同じ`.secrets/`のrole別fileを直接読み、password入りURLを組み立てず、Psycopgの接続parameterとして渡す。

### 6.2 Compose

`compose.yaml`にはDB serviceだけを定義する。Flask、ingest worker、pgAdmin等は追加しない。

- portは必ず`127.0.0.1:54329:5432`
- `POSTGRES_DB=saxo_market`
- passwordは`POSTGRES_PASSWORD_FILE`で`/run/secrets/...`から読む
- named volumeを`/var/lib/postgresql`へmount
- healthcheckはpasswordを表示せず`saxo_market`を確認
- `platform`を設定しない
- secretやcredentialをlabel、environmentの平文、command引数へ置かない

`.env.example`にはhost、port、database名等の非機密値だけを記載する。

### 6.3 Bootstrapとmigration

PostgreSQLの`CREATE DATABASE`はtransaction block内で実行できないため、次の二層に分ける。

1. cluster bootstrap: `postgres` databaseへ接続し、advisory lock下でroleと3 databaseを存在確認後に作る。`CREATE DATABASE`だけautocommitで実行する。
2. database-local migration: 各databaseへ接続し、DDLを1 migration 1 transactionで適用する。

`market_db.migrate`は次を保証する。

- filenameの昇順以外で適用しない
- migration fileのSHA-256を適用前に計算
- `ops.schema_migration`へdatabase、番号、filename、checksum、適用時刻を記録
- 未適用migrationはtransaction内でDDLと履歴を同時commit
- 同一番号・同一checksumは安全にskip
- 同一番号・異なるchecksumはDDL実行前にFAIL
- 同時実行をadvisory lockで直列化
- SQL本文、接続error、reprへpasswordを混入させない
- destructive down migrationを提供しない

`0001_bootstrap.sql`のchecksumは、各databaseのmigration table作成後に履歴へ登録する。cluster object作成のautocommit例外と、database-local migrationのtransaction性をmanifestへ明記する。

### 6.4 Connection

`market_db.connection`はroleとdatabaseの許可された組合せを列挙し、host、port、database、user、secret fileを個別parameterで返す。次を禁止する。

- password入り`DATABASE_URL`の保存・表示
- repository外のhost固有絶対pathの埋込み
- applicationによる`postgres`または`saxo_db_owner`利用
- callerが任意のdatabase/role組合せを文字列で注入すること

### 6.5 Operational inspectionとrunbook

`market_db.inspect`は固定済みviewだけをparameterized queryで参照するread-only CLIとする。subcommandは`inventory`、`coverage`、`freshness`、`runs`、`quality`、`lineage`、`storage`、`backups`に固定し、任意SQLやallow-list外のdatabase名を受け付けない。

- defaultはtable表示、`--format json`で安定したkeyのJSONを返す。
- 時刻はUTC、pathはrepository相対pathだけを表示する。
- marketのinventory/freshness/runs/quality/backupsは`saxo_app_reader`、marketのinventory/coverage/lineage/storageは`saxo_analyst_reader`を使用する。
- researchのinventory/coverage/lineage/storageは`v13_research_reader`を使用する。
- `--database`は`saxo_market`と`saxo_research_v13`だけを許可し、forwardは評価ゲートまで拒否する。
- writer/migrator/superuserへfallbackしない。
- DB1では市場データ0件を正常状態として返す。
- session calendarが必要なcoverageは、calendar未登録時に`NOT_EVALUATED`を返す。
- connection/config/query失敗はexit 1、`--fail-on-alert`の閾値違反はexit 2、正常照会はexit 0とする。
- exception、debug、JSONにpassword、token、account情報を含めない。

### 6.6 Controlled operations

`market_db.operate`は`saxo_ops_operator`を使い、`quality.acknowledge_event`、`quality.resolve_event`、`ops.start_backup_run`、`ops.finish_backup_run`だけを実行する。任意SQL、任意function、table直接DML、市場データ変更を許可しない。SECURITY DEFINER procedureは`saxo_db_owner`所有、`search_path=pg_catalog`固定、object名をschema修飾し、PUBLIC EXECUTEをrevokeする。

quality eventのresolution noteは対話入力またはstdinから受け取り、process argumentやlogへ残さない。DB1ではtransaction内fixtureに対する状態遷移を確認してrollbackし、永続的なquality/backup rowを残さない。

### 6.7 Operations runbook

`docs/database_operations_runbook.md`はDB1でinfrastructure部分を作成し、後続Phaseごとに追記する。DB1では起動、停止、通常restart、health、inspect、migration、checksum不一致、secret rotation、port/Docker/disk障害、禁止操作を実行可能なコマンドと期待結果付きで記載する。backup/import/APIに関する未実装手順は、実行可能であるかのように記載せず`LOCKED`と明示する。

## 7. 実装手順

### Step 1: Preflightと証跡保護

1. Git status、branch、HEADを記録する。
2. `manifests/import_file_inventory.csv`自身のSHA-256を確認する。
3. 69 CSVの存在、size、SHA-256を相対pathで再計算する。
4. JSON仕様をparseし、必須ID・status・phaseを確認する。
5. `.gitignore`が`.secrets/`、`.env`、backup、dump、cache、移管CSVを除外することをテストする。

不一致が1件でもあれば実装を開始せず`BLOCKED_SOURCE_INTEGRITY`とする。

### Step 2: Scaffoldと静的設定

1. Python package、migration directory、testsを作成する。
2. dependencyを固定する。
3. `.env.example`と`.dockerignore`を作成する。
4. `market_db.inspect`のcommand/format/exit-code contractを実装する。
5. `market_db.operate`のprocedure allow-listと入力contractを実装する。
6. infrastructure運用runbookの初版を作成する。
7. ComposeとSQLの静的検査を実装する。

### Step 3: Secret生成

1. secret生成scriptを実装・unit testする。
2. 実secretを生成し、directory/file modeを確認する。
3. Git statusとcredential scanで追跡対象外を確認する。

### Step 4: Compose検証と起動

1. `docker compose -p saxo-market-data config`を実行する。
2. 解決後設定でもlocalhost bind、volume mount、secret、image tagを確認する。
3. imageをpullし、repo digest、image ID、architectureを取得する。
4. serviceを起動し、healthyになるまで上限付きでpollする。
5. `SELECT version()`、server timezone、database名を確認する。

Docker daemon停止、image取得不能、port競合はコードのPASSに読み替えず、理由別の`BLOCKED`とする。

### Step 5: Bootstrapとmigration適用

1. 9 role（`postgres`を含む）を確認し、LOGIN/NOLOGINを検証する。
2. 3 databaseを作成してPUBLIC CONNECTを剥奪する。
3. database-local migrationを番号順に適用する。
4. `catalog.source_dataset`、session calendar、`ops.backup_run`、quality lifecycle列を含む空tableを作る。
5. 定義済み7つの運用viewを作る。
6. 定義済み4つのquality/backup procedureを作る。
7. schema/table/view/procedure ownerを`saxo_db_owner`へ統一する。
8. role別の最小権限をgrantする。
9. migration履歴とfile checksumを照合する。

### Step 6: Migration耐性検証

1. 初回適用が成功することを確認する。
2. 同一migrationを再実行し、DDL・履歴が増えないことを確認する。
3. test用一時copyのchecksumだけを変更し、不一致で適用前に拒否されることを確認する。
4. failure injection時にtransaction内DDLと履歴がともにrollbackされることを確認する。

追跡中のmigration file自体はtestのために変更しない。

### Step 7: 権限・分離integration test

許可操作と拒否操作をroleごとに実接続で確認する。DML確認はtransaction内で行い、必ずrollbackする。

| 検査 | 期待結果 |
|---|---|
| `saxo_db_owner` LOGIN | 拒否 |
| readerのDDL/DML | 拒否 |
| readerのwrite transaction | 拒否 |
| `saxo_ingest`の許可済みmarket DML | transaction内で成功 |
| `saxo_ingest`のDDL・他database接続 | 拒否 |
| `saxo_app_reader`の限定SELECT | 成功 |
| `saxo_app_reader`のraw/quality直接SELECT | 拒否 |
| `saxo_analyst_reader`のanalytics SELECT | 成功 |
| `market_db.inspect`のreader接続 | 成功 |
| `market_db.inspect`のwriter/migrator/superuser fallback | 拒否 |
| `market_db.inspect`への任意SQL・allow-list外database入力 | 拒否 |
| `market_db.inspect --database saxo_forward_v13` | 評価ゲート前は拒否 |
| `saxo_ops_operator`の許可済みprocedure | transaction内で成功 |
| `saxo_ops_operator`の直接DML・任意function・市場データ変更 | 拒否 |
| `market_db.operate`の不正状態遷移・note欠落 | 拒否 |
| `v13_research_reader`のresearch SELECT | 成功 |
| `v13_research_reader`のmarket/forward接続 | 拒否 |
| `v13_forward_writer`のappend procedure | transaction内で成功 |
| `v13_forward_writer`の直接INSERT/UPDATE/DELETE/SELECT | 拒否 |
| PUBLICのdatabase/schema作成 | 拒否 |
| FDW/dblink/cross-database object | 0件 |

### Step 8: Restart persistence

1. serviceを通常restartする。
2. healthyへ戻ることを確認する。
3. 3 database、role、schema、table、migration履歴が保持されることを確認する。
4. named volume名とmount先を再確認する。
5. volumeは削除しない。

### Step 9: 最終ゼロデータ・security検査

1. 全市場tableの件数が0であることを確認する。
2. `ops.schema_migration`以外にtest残骸がないことを確認する。
3. 69 CSVのSHA-256を再検証する。
4. inspect全subcommandをtable/JSON形式で実行し、0件または`NOT_EVALUATED`を返すことを確認する。
5. inventoryとstorageにschema/tableは出るが市場データ件数は0であることを確認する。
6. tracked file、Git diff、生成log、CLI出力、manifestをcredential patternでscanする。
7. Saxo API request数、order/precheck数が0であることを記録する。

### Step 10: 結果固定

1. `manifests/db1_implementation_manifest.json`を生成する。
2. manifestのJSON構文とartifact checksumを検証する。
3. `docs/db1_implementation_result.md`へ実行結果を記録する。
4. `docs/database_operations_runbook.md`のDB1手順を実際にdry-runまたは実行し、期待結果と一致させる。
5. `python3 -m pytest`と`python3 -m market_db.validate --phase db1`を最終実行する。
6. Git diffを確認し、DB1外の変更がないことを確認する。

## 8. Test構成

### 8.1 Unit test

- secret entropy、mode、既存file非上書き、値の非表示
- connection target allow-list
- migration番号・順序・SHA-256計算
- 同一checksum skip、checksum不一致拒否
- exception/logのcredential redaction
- manifest schemaと相対path制約
- inspect subcommand allow-list、parameter binding、table/JSON schema、exit code
- operate procedure allow-list、状態遷移、note入力、credential redaction
- 空DBとcalendar未登録時の0件/`NOT_EVALUATED`処理

### 8.2 Static test

- Compose project/service/image/port/volume/healthcheck
- `platform`、admin UI、public bindが存在しない
- `.env.example`にpasswordがない
- `.gitignore`のsecret、backup、dump、CSV除外
- SQLに平文password、Saxo credential、FDW/dblinkがない
- table列・型・keyが機械仕様と一致する
- source dataset/session calendar/backup run/quality lifecycleと7運用viewが機械仕様に一致する
- inspect CLIが任意SQL・任意database・writer roleを受け付けない
- operate CLIとops operatorが直接DML・任意functionを許可しない
- SECURITY DEFINER procedureのowner、固定search_path、schema修飾、PUBLIC EXECUTE revoke
- runbookが禁止操作とPhase lockを明示する

### 8.3 Integration test

- PostgreSQL server versionとUTC
- healthcheckとlocalhost-only bind
- database・role・schema・table存在
- role接続matrixと権限matrix
- migration初回・再実行・checksum不一致
- inspect 8 subcommandのtable/JSON出力とread-only権限
- operateのquality状態遷移、backup run状態遷移、rollback、直接DML拒否
- inventory/coverage/freshness/lineageの空DB semantics
- restart persistence
- 市場データ0件
- 移管CSV未変更

Dockerが必要なtestにはmarkerを付けるが、DB1の最終PASS判定ではskipを許可しない。環境理由で実行できない場合、unit/static gateとruntime gateを分け、総合判定を`BLOCKED`とする。

## 9. Manifest記録項目

`manifests/db1_implementation_manifest.json`には最低限次を記録する。

- schema version、phase、spec ID、実行時刻UTC、総合status
- Git branch、HEAD commit、実行開始・終了時のdirty状態
- inventory file SHA-256、CSV数・size・hash検査結果
- Docker/Compose version、image tag、repo digest、image ID、architecture
- Compose project、service、host bind、volume、health status
- PostgreSQL server versionとtimezone
- 3 database、role属性、schema、tableの検査結果
- source dataset、session calendar、backup run、quality lifecycle、運用viewの検査結果
- migration番号、target database、filename、SHA-256、適用結果
- unit/static/integration testの件数と結果
- inspect全subcommand、出力形式、exit code、read-only roleの結果
- ops operator、operate CLI、4 procedure、直接DML拒否の結果
- runbookの作成・手順検証結果と後続PhaseのLOCKED項目
- restart persistence、権限matrix、zero-data gateの結果
- credential scan件数、Saxo API call数、order/precheck数
- DB1で未実施とした項目
- DB2以降のlock状態
- 生成artifactの相対path、size、SHA-256

password、token、Authorization header、account identifier、hostname、host固有絶対pathは記録しない。

## 10. 判定規則

### PASS

READMEのDB1品質ゲートがすべて実環境で成功し、運用table/view/CLI/runbook、CSV未変更、市場データ0件、credential検出0件、成果物と証跡が揃った場合だけ`PASS`とする。

### FAIL

実装または検証可能な環境で、schema、constraint、権限、migration、security、restart persistence等が仕様を満たさない場合は`FAIL`とする。失敗したtest、再現コマンド、sanitized error、残存状態を記録する。

### BLOCKED

Docker daemon停止、port競合、image取得不能等、実行環境の外部条件により必須runtime gateを実施できない場合は`BLOCKED`とする。コードや静的testが成功しても総合PASSへ繰り上げない。

## 11. 失敗時の扱い

- container停止は可能だが、volumeを削除しない。
- migration途中失敗ではdatabaseを初期化せず、transaction rollbackと履歴を確認する。
- secret、CSV、manifestを削除してやり直さない。
- 同一番号のmigrationを書き換えて修復せず、新番号migrationを追加する。
- passwordや接続文字列をerror報告へ貼らない。
- destructive操作が必要になった場合は、理由、対象、影響、代替案を提示し、ユーザーの明示承認を得る。

## 12. DB1完了時の報告形式

1. 総合判定: `PASS` / `FAIL` / `BLOCKED`
2. 作成・変更file
3. image tag、digest、image ID、architecture
4. service、database、role、schema、table
5. migration番号、target、SHA-256
6. healthcheck、restart persistence、権限、security test
7. source dataset、session calendar、backup run、quality lifecycle、運用viewの検証結果
8. inspect CLI全subcommand、operate CLI/procedure権限、runbookの検証結果
9. 69 CSVが未変更であること
10. 市場データが0件であること
11. DB1で実施していない項目
12. 残課題と次に解放可能なPhase

DB1がPASSした場合に解放できるのはDB2だけである。v2ではDB2をimport・inventory/lineage・research snapshot、DB3を増分更新・派生足・freshness監視、DB4をread API・backup/restore・retention・runbook運用ゲートとして再凍結した。RT0はDB4 PASSまでLOCKEDのままとする。
