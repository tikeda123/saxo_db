# saxo_db 別Mac構築ランブック

更新日: 2026-08-07 JST

対象: 新しいmacOS端末へ`Saxo DB`のコード、ローカルPostgreSQL、Read API、任意のSaxo SIM定期更新を再現可能に構築する運用者

既定状態: **Git管理の人工CSVでschema/import/read-only interfaceの配線だけを確認し、Saxo API・scheduler・研究利用・注文・口座・資金操作は開始しない**

## 1. 目的と対象範囲

この手順は、GitHubの正規repositoryから新しいMacへ次を構築する。

- Python仮想環境と固定dependency
- loopback限定PostgreSQL 18.4 containerと3 database
- checksum付きmigration
- loopback限定Read API / データ管理Web UI
- 新Macで人が再認証した場合だけ有効化できるGET-only Saxo SIM scheduler
- health、reader role、read-only transaction、data quality状態の検証手順
- 外部市場データを含まないGit管理のsynthetic CSV smoke seed

次は対象外である。

- 売買戦略、特徴量、WFO/Holdout、PnL、allocationの実装・実行
- 注文、precheck、取消、position、口座、資金の操作
- 既存MacのKeychain、token、runtime、Docker volume、DB実データ、log、backupの複製
- public GitHubを使ったSaxo/Yahoo/FRED由来69 CSVの配布
- 新Mac構築と同時に行うSaxo full-refetch、USDJPY再開、quarantine解除
- LaunchAgentのinstall、LAN/InternetへのDB・API公開

## 2. 完了状態

最小構築の完了条件は次のとおりである。市場データが空でもinterfaceの構築は完了できるが、データ品質や研究利用をPASSとは判定しない。

1. `docker compose ... ps`でPostgreSQLが`running (healthy)`。
2. `market_db.migrate validate`が全適用migrationのchecksum一致を返す。
3. Read API preflightが`status=PASS`、`127.0.0.1:8766`、database `saxo_market`、role `saxo_app_reader`、`transaction_read_only=on`を返す。
4. `health -> inventory -> coverage -> freshness -> quality -> lineage`の順で状態を確認し、空、`NOT_EVALUATED`、`STALE`、`WARN`をPASSへ読み替えない。
5. schedulerは明示的に有効化するまで`STOPPED`。有効化する場合もUSDJPYを含まないscopeを使用する。
6. orders、prechecks、account/fund operationsは0件。

## 3. 前提

### 3.1 必須software

| 項目 | 要件 |
|---|---|
| OS | macOS。Apple Silicon / Intelのどちらでもよいが、実機で使用するDocker imageのarchitecture対応を確認する |
| Git | `git` commandが利用可能。SSH cloneの場合は新Mac自身のGitHub SSH認証を設定する |
| Python | Python 3.12以上。このrepositoryの検証環境はPython 3.12系 |
| container runtime | Docker Desktop、またはColima + Docker CLIのどちらか一方 |
| Docker Compose | `docker compose` subcommandが利用可能 |
| PostgreSQL | hostへのinstallは不要。`postgres:18.4-bookworm`をComposeで起動する |

Docker Desktopを使用する場合は起動後、Colimaを使用する場合は`colima start`後に次を確認する。

```bash
git --version
python3.12 --version
docker --version
docker compose version
docker info
```

Docker DesktopとColimaのdaemonを同時に使わない。`docker context show`と`docker info`で、意図したlocal daemonへ接続していることを確認する。

### 3.2 Port

| Port | 用途 | bind |
|---:|---|---|
| 54329 | PostgreSQL | `127.0.0.1`のみ |
| 8766 | Read API / データ管理Web UI | `127.0.0.1`のみ |
| 8765 | OAuth / scheduler Operator UI | `127.0.0.1`のみ。認証を行う場合だけ使用 |

競合時に不明なprocessを停止したり、LAN bindへ変更したりしない。

## 4. 既存Macから移してよいもの・禁止するもの

新Macではrepository directoryをFinder/AirDrop等で複製せず、GitHubからcloneする。

