# saxo_db — AI引き継ぎ書

このREADMEは、前の会話を参照できない別のAIが、このリポジトリだけを読んで安全にデータベース構築を再開するためのハンドオフです。

## 1. 最初に把握すること

このプロジェクトの目的は、Saxo OpenAPIから取得した市場データを、再現可能・追記可能・監査可能なPostgreSQLデータベースで管理し、後続の市場分析と短期売買戦略研究に利用できる状態にすることです。

現在は**Phase DB3 PASS / DB4 NEXT**です。DB2の移行・研究snapshotを保持したまま、増分取得、revision、calendar、watermark、4H/1D派生、coverage/freshness運用まで実装・実DB検証済みです。Saxo SIMのcanonical 13全銘柄を復旧し、通常run 104・105の連続PASSとDB3総合validator PASSを確認しました。

| 項目 | 現在の状態 |
|---|---|
| プロジェクト分離 | 完了 |
| DB0（仕様凍結） | 完了 |
| DB0 v2（データ管理・運用仕様の再凍結） | 完了 |
| CSV移管 | 完了、69ファイル、全件SHA-256一致 |
| Docker Compose | 完了、`postgres:18.4-bookworm`、localhost限定 |
| PostgreSQLコンテナ | 稼働中、healthcheck・restart persistence PASS |
| DB・role・schema・table | 完了、3 DB、用途別role、checksum付きmigration |
| 運用確認 | `market_db.inspect`、`market_db.operate`、runbook実装済み |
| CSVインポート | 完了、69ファイル・781,808 source row、再実行は全件skip |
| Research snapshot | 完了、cutoff `2024-06-28T23:59:59Z`、default read-only、dump検証PASS |
| Saxo API増分更新 | 実装済み、smoke・13銘柄refetch・通常run連続2回・DB3 validator PASS、local operator UI実装済み |
| Calendar / watermark | canonical 13割当済み、`ACTIVE=13`。ETF verified、FX provisional |
| 4H / 1D派生 | 現在4H 128,469行、1D 47,784行。完成・PASS 1Hだけから生成 |
| 鮮度・coverage | CLI/view実装済み。現状STALE 11、NOT_EVALUATED 2、FAIL 0。calendar外/欠損は分離表示 |
| 研究・バックテスト | 未実施 |

次に許可されている工程は、**Phase DB4のread API・backup/restore・retention・runbook運用ゲート**です。DB3は完了しています。RT0と研究PhaseはDB4 PASSまで開始しません。

AI側でlive gateを運用する場合は、`python3 -m market_db.operator_ui`を起動して`http://127.0.0.1:8765/`を開く。ユーザーはpassword欄へtokenを1回入力するだけで、その後の固定`reconcile` job開始・進捗監視・validatorはAIが行う。tokenはURL、コマンド引数、file、DB、log、cookie、browser storageへ保存せず、jobの子process環境だけへ渡す。

## 2. 絶対に守る境界

1. `data/import/` と `manifests/` の移管済みファイルは、インポート元の証跡です。上書き・整形・削除しません。
2. 24時間Access Token、AccountKey、ClientKey、口座識別子をファイル、DB、ログ、ブラウザ保存領域へ永続化しません。
3. 秘密情報はGit管理外の `.secrets/` に置き、ファイル権限を0600にします。値を標準出力へ表示しません。
4. Saxoの未調整OHLCと、分配金再投資を含むETF total-return系列は別テーブル・別`price_basis`として管理します。混ぜません。
5. 4時間足と日次リスク系列は、受理済み1時間足から決定論的に生成します。Saxo APIから取得済みの生4時間足は検算用アーカイブであり、正本にしません。
6. DB2ではAPI取得・4H/1D派生を行わず、DB3で初めて実施しました。特徴量、シグナル、損益、WFO、Holdout評価は引き続き行いません。
7. `2024-06-28T23:59:59Z`以前の研究スナップショットと、それ以後のforwardデータは物理DBで分離します。FDW・dblink・cross-database linkは禁止です。
8. `docker compose down -v`、volume削除、CSV削除、DB初期化などの破壊操作は、ユーザーの明示承認なしに実行しません。
9. 実装が動いたことと、研究戦略に優位性があることを混同しません。DB1〜DB4はデータ基盤の品質ゲートです。