| 分類 | 対象 | 扱い |
|---|---|---|
| 正規に取得 | Git管理されたcode、migration、docs、specs、manifest | `origin/main`からcloneする |
| 正規に取得 | `bootstrap/seed/`の人工CSV 3 file | GitHubからcloneする。migration/import/APIのsmoke専用。市場データ、研究、運用には使用禁止 |
| 条件付き | `data/import/`のimmutable実市場CSV 69 file | public Gitには含めない。権利確認済みの正規bundleをアクセス制御済み経路で用意できる場合だけ、inventoryのpath・size・row count・SHA-256を全件検証する。編集禁止 |
| 禁止 | macOS KeychainのSaxo credential / App Key entry | 移さない。新Mac上で人が初回設定・OAuthをやり直す |
| 禁止 | access token、refresh token、PKCE verifier、AccountKey、ClientKey、口座識別子 | export、表示、copy、chat貼付、file保存をしない |
| 禁止 | `.secrets/`、`.env` | 移さない。DB passwordは新Macで新規生成する |
| 禁止 | `.venv/` | 移さない。新MacのPythonから作成し直す |
| 禁止 | `.runtime/`、PID/state、log | 移さない。process identityはMac間で再利用できない |
| 禁止 | Docker volume `saxo_pg18_data`、`pgdata/`、DB実データdirectory | file copyしない。破損・3 DB不整合の原因になる |
| 禁止 | `data/acquisition/runs/` | 移さない。旧Macのruntime acquisition artifactとして保持する |
| 禁止 | `backups/`、`*.dump`、`*.manifest.json`のad-hoc copy | この標準手順では移さない。実DB移行は第8節の別承認手順とする |
| 禁止 | `exports/`、Parquet、任意log | bootstrap入力に使わず、新Macで必要時に再生成する |

## 5. Cloneとhost preflight

```bash
git clone git@github.com:tikeda123/saxo_db.git
cd saxo_db
git switch main
git fetch origin --prune
git status --short --branch
git rev-list --left-right --count main...origin/main
python3.12 scripts/verify_new_mac_environment.py --expect-clean-clone
```

期待値は`main...origin/main`のahead/behindが`0 0`、preflightが`status=PASS`である。preflightはSaxo API、PostgreSQL接続、file作成、DB書込みを行わない。Docker daemonをまだ起動していない段階では、toolとrepository contractだけを次で確認できる。

```bash
python3.12 scripts/verify_new_mac_environment.py \
  --expect-clean-clone --skip-docker-daemon
```

`FRESH_CLONE_HAS_NO_COPIED_LOCAL_STATE`がFAILした場合は、どのlocal-only directoryが混入したかを確認する。内容や秘密値を表示せず、そのcloneをbootstrapへ使用しない。

## 6. Python・local secret・PostgreSQL

### 6.1 仮想環境

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python --version
.venv/bin/python -m pytest -q tests/test_new_mac_environment.py tests/test_secret_generation.py
```

`.venv`はGit管理外であり、旧Macからcopyしない。

### 6.2 DB password生成

```bash
.venv/bin/python scripts/create_local_db_secrets.py
.venv/bin/python scripts/create_local_db_secrets.py --check
```

`.secrets/`はdirectory `0700`、8 fileは`0600`で新規生成される。値をterminal、clipboard、log、manifestへ出さない。旧Macのpassword fileを移してはならない。

### 6.3 PostgreSQLとmigration

```bash
docker compose -p saxo-market-data config
docker compose -p saxo-market-data pull postgres
docker compose -p saxo-market-data up -d postgres
docker compose -p saxo-market-data ps
.venv/bin/python -m market_db.migrate all --through 0018
.venv/bin/python -m market_db.migrate validate
```

`migrate all --through 0018`はlocal clusterにrole、`saxo_market`、`saxo_research_v13`、`saxo_forward_v13`を作成し、データ依存前までのchecksum付きmigrationを適用する。`0019`は11 ETF dataset/mappingを検証するため、CSV import後に適用する。`docker compose down -v`、volume削除、database drop、適用済みmigrationの編集は禁止する。

この時点はcore schema-onlyであり、Read APIのfull contractはまだ完成していない。次節でbootstrap方式を一つだけ選ぶ。

## 7. データbootstrapの選択

### 選択A: Git管理synthetic seed（既定・interface確認用）

3 CSV / 55人工行 / 2,323 bytesだけを使用する。Saxo、Yahoo Finance、FREDその他の実市場データを含まない。

```bash
.venv/bin/python scripts/verify_bootstrap_seed.py
.venv/bin/python -m market_db.bootstrap_seed verify
.venv/bin/python -m market_db.bootstrap_seed import
.venv/bin/python -m market_db.bootstrap_seed status
.venv/bin/python -m market_db.migrate apply
.venv/bin/python -m market_db.migrate validate
```

`bootstrap_seed import`は0018適用済み、0019未適用、主要data relationが空の場合だけ1 transactionで実行する。全値を`SYNTHETIC_BOOTSTRAP_ONLY`、`NOT_EVALUATED`、inactiveとして登録する。結果はCSV→raw/curated→Read APIの配線確認に限り、current data、total-return品質、official close、研究readinessを証明しない。

このDBへ正規データを後から混在させず、schedulerを起動しない。正規運用へ進む場合は別の空clusterを選択Bの正規bundleから構築する。詳細と公開可否判定は[別Mac用CSV bootstrap監査](new_mac_csv_bootstrap_audit.md)を参照する。

### 選択B: 権利確認済みimmutable実市場CSVからDB2を再構築

69 file / 781,808 rows / 160,403,659 bytesの検証済みimport bundleを、public GitHub以外のアクセス制御済み経路で用意できる場合だけ実施する。Git管理の`manifests/import_file_inventory.csv`と完全一致しないbundleは使用しない。

```bash
.venv/bin/python -m market_db.import_legacy verify
.venv/bin/python -m market_db.import_legacy import
.venv/bin/python -m market_db.import_legacy status
.venv/bin/python -m market_db.migrate apply
.venv/bin/python -m market_db.migrate validate
.venv/bin/python -m market_db.research_snapshot create
.venv/bin/python -m market_db.research_snapshot status
```

必須条件:

- `verify`が69 file、row count、size、source/copied SHA-256の全件一致を返す。
- Saxo/Yahoo/FRED由来CSVをGit add、Git LFS、release asset、public URLへ置かない。
- importは1 file単位transactionで行い、途中失敗時にCSVや台帳を修正しない。
- research snapshotのcutoff、content manifest、database default read-onlyを照合する。
- `saxo_market`と`saxo_research_v13`のlineageが同じinventoryへ結び付く。
- 既知WARN/FAIL/`NOT_EVALUATED`を削除・PASS化しない。

### 選択C: core schema-onlyで停止

第6節で止める。これはmigration 0018までの開発・監査状態であり、full Read API contract完成や市場データ利用可能を意味しない。後で選択A/Bのどちらか一方を空DBへ投入する。schedulerを初回backfill代わりに起動しない。

### 選択D: Saxoから新規取得

新Mac構築の既定手順では実施しない。Saxo OAuthを新Macで設定し、既存baseline・watermark・calendar・scopeが整合することを確認した後の通常運用である。空DBを無条件full-refetchする手段ではない。DataVersion warning、instrument drift、quality gateを回避してはならない。

## 8. Backup restoreをデータ移行に使う場合

このrepositoryの`market_db.backup restore-smoke`は、一時DBへ復元して元DBとsignatureを比較し、一時DBを削除する検査機能である。既存3 DBを上書きするproduction restore CLIではない。この標準ランブックでは旧Macのbackupを新Macへcopy・restoreしない。

それでも実DB移行が必要な場合は別作業として明示承認し、少なくとも次を満たす移行計画を先に作る。

1. 旧Macのwriter/schedulerを停止し、3 DBの取得中に書込みがないことを証明する。
2. `saxo_market`、`saxo_research_v13`、`saxo_forward_v13`の3 dumpとmanifestを同一移行setとして識別する。
3. 各manifestが`status=PASS`、SHA-256/size/`pg_restore --list` PASSである。
4. 新Macの既存DBへ上書きせず、allow-list済みの隔離DB名へrestoreする。
5. 3 DBすべてでmigration番号・checksum、relation count、primary-key duplicate 0、dataset/lineage、research cutoff、read-only default、role grantを比較する。
6. market DBのwatermark、latest ingestion run、derived row、quality event、C2 overlayを比較し、USDJPY quarantineを維持する。
7. 不一致時はcutoverせず隔離DBを破棄し、旧Mac正本を変更しない。
8. cutover/rollback手順、停止時間、両Macのsingle-writer保証を別承認する。

Docker volume directoryのcopy、`pgdata/` copy、1 DBだけのrestore、既存DBへの`pg_restore --clean`は行わない。

## 9. Read APIとread-only検証

```bash
.venv/bin/python -m market_db.read_api_service start
.venv/bin/python -m market_db.read_api_service status --format json
.venv/bin/python -m market_db.read_api_preflight --format json
curl --fail http://127.0.0.1:8766/health
```

`/health`の必須値:

```text
status=PASS
database_name=saxo_market
role_name=saxo_app_reader
transaction_read_only=on
statement_timeout=30s
```

選択AまたはBでfull migrationまで完了した場合だけ次を実行する。

```bash
.venv/bin/python -m market_db.inspect inventory
.venv/bin/python -m market_db.inspect coverage
.venv/bin/python -m market_db.inspect freshness
.venv/bin/python -m market_db.inspect quality --fail-on-alert
.venv/bin/python -m market_db.inspect lineage
```

`quality --fail-on-alert`のexit 2、`UNKNOWN`、`STALE`、`NOT_EVALUATED`は記録して停止し、正常へ読み替えない。選択Aの人工行が`NOT_EVALUATED`/staleなのは意図した状態であり、正常な市場データへ読み替えない。Web UIは<http://127.0.0.1:8766/ui/overview>で参照できる。

## 10. Saxo認証とscheduler（明示的な任意工程）

### 10.1 新Macでの再認証

Keychain credentialは移行しない。新Macの利用者が、SIM固定・PKCE・trading disabledのdata-only applicationを使って初回設定する。

1. Saxo Developer Portalで既存SIM appのredirect URIが`http://localhost/saxo/oauth/callback`であることを人が確認する。
2. Operator UIを起動する。
3. <http://127.0.0.1:8765/>の画面でApp Keyを明示保存し、macOS Keychainの承認に従う。値は再表示しない。
4. 「Saxo OAuth接続」を選び、新Mac上で人がlogin/同意する。
5. `AUTH_READY`を確認する。認証完了だけではschedulerを開始しない。