## 3. 読む順序

別のAIは、作業開始前に次をこの順序で読んでください。

1. `README.md` — 現在地、禁止事項、次工程
2. `specs/saxo_db_import_spec.json` — この新プロジェクトでの正本ディレクトリ、ファイル数、移管ルール
3. `docs/v13_phase_db0_database_implementation_spec.md` — 人間向けの凍結済みDB実装仕様
4. `specs/v13_phase_db0_database_spec.json` — 機械可読なDB・role・schema・table・増分更新仕様
5. `docs/db3_implementation_plan.md` — DB3取得・transaction・calendar・live gate計画
6. `docs/db3_implementation_result.md` — DB3 offline実測、live blocker、未解放範囲
7. `manifests/db3_implementation_manifest.json` — DB3 offline gateの機械可読証跡
8. `docs/database_operations_runbook.md` — DB1〜DB3の実行可能な運用手順と後続lock
9. `docs/db2_implementation_plan.md` — DB2の分類、import、snapshot、gateと実施結果
10. `docs/db2_implementation_result.md` — DB2の実測件数、品質、snapshot、未実施範囲
11. `manifests/db2_implementation_manifest.json` — DB2の機械可読証跡
12. `docs/db1_implementation_plan.md` — DB1の実装順序、運用view/CLI/runbook、テスト、判定規則
13. `manifests/db0_spec_amendment_v2_manifest.json` — 運用仕様を追加したv2再凍結の成果物hash
14. `manifests/import_file_inventory.csv` — 69 CSVの相対パス、行数、サイズ、SHA-256
15. `docs/saxo_api_data_acquisition_handoff.md` — token、instrument確認、full/incremental取得、DataVersion、品質・security gate
16. `docs/saxo_db_project_plan.md` — DB0〜DB4と研究再開条件
17. `docs/v13_category_specific_intraday_strategy_research_plan.md` — DB完成後の研究目的。DB構築中は実行しない

相違がある場合の優先順位は、`README.md`の安全境界 → `specs/saxo_db_import_spec.json`の新プロジェクト相対パス → `specs/v13_phase_db0_database_spec.json`の技術仕様 → 人間向け文書です。仕様を変更する必要が出た場合は、黙って解釈変更せず、差分と理由を提示して再凍結してください。

## 4. 移管済みデータ

| 区分 | 保存先 | CSV数 | データ行数 | 用途 |
|---|---:|---:|---:|---|
| 正規化済みintraday | `data/import/intraday/normalized/` | 26 | 525,381 | 1H、取得済み4H、1Dの主要入力 |
| 取得サマリー | `data/import/intraday/collection_summary.csv` | 1 | 26 | 取得結果メタデータ |
| Saxo複数資産日足 | `data/import/daily/saxo_multi_asset/` | 8 | 52,692 | 旧日足資産系列 |
| Saxo ETF raw日足 | `data/import/daily/saxo_etf_raw/` | 14 | 58,592 | Saxo由来の未調整日足 |
| ETF11ソース | `data/import/daily/etf11_sources/` | 14 | 90,727 | 既存ETFソース入力 |
| ETF total-return | `data/import/daily/curated_etf_total_return/` | 1 | 54,285 | 調整済み日次系列。Saxo rawと分離 |
| 分析ベースライン | `data/import/analysis_baseline/` | 5 | 105 | 既存の分析サマリー・比較用 |
| **合計** | `data/import/` | **69** | **781,808** | メタデータ・派生系列を含む |

CSV合計サイズは160,403,659 bytesです。全69ファイルはコピー元とSHA-256が一致しており、credential/token文字列の検査結果は0件でした。以後の完全性確認は、コピー元の絶対パスではなく`manifests/import_file_inventory.csv`を使用してください。