```bash
.venv/bin/python -m market_db.operator_ui_service restart --port 8765
.venv/bin/python -m market_db.saxo_auth status --callback-port 8765
```

refresh credentialとApp KeyはKeychain、access tokenはprocess memoryだけに置く。repository、`.env`、`.secrets/`、argv、DB、log、browser storageへ保存しない。

### 10.2 scheduler開始前gate

開始前に次をすべて確認する。

- DB bootstrapが完了し、migration checksumと必要なwatermark/calendarが整合する。
- Read API health/read-onlyはPASS。
- OAuthは`AUTH_READY`、environmentは`SIM`。
- `specs/source_collection/fx_research_candidate_scheduler_scope_v1.json`のactivation gateが現DB状態と一致する。
- scopeは`all_except_usdjpy_with_fx_research_candidates_20260727`、includedはETF11/EURUSD/AUDUSD/USDCAD/USDCHF、excludedはUSDJPY。
- USDJPYは`BLOCKED_PROVIDER_CONTENT_QUALITY`のまま。取得、version probe、full-refetch、再開を行わない。
- orders/prechecks/write requestsは0。

明示的に開始する場合だけ次を実行する。

```bash
.venv/bin/python -m market_db.periodic_update_service start \
  --callback-port 8765 \
  --scope-profile all_except_usdjpy_with_fx_research_candidates_20260727
.venv/bin/python -m market_db.periodic_update_service status
```

このschedulerはallow-list済みSaxo SIM GETを実行しDBへ市場データを書き込むため、単なる新Mac構築確認では開始しない。Strategy側の注文app、credential、order adapterを共有しない。

## 11. 停止

```bash
.venv/bin/python -m market_db.periodic_update_service stop
.venv/bin/python -m market_db.operator_ui_service stop --port 8765
.venv/bin/python -m market_db.read_api_service stop
docker compose -p saxo-market-data stop postgres
```

停止時もvolume、DB、raw、backup、Keychainを削除しない。credentialを明示的に失効させる場合だけ、影響確認後に`market_db.saxo_auth logout`を使用する。