## 5. 凍結済みターゲット構成

### Docker/PostgreSQL

- Composeファイル: `compose.yaml`
- Compose project name: `saxo-market-data`
- service名: `postgres`
- image: `postgres:18.4-bookworm`
- host bind: `127.0.0.1:54329`
- container port: `5432`
- timezone: UTC
- restart policy: `unless-stopped`
- healthcheck: `pg_isready`、対象DB `saxo_market`
- named volume: `saxo_pg18_data`
- mount先: `/var/lib/postgresql`
- 外部公開、管理GUI、`platform`固定: なし
- Python driver: `psycopg[binary,pool]==3.3.4`
- TimescaleDB・初期partitioning: 使用しない

partitioningの再検討条件は、curated barが1,000万行以上、DBが8GB以上、または主要クエリのp95が2秒超のいずれかです。

### 物理データベース

| DB | 目的 | 書込 | 接続境界 |
|---|---|---|---|
| `saxo_market` | 更新可能な市場データ正本 | `saxo_ingest` | app/analyst readerを用途分離 |
| `saxo_research_v13` | cutoff以前の不変研究スナップショット | 原則なし | `v13_research_reader`のみ、default read-only |
| `saxo_forward_v13` | RF0以後のappend-only forwardデータ | `v13_forward_writer`経由 | gate通過までreaderを与えない |

### Role

- `saxo_db_owner`: NOLOGIN、object owner
- `saxo_migrator`: DDL/migration専用
- `saxo_ingest`: ingestion DMLと許可済みprocedure
- `saxo_app_reader`: curated/opsのアプリ読取
- `saxo_analyst_reader`: analytics用途の読取
- `saxo_ops_operator`: 許可済みquality/backup procedureの実行専用
- `v13_research_reader`: research snapshot専用
- `v13_forward_writer`: forward append専用
- `postgres`: bootstrap/emergency専用。通常利用しない

reader roleには`statement_timeout`、`default_transaction_read_only=on`、`temp_file_limit`を設定します。PUBLICの不要な`CREATE`権限と、不要なschema権限を剥奪します。

### Schemaと主要テーブル

schemaは `catalog`、`ops`、`raw`、`staging`、`curated`、`derived`、`quality`、`analytics` です。

主要テーブルは次のとおりです。厳密な列・型・制約は機械仕様を正本としてください。

- `catalog.source_dataset`
- `catalog.session_calendar`
- `catalog.session_interval`
- `catalog.instrument`
- `ops.ingestion_run`
- `ops.source_file`
- `ops.watermark`
- `raw.market_bar_revision`
- `raw.reference_observation`
- `curated.market_bar`
- `curated.etf_total_return_daily`
- `derived.market_bar_4h`
- `derived.market_bar_1d_risk`
- `quality.event`
- `ops.research_snapshot`
- `ops.backup_run`
- `ops.schema_migration`

運用者はtableを直接探索するのではなく、read-only viewと`python3 -m market_db.inspect`を標準入口として、dataset inventory、coverage、freshness、lineage、ingestion run、品質event、storage、backup状態を確認します。品質eventとbackup実績の状態更新は、`saxo_ops_operator`を使う`market_db.operate`の許可済みprocedureだけに限定します。DB2では実データの件数・期間・品質をgate化済みです。session calendar未登録のcoverage/freshnessはDB3まで`NOT_EVALUATED`です。

固定viewは`analytics.v_data_inventory`、`analytics.v_data_coverage`、`analytics.v_data_lineage`、`ops.v_ingestion_status`、`quality.v_open_event`、`ops.v_storage_usage`、`ops.v_backup_status`です。research DBにはinventory/coverage/lineage/storageだけを公開し、forward DBは評価ゲート前の参照対象にしません。

時刻は`TIMESTAMPTZ`のUTC、日付は`DATE`、価格は`NUMERIC(24,12)`、volumeは`NUMERIC(30,8)`、SHA-256は`CHAR(64)`、構造化メタデータは`JSONB`を使います。

## 6. 実施済みPhase DB1

DB1は、データを投入せず、Docker/PostgreSQL基盤、migration、role境界を再現可能な形で構築し、2026-07-16にruntime gateを含めてPASSしました。詳細は`docs/db1_implementation_result.md`と`manifests/db1_implementation_manifest.json`を参照してください。

### DB1で作成済みの成果物

DB1では最低限、次を作成済みです。

```text
compose.yaml
.env.example
.dockerignore
requirements.txt
scripts/create_local_db_secrets.py
db/migrations/0001_bootstrap.sql
db/migrations/0002_market_schema.sql
db/migrations/0003_research_schema.sql
db/migrations/0004_forward_schema.sql
db/migrations/0005_grants_and_forward_append.sql
db/migrations/0006_operational_views.sql
db/migrations/0007_operational_procedures.sql
db/migrations/0008_quality_privilege_hardening.sql
market_db/__init__.py
market_db/connection.py
market_db/migrate.py
market_db/validate.py
market_db/inspect.py
market_db/operate.py
tests/
tests/test_inspect_cli.py
tests/test_operate_cli.py
manifests/db1_implementation_manifest.json
docs/db1_implementation_result.md
docs/database_operations_runbook.md
```

必要ならmigration SQLを責務単位で追加して構いません。ただし番号、適用順、SHA-256、適用時刻を`ops.schema_migration`へ記録し、同一番号の内容変更を拒否する仕組みにしてください。destructive down migrationは作りません。

### DB1の実行順序

1. 移管manifestと69 CSVの相対パス・サイズ・SHA-256を再検証する。
2. Git状態を確認し、ユーザーの既存変更を保護する。
3. `.gitignore`を確認し、`.secrets/`、`.env`、backup、DB dump、Python cacheを除外する。
4. `create_local_db_secrets.py`で高強度passwordを生成し、`.secrets/`へ0600で保存する。値は表示しない。
5. 凍結仕様どおりに`compose.yaml`を作り、`docker compose config`で検証する。
6. imageをpullし、実際のdigestをDB1 manifestへ記録する。
7. named volumeを使ってPostgreSQLを起動し、healthcheckがhealthyになることを確認する。
8. bootstrap/migrationを実行して3 DB、role、schema、最低限のtable・constraint・運用viewを作る。
9. `market_db.inspect`の全subcommandが空DBで0件または`NOT_EVALUATED`を安全に返すことを確認する。
10. writer/reader/owner/migrator/operatorの権限分離、運用view/inspect CLIのread-only性、operate CLIのprocedure-only更新を自動テストする。
11. コンテナ再起動後もschema、view、migration履歴が残ることを確認する。
12. 起動・確認・障害対応・secret rotation・backup/restoreのrunbookを作成する。
13. 実行コマンド、digest、migration checksum、テスト結果、未実施事項を結果文書とmanifestに記録する。

### DB1では行わないこと

- CSVをraw/curatedへ投入しない
- Saxo APIへ接続しない
- 24時間tokenを求めない・保存しない
- watermark増分更新を実行しない
- 4H/1Dを生成しない
- research snapshotをデータで満たさない
- 分析、特徴量、シグナル、PnL、WFO、Holdoutを計算しない

## 7. DB1品質ゲート

次の全項目が確認できたときだけDB1をPASSとします。