## 12. Code更新

更新前に未commit差分がないことを確認し、schedulerとRead APIを停止する。DBに重要なlocal更新がある場合は、同じMac内で検証済みbackup/restore-smokeを作成してから進める。backupをGitや別Macへ移さない。

```bash
git status --short --branch
git fetch origin --prune
git pull --ff-only origin main
.venv/bin/python -m pip install -r requirements.txt
docker compose -p saxo-market-data pull postgres
docker compose -p saxo-market-data up -d postgres
.venv/bin/python -m market_db.migrate apply
.venv/bin/python -m market_db.migrate validate
.venv/bin/python -m market_db.read_api_service start
.venv/bin/python -m market_db.read_api_preflight --format json
```

適用済みmigrationを変更しない。checksum mismatch時は停止し、Git版、DB適用履歴、対象Macを照合する。

## 13. トラブルシュート

### Docker / Colima

```bash
docker context show
docker info
docker compose -p saxo-market-data ps
docker compose -p saxo-market-data logs --tail 100 postgres
```

daemon停止やColima disk lockはinterface/operational blockであり、data quality FAILではない。`docker compose down -v`やvolume削除で直さない。

### Port競合

```bash
lsof -nP -iTCP:54329 -sTCP:LISTEN
lsof -nP -iTCP:8765 -sTCP:LISTEN
lsof -nP -iTCP:8766 -sTCP:LISTEN
```

repository service managerはcwd、command、PID/start fingerprint、healthが一致するprocessだけを停止する。未知processを`kill`/`pkill`しない。

### Read API

- `BLOCKED_DATABASE_UNHEALTHY`: PostgreSQLのhealth/logを確認する。
- `BLOCKED_PORT_CONFLICT`: listenerを識別し、勝手に停止しない。
- `BLOCKED_READ_ONLY_BOUNDARY`: role、transaction read-only、timeout差分を解消するまでconsumerへ公開しない。
- `BLOCKED_STALE_PID`: state fileを書き換えず、process identityを確認する。

### OAuth / scheduler

- `AUTH_CONFIG_MISSING`: 新MacのOperator UIでApp Keyを人が設定する。
- `AUTH_LOGIN_REQUIRED`: 新MacでOAuthをやり直す。旧MacのKeychainをcopyしない。
- `AUTH_KEYCHAIN_*`: Keychain lock/access controlを確認し、値をterminalへ出さない。
- `DATA_NOT_READY`: data-not-readyとして扱い、品質FAILや無制限retryへ変えない。
- `BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED`: current total-return jobを開始しない。
- USDJPY関連: quarantineを維持し、他系列のbootstrapと混ぜない。

### Import / migration

- import verify失敗時はCSV・inventoryを編集しない。
- synthetic seedを既存DB、正規DB、scheduler対象DBへimportしない。
- migration 0019を空DBへ先行適用しない。0018 → CSV import → 0019以降の順序を守る。
- migration checksum mismatch時は適用済みSQLを変更しない。
- 3 DBの一部だけが進んだ場合はRead API/schedulerを開始せず、migration結果とtransaction logを確認する。

## 14. 引渡しチェックリスト

- [ ] `origin/main`からclean cloneし、ahead/behindが0/0
- [ ] 新Macpreflight PASS
- [ ] Python 3.12 venvと固定dependencyを新規作成
- [ ] `.secrets/`を新Macで生成し、mode検証PASS
- [ ] PostgreSQL 18.4 healthy、3 DB migration checksum PASS
- [ ] データbootstrap方式と証拠を記録
- [ ] synthetic seedの場合は`SYNTHETIC_BOOTSTRAP_ONLY`であり研究・運用へ接続していない
- [ ] Read API health/role/read-only/timeout PASS
- [ ] inventory/coverage/freshness/quality/lineageを状態どおり記録
- [ ] 旧MacのKeychain/token/runtime/DB/log/backupを移していない
- [ ] schedulerは既定STOPPED。開始した場合はSIM/GET-only/scope/USDJPY除外を確認
- [ ] orders/prechecks/account/fund operations 0

通常運用の詳細は[データ管理・運用ランブック](database_operations_runbook.md)、外部consumer契約は[Read APIインターフェース仕様](read_api_interface.md)を参照する。