- `docker compose config`が成功する。
- PostgreSQLは`127.0.0.1:54329`からのみ接続でき、外部interfaceへ公開されない。
- serviceがhealthyで、再起動後もnamed volume上の状態が維持される。
- image tagと取得digestがmanifestに記録される。
- 3つの物理DBと定義済みroleが存在する。
- `saxo_db_owner`はNOLOGINで、通常接続に使われない。
- readerはDDL/DMLできず、writerは許可外schemaへ書き込めない。
- `v13_research_reader`は`saxo_market`へ接続できない。
- `v13_forward_writer`は許可済みappend経路以外で変更できない。
- migrationは初回適用、再実行、checksum不一致拒否をテスト済みである。
- `catalog.source_dataset`、`ops.backup_run`、品質event lifecycle列、定義済み運用viewが存在する。
- `market_db.inspect`のinventory、coverage、freshness、runs、quality、lineage、storage、backupsが空DBで安全に動作する。
- 運用view/CLIはread-onlyで、任意SQLやcredentialを受け付けない。
- `saxo_ops_operator`は許可済みquality/backup procedureだけを実行でき、tableの直接DMLと市場データ変更ができない。
- repositoryとログにpassword/token/account IDが残っていない。
- 移管済み69 CSVのSHA-256が変化していない。
- DB1で市場データが投入されていない。
- `docs/db1_implementation_result.md`と`manifests/db1_implementation_manifest.json`が作成されている。

成果物が存在するだけではPASSにしません。実行時検証が失敗した場合は、`FAIL`または`BLOCKED`として原因、再現手順、次に必要な条件を記録してください。

## 8. DB3の現在地と後続工程

DB2 PASSによりDB3だけを解放しました。DB4とRT0は引き続きLOCKEDです。

### Phase DB2：既存CSVインポート（PASS）

- 69 source file、781,808 source rowをimmutableな入力として登録した。
- raw market bar 636,629行、reference 90,894行、curated 1H 394,992行、ETF total-return 54,285行を分類保存した。
- 既知品質FAIL 5件はrawを改変せずOPEN eventとして保持した。
- source file単位のlineage不一致0件、coverageは`NOT_EVALUATED`を確認した。
- cutoff以前だけを`saxo_research_v13`へ物理copyし、default read-only化した。
- snapshot content/dump manifest、dump SHA-256、`pg_restore --list`を検証した。restore smoke testはDB4までLOCKEDである。

### Phase DB3：増分更新（PASS）

- SIM限定GET allow-list、token redaction、429および一時的network例外の1/2/4秒・最大4attempt有限retryを実装した。
- Etf 20本、FxSpot 72本の実bar overlapと`Mode=From` forward pagingを実装した。
- raw JSON atomic保存、SHA-256、source file、raw revision、curated latest、watermarkをrun IDで追跡する。
- stage → validate → raw append → curated upsert → watermark → 4H/1Dを単一transactionでcommitする。失敗時はDB変更をrollbackし、raw artifactとsanitized run metadataだけを残す。
- `DataVersion`変化時は`STALE_DATA_VERSION`で停止する。対象1銘柄だけを`Mode=UpTo`で全取得し、guard付きprocedureで置換する`full-refetch`経路を実装した。
- full-refetch限定で、過去FxSpot High/Low交差を最大10 unique rowかつ全観測の0.01%以下だけ無補正隔離する。raw JSON・SHA-256・元Bid/Askを保持し、`rejected_rows`と解決済みWARNへ記録する。通常run、Open/Close、最新sample、閾値超過は全runをFAILする。
- localhost限定operator UIは、任意commandを受け付けず、固定`market_db.incremental_update reconcile`をsingle jobとして起動する。CSRF、same-origin、no-store、CSP、token出力redactionを強制する。
- ETF calendarはholiday、短縮取引、DST、例外休場をUTC化した。FXはSaxo live trading schedule照合前なのでprovisional・`NOT_EVALUATED`である。
- coverageはmissingとout-of-sessionを分ける。既存ETFは11銘柄ともWARN、FX 2銘柄はNOT_EVALUATEDで、FAILは0。
- 現在のwatermarkは`ACTIVE=13`であり、freshnessはSTALE 11、NOT_EVALUATED 2、FAIL 0である。
- EURUSDは過去High/Low交差5件、USDJPYは9件を上限内で無補正隔離し、raw原本と解決済みWARNを保持した。
- 通常run 104・105が連続PASSし、DB3総合validatorもPASSした。分割実行した全test 74件はPASS。

### Phase DB4：参照・運用ゲート

- inventory、coverage、freshness、lineage、run、quality、storage、backup statusのread APIを作る。
- quality eventのacknowledge/resolveとbackup実績更新を、監査可能なprocedure経由に限定する。
- 運用CLIとread APIの件数・期間・状態が一致することを確認する。
- `ops.backup_run`へbackup・検証・restore smoke test結果を記録する。
- backup/restore smoke testを行う。
- daily 7世代・weekly 4世代のretentionとdisk使用量を確認する。
- runbookに沿った起動、restart、障害、secret rotation、restoreのdrillを行う。

DB1〜DB4がすべてPASSして初めて、短期戦略研究のPhase RT0へ進めます。

## 9. 運用・バックアップの凍結方針

- backup形式: `pg_dump` custom format
- 保存先: `backups/postgres/`
- 実施時点: 初回import後、schema migration前後、成功した増分更新後は最大日次1回
- retention: daily 7世代、weekly 4世代
- 各dumpはSHA-256と`pg_restore --list`で検証する。
- 月次で別名DBへのrestore smoke testを実施する。
- backup、dump、秘密情報はGitへ追加しない。

## 10. 作業中の確認コマンド

仕様・移管bundleの確認:

```bash
python3 -m json.tool specs/saxo_db_import_spec.json >/dev/null
python3 -m json.tool specs/v13_phase_db0_database_spec.json >/dev/null
git status --short
```

DB3現在状態の確認例:

```bash
docker compose -p saxo-market-data config
docker compose -p saxo-market-data ps
docker compose -p saxo-market-data exec postgres pg_isready -d saxo_market
SAXO_DB_INTEGRATION=1 python3 -m pytest
python3 -m market_db.validate --phase db3
python3 -m market_db.session_calendar status
python3 -m market_db.incremental_update status
python3 -m market_db.operator_ui
python3 -m market_db.import_legacy status
python3 -m market_db.research_snapshot status
python3 -m market_db.inspect inventory
python3 -m market_db.inspect lineage
python3 -m market_db.inspect quality
python3 -m market_db.inspect storage
```

実際の接続情報は`.secrets/`からプロセス環境へ注入し、コマンドライン引数、shell history、ログへpasswordを露出させないでください。

## 11. 別AIが最後に報告する形式

DB3作業の完了報告には、最低限次を含めてください。

1. 結論: `PASS` / `FAIL` / `BLOCKED`
2. 作成・変更したファイル
3. canonical 13、watermark、raw/curated/4H/1Dの実測件数
4. revision、quality event、coverage、freshness状態
5. 適用migration番号とSHA-256
6. research DBが未変更・read-onlyであること
7. 全testとDB3 validatorのoffline/live結果
8. 移管69 CSVが未変更であること
9. token/account情報保存0、注文/precheck 0、戦略計算0
10. 残課題と、次に解放されたPhase

「コードを書いた」「テストファイルを作った」だけでPASSと報告しないでください。Docker daemonが利用できない等で実行確認できない場合は、実装ゲートと実行ゲートを分け、実行ゲートを`BLOCKED`としてください。

## 12. 再開用プロンプト

別のAIへは、次の依頼文で再開できます。

> repository rootの`README.md`、DB3計画・結果、運用runbookを読んでください。DB3はcanonical 13の`ACTIVE`、通常run 104・105連続PASS、総合validator PASSで完了しています。次に許可されたPhase DB4のread API、backup/restore、retention、runbook drillだけを実装してください。RT0、特徴量、戦略、PnL、WFO、HoldoutはDB4 PASSまで開始しないでください。

---

最終更新: 2026-07-17 JST
現在のゲート: `DB0 v2=RE-FROZEN / DB1=PASS / DB2=PASS / DB3=PASS / DB4=NEXT / RT0=LOCKED`
