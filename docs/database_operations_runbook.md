# Saxo DB データ管理・運用ランブック

更新日: 2026-07-19 JST
対象: Phase DB1 infrastructure / DB2 legacy import / DB3 incremental market data / DB4 read and operations
状態: **DB1・DB2・DB3・DB4・DMUI4 PASS、データ管理基盤として継続運用**

## 1. 安全境界

- すべてrepository rootから実行する。Pythonコードはhost固有の絶対pathを前提にしない。
- passwordは`.secrets/`の権限`0600`のfileだけに置き、terminal、log、manifestへ表示しない。
- Saxo access token、AccountKey、ClientKey、口座識別子は本プロジェクトへ保存しない。OAuth refresh credentialだけは無人更新用としてmacOS Keychainへ保存できるが、repository、`.secrets/`、DB、manifest、logには保存しない。
- 69 CSVはimmutableな監査原本であり、上書き・整形・削除しない。再取込前にinventoryのsize/SHA-256を必ず検証する。
- DB2のimportとresearch snapshotは完了済みである。通常確認では`status`とread-only inspectを使い、既存内容を手動更新しない。
- `docker compose down -v`、`docker volume rm`、database drop、migration fileの上書きは実行禁止。volume削除が必要な場合は事前に影響を提示し、明示承認を得る。

## 2. 初回セットアップ

```bash
python3 scripts/create_local_db_secrets.py
docker compose -p saxo-market-data config
docker compose -p saxo-market-data pull postgres
docker compose -p saxo-market-data up -d postgres
python3 -m market_db.migrate all
```

期待結果はsecret 8件の準備、解決済みCompose設定の表示、`postgres`の`healthy`化、3 databaseへのmigration適用である。secret値はどの出力にも現れない。

## 3. 日常の起動・状態確認・停止

起動:

```bash
docker compose -p saxo-market-data up -d postgres
docker compose -p saxo-market-data ps
.venv/bin/python -m market_db.read_api_service start
.venv/bin/python -m market_db.read_api_service status --format json
.venv/bin/python -m market_db.read_api_preflight --format json
```

通常restart:

```bash
docker compose -p saxo-market-data restart postgres
docker compose -p saxo-market-data ps
```

停止（volumeは保持される）:

```bash
.venv/bin/python -m market_db.read_api_service stop
docker compose -p saxo-market-data stop postgres
```

`ps`ではPostgreSQLが`running (healthy)`で、host側portは`127.0.0.1:54329`だけにbindされる。Read API preflightは`PASS`、`127.0.0.1:8766`、`saxo_app_reader`、read-only、30秒timeout、API v1/revision 1.2を返すことを確認する。restart後はmigration履歴とschemaが保持される。

## 4. 格納データの確認

任意SQLは使わず、固定viewをreader roleで参照する。

```bash
python3 -m market_db.inspect inventory
python3 -m market_db.inspect coverage
python3 -m market_db.inspect freshness --format json
python3 -m market_db.inspect runs
python3 -m market_db.inspect quality --fail-on-alert
python3 -m market_db.inspect lineage
python3 -m market_db.inspect storage
python3 -m market_db.inspect backups
python3 -m market_db.inspect inventory --database saxo_research_v13
```

exit codeは正常`0`、接続・設定・query失敗`1`、`--fail-on-alert`で警告条件を検出した場合`2`。calendarや期待更新間隔が未登録ならcoverage/freshnessは`NOT_EVALUATED`となり、根拠なしにPASSへしない。DB3の`coverage`は`missing_rows`、`out_of_session_rows`、`calendar_aligned_rows`を分離し、`freshness`はwatermarkと次のcalendar slotを比較する。`saxo_forward_v13`のinspectは評価ゲートまで拒否される。

DB2完了時点では、market inventoryは実データを返し、DB2 legacy importのlineageは69 source fileを返す。DB3開始後の`ops.source_file`とlineage全体はlive取得artifactを監査追記するため69件を超える。DB2 baselineの照合は`ops.ingestion_run.trigger='DB2_LEGACY_IMPORT'`に限定する。`quality --fail-on-alert`でERROR/CRITICALのCURRENTまたはUNKNOWNを検出した場合はexit 2となる。2026-07-19のmigration 0015適用時点では旧OPEN event 22件が未reviewのため全件UNKNOWNであり、正常扱いせずDMI1Bを`BLOCKED_DATA_RECONCILIATION`に保つ。根拠なくresolve、HISTORICAL分類、raw修正をしない。

## 5. Qualityとbackup状態の限定更新

quality noteは引数へ書かず、promptまたはstdinから入力する。

```bash
python3 -m market_db.operate acknowledge-quality 123 --operator operator-label
python3 -m market_db.operate resolve-quality 123 --operator operator-label
python3 -m market_db.operate record-quality-scope 123 \
  --scope-kind INSTRUMENT --affected-layer curated --price-basis native_ohlc \
  --operator operator-label
python3 -m market_db.operate review-quality 123 \
  --applicability HISTORICAL --superseded-by-run-id 456 \
  --operator operator-label
python3 -m market_db.operate start-backup saxo_market backups/example.dump
python3 -m market_db.operate finish-backup 1 FAILED --error-code MANUAL_CHECK
```

scope evidence、applicability reason、quality noteは非表示promptまたはstdinから入力し、shell引数へ書かない。scope/reviewは追記専用で、誤りの修正は既存rowのUPDATEではなく新しいrowを追記する。HISTORICALは対象run manifest、後続PASS run、現在watermark/coverage/freshnessの証拠を確認してから選択する。不明ならUNKNOWNを維持する。

CLIは`saxo_ops_operator`で固定済みprocedureだけを実行する。table直接更新、任意procedure、任意SQL、市場データ変更は許可されない。DB4のbackup commandは内部で開始・完了procedureを使い、restore結果は`ops.record_restore_smoke`だけから記録する。

## 6. Migration運用

適用とchecksum検証:

```bash
python3 -m market_db.migrate apply
python3 -m market_db.migrate validate
```

同一番号・同一checksumはskipされる。同一番号・異なるchecksumはDDL前に停止する。checksum不一致時は適用済みfileを修正せず、Git差分と適用履歴を確認し、修正migrationを新番号で作る。

## 7. DB2 legacy import

入力検証と現在件数の確認:

```bash
python3 -m market_db.import_legacy verify
python3 -m market_db.import_legacy status
```

`verify`の期待値は69 file、781,808 row、160,403,659 bytes、error 0である。`status`の期待値はsource dataset 6、instrument 18、ingestion run/source file各69、raw market bar 636,629、reference 90,894、curated 1H 394,992、ETF total-return 54,285、quality event 5である。

初回または中断後の再実行:

```bash
python3 -m market_db.import_legacy import
```

処理はadvisory lockを取得し、1 file単位のtransactionで実行する。登録済み相対pathのhash/row countが一致すればskipし、異なれば停止する。DB2完了後の通常再実行では`imported_files=0`、`skipped_files=69`となる。CSVを変更して再実行したり、台帳を手動削除して取り込み直したりしない。

## 8. Research snapshot

状態確認:

```bash
python3 -m market_db.research_snapshot status
```

期待値はsnapshot 1件、cutoff `2024-06-28T23:59:59Z`、raw/curated最大時刻`2024-06-28T20:00:00Z`、total-return最大日`2024-06-28`、database default read-only `on`である。

作成処理は冪等であり、DB2完了後は既存snapshotを検証してskipする。

```bash
python3 -m market_db.research_snapshot create
```

内容manifestは`manifests/db2_research_snapshot_content.json`、dump manifestは`manifests/db2_research_snapshot_dump.json`、Git管理外のcustom-format dumpは`backups/postgres/saxo_research_v13_db2.dump`にある。dump hashと`pg_restore --list`はDB2で検証済みである。このDB2固定証跡はDB4の一般retention命名規則の対象外である。

## 9. DB3 calendar・watermark・派生足

初期化済み状態のread-only確認:

```bash
python3 -m market_db.session_calendar status
python3 -m market_db.incremental_update status
python3 -m market_db.inspect coverage
python3 -m market_db.inspect freshness
```

現行実測はcanonical 13 watermark、ETF 11銘柄のverified calendar、FX 2銘柄のprovisional calendarである。ETF coverageは既存履歴の欠損とcalendar外バーを分離した`WARN`、FX coverageはlive Saxo schedule照合まで`NOT_EVALUATED`となる。これはFAILを隠すものではなく、判定根拠の確度を状態として残すためである。

calendar再生成、watermark初期化、派生足再生成は冪等だが、日常確認ではなくmigration後・仕様変更後だけ実行する。

```bash
python3 -m market_db.session_calendar apply
python3 -m market_db.incremental_update initialize-watermarks
python3 -m market_db.derive_bars
```

4H/1Dは`is_complete=true AND quality_status=PASS`の1Hだけをcalendar内で集約する。Saxo raw 4Hを正本にせず、FX provisional行は`NOT_EVALUATED`のまま保持する。

## 10. DB3 live SIM更新

tokenは対話terminalのprocess環境、またはlocalhost operator UIのrequest/job memoryだけへ入れ、file、`.env`、shell引数、DB、manifest、Codex chatへ貼らない。次はCLI例であり、入力値は表示されない。

```bash
read -rs SAXO_ACCESS_TOKEN
export SAXO_ACCESS_TOKEN
python3 -m market_db.saxo_smoke_test
python3 -m market_db.incremental_update run
python3 -m market_db.incremental_update run
python3 -m market_db.validate --phase db3
unset SAXO_ACCESS_TOKEN
```

### 10.1 AI運用用local operator UI

AI側にtokenをchatやtool引数で渡さずlive gateを実行する場合は、localhost限定operator UIを使う。

```bash
.venv/bin/python -m market_db.operator_ui
```

`http://127.0.0.1:8765/`はOAuthとschedulerの管理に使う。review-first policyではpassword欄と汎用reconcile endpointを無効化し、`REVISION_REVIEW_REQUIRED_USE_EXPLICIT_APPLY`で拒否する。DataVersion warningは8766のRead APIで証跡を確認し、reviewer・note付きのapprovalを別操作で記録した後だけ、event IDとinstrumentを固定した`data_version_reconcile apply`を使う。任意command、任意shell文字列、未承認eventは受け付けない。

安全境界:

- bindは`127.0.0.1`固定。remote bind optionを持たない。
- tokenはPOST bodyから子process環境へ一度だけ渡し、URL、command argument、file、DB、log、cookie、`localStorage`、`sessionStorage`へ保存しない。
- requestはexact loopback Origin/Host、CSRF token、JSON、16 KiB上限を検査する。
- `shell=False`、stdin閉鎖、single active job、固定repository cwdを強制する。
- responseは`Cache-Control: no-store`、CSP、frame拒否を設定し、Bearer/JWT形式と入力tokenの出力をredactする。
- job完了後はtoken参照を破棄する。画面を閉じ、serverを`Ctrl-C`で停止する。

token入力前の画面表示、空入力拒否、`/health`、`IDLE` statusは秘密情報なしでAIが検査できる。token値そのものをAI chat、terminal output、screen captureへ表示しない。

### 10.2 OAuth・無人定期更新

定期運用では24時間tokenを使わずOAuth PKCEを使用する。Developer PortalのSIMアプリには`http://localhost/saxo/oauth/callback`（portなし）を登録し、AppKeyをoperator UIの起動環境へ設定する。実行時callbackは`http://localhost:8765/saxo/oauth/callback`であり、SaxoのPKCE仕様に従って登録済みdomain/pathへ任意portで戻す。AppKeyはOAuth client IDでありtokenではないが、Gitへ固定しない。

```bash
export SAXO_OAUTH_APP_KEY='<SIM application key>'
.venv/bin/python -m market_db.operator_ui
```

`http://127.0.0.1:8765/`の「Saxo OAuth接続」で初回loginを完了し、`AUTH_READY`を確認してから「定期更新を開始」を実行する。CLIの場合は次を使用する。

```bash
.venv/bin/python -m market_db.saxo_auth login --callback-port 8765
.venv/bin/python -m market_db.saxo_auth status --callback-port 8765
.venv/bin/python -m market_db.periodic_update schedule \
  --scope-profile all_except_usdjpy_provider_quarantine_20260727
.venv/bin/python -m market_db.periodic_update_service start --callback-port 8765 \
  --scope-profile all_except_usdjpy_provider_quarantine_20260727
.venv/bin/python -m market_db.periodic_update_service status
```

運用仕様:

- refresh credentialとPKCE verifierだけをmacOS Keychainへ保存する。
- access tokenはscheduler process memoryだけで保持する。
- OAuth reconcileは各normal/full-refetch step前にaccess tokenを取得し、full-refetch前は強制refreshする。20分access tokenを13系列の全reconcileへ固定しない。
- 一時scope `all_except_usdjpy_provider_quarantine_20260727`はEURUSDとETF 11系列だけを有効にし、USDJPYを`BLOCKED_PROVIDER_CONTENT_QUALITY`として除外する。正本は`specs/source_collection/periodic_scheduler_scope_v1.json`、実行値はservice／scheduler stateの`scheduler_scope`で照合する。
- XNYS regular slotは株式・REIT（SPY、IWM、EFA、EEM、VNQ）、債券・Credit（SHY、IEF、TLT、TIP、LQD）、Gold（GLD）をinstrument lane別transactionで取得する。あるinstrumentのblockerで同category内の他instrumentも停止しない。
- XNYS session終了45分後に`etf_daily_close`をETF11のinstrument lane別に実行する。C2専用日足はactual terminal 1Hを必須とし、短い内部欠落だけをmigration 0036の明示WARN付きoverlayで最大2本補完できる。3本以上、terminal欠落、lineage不一致は当該instrumentだけをBLOCKEDにし、他銘柄・serviceは継続する。raw/canonical/既存derivedを補完で上書きせず、日次close自体を別providerやquote feedから取得しない。
- `RETRY_EXHAUSTED:DATA_NOT_READY`は元slotの監査証跡であり、次のhourly/daily slotを抑止しない。週末・休場日は新barを要求せず、`/api/v1/c2/daily-close-status`の`NEXT_SESSION_WAIT_NON_BLOCKING`を確認する。
- FX hourly slotはSBFX 24x5 calendarでEURUSDだけを取得する。USDJPYは訂正版DataVersion確認、guard付きfull-refetch PASS、通常run連続2回PASSまでscheduleへ戻さない。
- USDJPYの隔離中監視は全履歴取得ではなく、専用の`usdjpy_version_watch`を使う。`status`はDB read-only、`probe`はSaxo Chartを`Count=1`で1回だけGETする。同じ既知quarantine DataVersionならrawを追加保存せず、新versionだけをisolated provider evidenceとして保存する。curated、raw DB、watermark、publicationは変更せず、full-refetchも開始しない。

```bash
.venv/bin/python -m market_db.usdjpy_version_watch status
.venv/bin/python -m market_db.usdjpy_version_watch probe \
  --auth-mode keychain --callback-port 8765
```

`NEW_PROVIDER_DATA_VERSION_REVIEW_REQUIRED`はfull-refetchの自動許可ではない。sanitized evidenceをreviewし、対象USDJPYだけのguarded full-refetch実行可否を別途承認する。
- ETFはDBのXNYS sessionで各完成可能1H終了15秒後に開始し、第1barは10:33 ETをdeadlineとする。
- EURUSDは毎UTC時03分に開始し、時10分をdeadlineとする。
- expected watermark未到達は`DATA_NOT_READY`とし、data quality FAILにしない。
- 401は強制refresh後1回再試行する。network、429、`DATA_NOT_READY`は最大4 attemptで打ち切る。
- DataVersion変更単独はterminal blockerではない。future policyでは`REVIEW_PENDING` eventと限定sample evidenceを保存し、schedulerはslotを警告付き`PASS`として終了して次laneへ進む。`RUNNING_DEGRADED`、category停止、service停止へ遷移しない。
- 新versionのChart JSONはimmutable revision evidenceとして保持するが、明示applyまではstaging、raw market revision、curated、watermark、4H/1Dへ書き込まない。accepted freshnessはwatermark基準、新provider evidence時刻は別fieldとして表示する。
- reviewとapplyは別操作である。旧`run` commandとOperator UIの汎用reconcileは使用しない。詳細は[`data_version_warning_review_policy_20260728.md`](data_version_warning_review_policy_20260728.md)を参照する。

```bash
.venv/bin/python -m market_db.data_version_reconcile review \
  --revision-event-id <event-id> --decision APPROVE_APPLY \
  --reviewer '<operator-id>' --note '<承認根拠>'
.venv/bin/python -m market_db.data_version_reconcile apply \
  --instrument-key eurusd --revision-event-id <event-id> \
  --confirm APPLY_RECONCILE --auth-mode keychain --callback-port 8765
```

- full-refetch PASS後は同じ明示scopeでschedulerを再開し、通常slotのwatermark gate PASSを確認する。canonical全体reconcileでprovider隔離中のUSDJPYを巻き込まない。
- `.runtime/periodic_update/`のstate/logは0600、Git管理外で、token値を含めない。
- `total_return.status=BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED`の間はtotal-return jobをscheduleしない。

FX gap分類:

```bash
.venv/bin/python -m market_db.fx_gap_report \
  --output-dir manifests/fx_gap_classification
```

`SBFX_24X5`のverified完成足slotとcurated 1Hをanti-joinし、raw revision、成功run範囲、quarantine eventを同一repeatable-read snapshotで照合する。JSON/CSVには全missing timestamp、cause、owner、必要証拠、blocking判定を残す。日曜17時以降New Yorkは月曜FX sessionの正規開始なので、曜日だけでweekend closureに分類しない。価格の補間、forward fill、別provider、反対sideからの生成は禁止する。

停止とcredential削除:

```bash
.venv/bin/python -m market_db.periodic_update_service stop
.venv/bin/python -m market_db.saxo_auth logout --callback-port 8765
```

`logout`はKeychainのrefresh credentialを削除する。DB、raw artifact、ingestion run、watermarkを削除しない。LaunchAgentは実credentialと3取引日のSLA受入が完了するまでinstallしない。

手動canonical runは13系列すべて、S6V5A定期runは固定6系列を単一transactionで処理する。Etfは最新完成実バーから20本、FxSpotは72本をoverlapし、境界を含む`Mode=From`結果を重複排除する。full-refetchの`Mode=UpTo`境界で値が異なる重複sampleが返る場合は、request順で最初に取得した完成側sampleを保持し、次の古いpage末尾の部分形成sampleで上書きしない。両raw JSONは変更せず保持する。raw JSONは`data/acquisition/runs/<run-id>/`へatomic保存するがGit管理外で、AccountKey、ClientKey、TradableOn等は除去する。smoke response bodyは保存しない。

正常時はraw revision、curated latest、watermark、4H/1D、run statusが同時にcommitされる。品質失敗時はこれらをrollbackし、取得済みraw artifact、sanitized error code、OPEN quality eventだけを残す。直後の2回目runは、新しい完成足がなければ新規行0、形成中sampleの変化は最大1行/銘柄までを許容する。

主要な停止code:

- `BLOCKED_LIVE_SIM_TOKEN`: tokenがprocessにない。
- `BLOCKED_TOKEN_EXPIRED`: token失効。新しいsession-only tokenで再実行する。
- `BLOCKED_PERMISSION_OR_NETWORK_REPUTATION`: permissionまたはSaxo側network reputation制約を確認する。
- `BLOCKED_RATE_LIMIT`: 有限retry後も429。runを連打せずreset後に再実行する。
- `FAILED_NETWORK`: timeout・接続切断等がGET限定の1/2/4秒・最大4attempt retry後も継続した。Saxo疎通を確認し、同じsession-only tokenで`reconcile`を再実行する。
- `BLOCKED_INSTRUMENT_DRIFT`: UIC、AssetType、Symbol、Currency不一致。自動代替しない。
- `DATA_VERSION_REVISION_REVIEW_PENDING`: DataVersion変化を検知し、証跡を保存した非停止warning。current curated、水位、派生足は不変。Read APIのrevision eventをreviewする。
- `BLOCKED_REVISION_SCOPE_UNRESOLVED_FULL_REFETCH_REQUIRED`: 96→384→1200本で訂正境界を限定できない。対象instrumentだけをguard付きfull refetchへ送る。

DataVersion block後の対象1銘柄だけ、guard付きfull refetchを使える。`data_status=STALE_DATA_VERSION`でない場合、procedureは削除前に拒否する。取得履歴が既存最古時刻まで届かなければtransactionをrollbackする。

```bash
python3 -m market_db.inspect freshness --format json
python3 -m market_db.data_version_reconcile review --revision-event-id <event-id> \
  --decision KEEP_CURRENT --reviewer '<operator-id>' --note '<判断根拠>'
python3 -m market_db.incremental_update run
python3 -m market_db.incremental_update reconcile
python3 -m market_db.incremental_update status
```

通常の第一選択はreconcileではなく、警告証跡のreviewである。DataVersion差を無視せず記録する一方、review承認前にcuratedを変更しない。`apply`は`APPROVE_APPLY`済みevent、instrument、old/new version、accepted watermarkが一致する場合だけ使う。手動DELETE、watermarkの直接更新、自動full-refetchは禁止する。
`status`はread-only transactionで通常runの状態別件数とwatermark状態別件数だけを返し、DBを変更しない。

FxSpotのfull-refetchでは、過去のHigh/Low極値だけに交差があるrowを最大10件かつ全unique観測の0.01%以下に限って自動隔離する。これは値の修正ではない。raw JSONとSHA-256を保持し、rowをraw revision・curated・派生足から除外し、成功runの`rejected_rows`と`db3_fx_crossed_extrema_quarantine`の解決済みWARNへ記録する。最新sample、Open/Close交差、欠損、OHLC違反、件数・比率超過、重複矛盾は全runをFAILする。operatorはBid/Askのswap、補間、clamp、手動DELETEで回避してはならない。

隔離結果は次で確認する。

```bash
python3 -m market_db.inspect runs --format json
python3 -m market_db.inspect quality --format json
```

run JSONの`rejected_rows`、quality eventのtimestamp・元Bid/Ask・raw artifact相対path・payload SHA-256を照合する。WARNが解決済みのためopen event viewに出ない場合は、対象runのmanifestとDBの`ops.ingestion_run`を基準にし、任意SQLを一般readerへ許可しない。

複数銘柄で`DataVersion`が変化しても、各instrumentのwarning eventとevidence sampleを独立して追記し、schedulerは全laneを継続する。applyが必要と判断されたeventだけを1銘柄ずつ明示承認し、Keychainのrotating refresh credentialからaccess tokenをprocess memoryへ取得する。各runは独立したmanifestを保持し、tokenをfileやDBへ保存しない。

`manifests/db3_implementation_manifest.json`の派生足件数はoffline実装時点の固定証跡であり、live DBの恒久的な件数制約ではない。validatorはmanifestに非空・quality FAIL 0のbaselineが記録されていることを検査し、現在DBについては別途、派生足が非空・quality FAIL 0・canonical watermark整合であることを検査する。正常な増分取得やfull-refetchで行数が変化しても、固定baselineとの不一致だけを理由にoffline gateをFAILにしない。

### 10.3 FX研究候補のオンボーディング

AUDUSD、USDCAD、USDCHFはcanonical 13およびUSDJPY復旧とは分離した候補datasetで扱う。migration `0026`適用後も初期状態は`CANDIDATE`で、candidate schedulerは起動しない。

状態確認:

```bash
.venv/bin/python -m market_db.fx_candidate_onboarding status
```

全履歴取得は1ペアずつ行う。既存EURUSD/ETF slotの直前を避け、正規schedulerがRUNNING、AUTH_READY、orders/prechecks=0であることを先に確認する。

```bash
.venv/bin/python -m market_db.periodic_update_service status
.venv/bin/python -m market_db.fx_candidate_onboarding onboard \
  --instrument-key audusd --auth-mode keychain --callback-port 8765
```

`onboard`はSaxo GETだけを用い、UpTo全pageをimmutable rawへ保存してから、単一DataVersion、page境界、UTC順序、重複、null、非正値、Bid/Ask両side OHLC、交差、coverage/gap、freshness、open quality eventを検査する。PASSしたペアだけ`STAGING`となる。認証・HTTP・timeoutはinterface/operationalであり、content quality FAILへ読み替えない。

通常更新の受入は一度のコマンドで1回だけ行う。同時刻の再実行は合格回数に数えず、`latest_complete_time_utc`が前回受入時刻より前進した場合だけ連続PASSを加算する。

```bash
.venv/bin/python -m market_db.fx_candidate_onboarding accept \
  --instrument-key audusd --auth-mode keychain --callback-port 8765
```

1回目は`STAGING / consecutive_normal_passes=1`、別の完成1Hが公開された後の2回目で`PUBLISHED / 2`となる。未前進は`DATA_NOT_READY_CANDIDATE_WATERMARK_NOT_ADVANCED`として保留し、quality FAILにしない。1ペアの失敗はそのinstrumentだけを`BLOCKED`にし、EURUSD、ETF11、他候補、USDJPYへ波及させない。

全3ペアが`PUBLISHED / 2`、consumer availability
`AVAILABLE_WITH_WARNINGS`、承認済みresearch policy、quality WARN、coverage WARN、
freshness PASS、blockerなしの場合だけ、dormant profile
`all_except_usdjpy_with_fx_research_candidates_20260727`を開始候補にできる。
ここでWARNは一般的な品質gate緩和ではなく、ユーザー承認済みの限定契約がDBと
Read APIへ一致している場合だけ認める。候補は`fx_research_candidates_hourly`の
単一instrument slot（UTC時06分due、15分deadline）で分離される。既存active
profileを自動で切り替えず、USDJPYを含めない。

現在の研究契約では、AUDUSDの既知extrema 14件をrawのまま保持し、curatedから
無補間で除外する。14件のfingerprint、件数、期間、High/Low fieldのいずれかが
変わった場合は自動で再reviewへ戻す。USDCAD/USDCHFのeffective coverage startは
`2010-06-18T00:00:00Z`で、provider表示の2002年からの空白を補間しない。AUDUSDの
effective startは実取得で確認した`2003-05-12T00:00:00Z`である。

`SAXO_OAUTH_APP_KEY`が実行processにない場合は`AUTH_CONFIG_MISSING`でDB変更前に停止する。値をchat・logへ出さず、必要ならAppKeyを保持しているterminalから次のprocess環境へ設定する。refresh credentialやaccess tokenはlaunchdへ設定しない。

Operator UIの「定期更新を開始」は、3候補の永続化済みactivation gateがPASSなら
candidate profileを選び、未達なら従来のUSDJPY除外profileを選ぶ。画面操作だけで
gateを上書きできない。CLIでcandidate profileを明示しても、service startとdaemon
startの両方が同じgateを再検証する。

```bash
launchctl setenv SAXO_OAUTH_APP_KEY "$SAXO_OAUTH_APP_KEY"
```

## 11. DB4 Read API・backup・export

Read APIは一般DB管理画面ではなく、固定された参照endpointだけをlocalhostへ公開する。
外部プロジェクトからの接続契約、response schema、品質確認、期間分割、total-return、エラー処理、Parquetの使い分けは`docs/read_api_interface.md`を正本とする。

```bash
.venv/bin/python -m market_db.read_api_service start
.venv/bin/python -m market_db.read_api_service status --format json
.venv/bin/python -m market_db.read_api_preflight --format json
curl --fail 'http://127.0.0.1:8766/api/v1/operations/inventory?limit=25'
curl --fail 'http://127.0.0.1:8766/api/v1/bars?instrument_key=iwm&layer=1h&start=2026-07-15T00:00:00Z&end=2026-07-17T00:00:00Z&limit=100'
```

barはinstrument、layer、UTC start/endが必須で最大10,000行である。APIは`saxo_app_reader`、read-only transaction、最大5接続、30秒statement timeoutを使う。write method、任意relation、任意SQL、token入力を追加しない。

外部consumerへデータ取得を許可する前の標準順序は、`DB container healthy -> Read API start/status -> non-data preflight PASS artifact保存 -> model/spec/source query freeze -> explicit one-time authorization -> atomic acquisition/evaluation`である。preflight自体は`/`、`/health`、parameterなしのbars/total-returnだけを呼び、市場・metadata rowを取得しない。preflight PASSをcoverage、freshness、quality、戦略性能のPASSへ読み替えない。

serviceのstateとlogはGit管理外の`.runtime/read_api/state.json`、`.runtime/read_api/read_api.log`に0600で保存し、directoryは0700とする。startはPostgreSQL unhealthyまたはport競合時にfail-closed、healthyな同一serviceへは冪等PASSとなる。stopはPID、開始fingerprint、command hash、cwd、portが一致するrepo管理processだけへSIGTERMを送る。broadな`pkill`や未検証PIDへの`kill`を使わない。Read API停止後もPostgreSQL、8765 operator UI、volume、data、migrationは停止・変更しない。

外部projectの正式preflightはatomic endpointを使う。

```bash
curl --fail --get 'http://127.0.0.1:8766/api/v1/series-status' \
  --data-urlencode 'instrument_key=spy' \
  --data-urlencode 'layer=1h' \
  --data-urlencode 'price_basis=native_ohlc'
```

このresponseのidentity、coverage、freshness、quality、watermark、latest runは同じ`REPEATABLE READ / READ ONLY` transaction snapshotで取得される。`eligibility_status`が`BLOCKED`の場合はcurrent/live利用へ進まない。`ELIGIBLE_WITH_WARNINGS`の場合も`eligibility_warnings`を利用側で明示的に扱う。複数のoperations responseをclient側でjoinした結果を正式preflightとして保存しない。

固定研究snapshotの1H OHLCはsnapshot-bound endpointで取得する。

```bash
curl --fail --get 'http://127.0.0.1:8766/api/v1/snapshots/1/bars' \
  --data-urlencode 'instrument_key=spy' \
  --data-urlencode 'layer=1h' \
  --data-urlencode 'price_basis=native_ohlc' \
  --data-urlencode 'start=2024-06-28T13:00:00Z' \
  --data-urlencode 'end=2024-06-29T00:00:00Z' \
  --data-urlencode 'limit=100'
```

このendpointはcurrent poolを使わず、`saxo_research_v13`へ`v13_research_reader`で接続する専用poolを使う。responseの`integrity.status=PASS`、`truncated=false`を確認し、`requested_snapshot_id`、`resolved_snapshot_id`、`snapshot_sha256`、`row_count`、`ordered_content_sha256`をconsumer runへ保存する。4H/1Dは`SNAPSHOT_LAYER_NOT_AVAILABLE`、未知IDは`SNAPSHOT_NOT_FOUND`、manifestまたはDB内容不一致は503で停止する。これらをcurrent `/api/v1/bars`へfallbackしない。

DMI2B不変性probeは、current `saxo_market`のsession-local temporary tableへcommitした更新を挟み、同じsnapshot queryのrow count、全row、ordered content SHA、snapshot SHAが同一であることを検証する。共有tableやsnapshot DBは変更しない。

stable total-returnは`GET /api/v1/total-return`を使用する。`catalog.series_instrument_mapping`のapproved mappingだけを参照し、symbol文字列の暗黙joinは行わない。

```bash
curl --fail --get 'http://127.0.0.1:8766/api/v1/total-return' \
  --data-urlencode 'instrument_key=iwm' \
  --data-urlencode 'start=2024-01-01T00:00:00Z' \
  --data-urlencode 'end=2024-07-01T00:00:00Z' \
  --data-urlencode 'eligibility=eligible'
```

`eligibility=eligible`はPASS行だけ、`stored_complete`はWARN/NOT_EVALUATEDを含む可能性がある。複数source dataset候補で`source_dataset_id`を省略した場合は`SOURCE_DATASET_REQUIRED`となり、native OHLC endpointへfallbackしない。

DMI4 cursorの受入確認とconsumer fixtureは次で実行する。fixtureは署名secretやSaxo credentialを含まず、Read APIを再起動しても安全に再実行できる。

```bash
PYTHONPATH=. .venv/bin/pytest -q \
  tests/test_dmi4_fixture_contract.py \
  tests/test_dmi4_pagination.py \
  tests/test_dmi4_contract.py
```

複数pageの連結結果はdirect queryと一致し、missing、duplicate、order reversalが0であることを確認する。`CURSOR_INVALID`、`CURSOR_QUERY_MISMATCH`、`CURSOR_EXPIRED`はデータ取得成功として扱わず、queryを固定して最初のpageから再開する。

### 11.1 データ管理Web UI

Read APIを起動した同じprocessで、データ管理UIも利用できる。

```bash
.venv/bin/python -m market_db.read_api_service start
```

ブラウザで <http://127.0.0.1:8766/ui/overview> を開く。`8765`の取得・Reconcile用operator UIとは別の画面であり、Saxo tokenは不要で、入力欄も存在しない。停止は`.venv/bin/python -m market_db.read_api_service stop`を使う。

- `データ概要`: 有効dataset、正式13銘柄、1H/4H/1D件数、現在の品質・鮮度、最新run
- `データ在庫`: 正式・派生・total return・raw/archive・referenceを別系列として検索し、50件ずつ確認
- `系列チャート`: 保存済み1H/4H/1Dをローソク足、ETF total returnを折れ線で確認
- `品質・鮮度`: current guardrailと過去のOPEN eventを分離して確認
- `取込・由来`: run、error code、相対manifest path、source file lineageを確認
- `バックアップ`: backup/restore smokeとrelation別sizeを確認

チャートの既定は`eligible`だけである。`管理確認モード`はcompleteだが`WARN/NOT_EVALUATED`の保存済みbarを監査表示し、固定警告を出す。これは研究利用可能への昇格ではない。画面から取得、Reconcile、修正、削除、backup、restore、注文を実行できない。

UI確認用の固定endpoint:

```bash
curl --fail http://127.0.0.1:8766/api/v1/ui/overview
curl --fail 'http://127.0.0.1:8766/api/v1/ui/series?canonical_only=true&limit=50&offset=0'
```

品質画面の`全scope blocker`はraw/archiveを含むevent単体の件数、`canonical blocker`はcurated 1Hへscopeが一致する件数である。CURRENT raw eventを消したりHISTORICALへ変更してcanonicalをPASSに見せてはならない。

DMI1 legacy reviewの固定planはread-onlyで再検証できる。

```bash
.venv/bin/python -m market_db.operate reconcile-dmi1-legacy \
  --operator operator-label
```

通常は`PLAN_VALID`と`database_writes=0`を確認するだけでよい。`--apply`は初回または根拠付き再review時だけ使用する。新規eventはrule policyに従ってscope/applicabilityが同一transactionで追記され、未知ruleはUNKNOWNとしてblockする。`db3_atomic_run_gate`は同一instrumentのfull-refetch PASSまたは13系列normal PASSが成立した場合だけ、元eventを変更せずHISTORICAL reviewを自動追記する。

assetは自己ホストし、TradingView Lightweight Charts 5.2.0とApache-2.0 licenseを同梱する。DB4の旧実装manifestは変更せず、DMUI4 manifestが現行artifactと旧DB4 manifestの親SHA-256を検証する。

3 DBのbackupとmarket DBのrestore smoke:

```bash
.venv/bin/python -m market_db.backup create saxo_market --restore-smoke
.venv/bin/python -m market_db.backup create saxo_research_v13
.venv/bin/python -m market_db.backup create saxo_forward_v13
.venv/bin/python -m market_db.inspect backups
```

dumpは`backups/postgres/<database>_<UTC>.dump`へatomicに作成し、対応する`.manifest.json`、SHA-256、size、`pg_restore --list`が揃った場合だけPASSとなる。restore smokeは`saxo_db4_restore_<random>`だけを作成し、元DBと主要件数・主キー重複・snapshot cutoffを比較後、必ず削除する。手動の任意DB名へのrestoreや既存DBへの上書きは禁止する。

retentionはDBごとにdaily 7・weekly 4を保持する。必ずdry-runを先に確認し、想定外の候補が1件でもあればapplyしない。

```bash
.venv/bin/python -m market_db.backup retention
.venv/bin/python -m market_db.backup retention --apply
```

ParquetはGit管理外の`exports/parquet/`だけへ作成する。

```bash
.venv/bin/python -m market_db.export_parquet \
  --instrument-key iwm --layer 1h \
  --start 2026-07-15T00:00:00Z --end 2026-07-17T00:00:00Z \
  --output iwm_sample.parquet
```

最大100,000行で、SHA-256とDuckDB read-back件数が一致しなければFAILする。PostgreSQLが正本であり、ParquetをDBへ逆importしない。

## 12. 総合検証

```bash
SAXO_DB_INTEGRATION=1 .venv/bin/python -m pytest
.venv/bin/python -m market_db.validate --phase db4
```

統合testをskipした結果はPASSにしない。DB4 validatorはDB1〜DB3を回帰し、migration 0013/0014/0015、reader権限、3 DB backup、market restore smoke、一時DB削除、Parquet再読込、DB4親manifest、DMUI4拡張manifest、DMI1拡張manifestを検証する。DMI1の契約実装PASSと旧event reconciliationのBLOCKEDは別状態として報告する。

## 13. Secret rotation

自動rotation commandは提供しない。rotationが必要な場合はservice影響を評価し、対象roleごとに次を実施する。

1. maintenance windowを確保し、対象roleを利用するprocessを停止する。
2. 新passwordを権限`0600`の一時fileとして生成する。値をshell historyへ書かない。
3. `postgres` emergency接続でparameter bindingまたは安全な対話入力を用い、対象roleのpasswordを変更する。
4. 対応する`.secrets/<role>_password`をatomicに置換し、modeを再確認する。
5. 対象roleだけで接続検証する。

bootstrap用`postgres_password`のfileだけを先に変更しても既存cluster passwordは変わらない。fileとdatabase roleを必ず同じmaintenance作業で同期する。

## 14. 障害対応

### Docker daemon停止

`docker info`を確認し、Docker Desktopを起動してから再試行する。daemonなしでruntime gateをPASS扱いにしない。

### Port 54329競合

`lsof -nP -iTCP:54329 -sTCP:LISTEN`でprocessを確認する。勝手にprocessを停止したり、仕様のportを変更したりしない。

### unhealthy

```bash
docker compose -p saxo-market-data ps
docker compose -p saxo-market-data logs --tail 100 postgres
```

出力を共有する前にcredentialや個人情報を確認する。volume削除で復旧しない。

### Read API operational readiness BLOCKED

```bash
.venv/bin/python -m market_db.read_api_service status --format json
lsof -nP -iTCP:8766 -sTCP:LISTEN
```

- `BLOCKED_READ_API_NOT_RUNNING`: PostgreSQL healthyを確認してservice `start`を実行する。preflight自身は自動起動しない。
- `BLOCKED_PORT_CONFLICT`: listenerの所有processを確認し、勝手に停止・port変更せず所有者へ確認する。
- `BLOCKED_DATABASE_UNHEALTHY`: PostgreSQLの`ps`とlogを確認し、volume削除で復旧しない。
- `BLOCKED_READ_ONLY_BOUNDARY`: role、read-only、timeoutの設定差分を調査し、データ取得を止める。
- `BLOCKED_API_CONTRACT_MISMATCH`: API/OpenAPI revisionと必須routeを照合し、旧APIへfallbackしない。
- `FAILED_PREFLIGHT_INTERNAL`: preflight実装またはhost commandの異常として扱い、PASSへ読み替えない。

stale stateは別processをsignalせず`BLOCKED_STALE_PID`として閉じる。state fileを手動で別PIDへ書き換えない。必要な調査後もprocess identityが一致しない場合は、所有者確認のうえruntime stateだけを除去する。LaunchAgentは自動installせず、常駐化はrepo-local lifecycle受入後のoperator判断とする。

### Disk不足

`docker system df`とhost空き容量を確認する。named volumeは削除せず、不要な別projectの資源整理について所有者確認を行う。

### Import検証失敗

`market_db.import_legacy verify`またはimportが失敗した場合は処理を停止し、missing、size、SHA-256、immutable registration mismatchの区分を確認する。CSV、inventory、適用済みmigrationをその場で書き換えない。

### Snapshot検証失敗

research DB、content manifest、dump manifest、dump本体のいずれかが不一致ならDB3へ進めない。research DBのtruncate/drop、dump削除、snapshot row手動更新は行わず、validator出力とGit差分を保存して原因を特定する。

### DB3更新中断・品質gate失敗

`market_db.inspect runs`と`market_db.inspect quality`でrun ID、last success step、sanitized codeを確認する。stagingは成功・rollback後に0件でなければ新規runを開始しない。raw artifactは削除せず、token/account情報がないことを確認する。curated、derived、watermarkを個別に手動修正しない。

### 定期更新・OAuth BLOCKED

```bash
.venv/bin/python -m market_db.saxo_auth status --callback-port 8765
.venv/bin/python -m market_db.periodic_update_service status
.venv/bin/python -m market_db.periodic_update status
```

- `AUTH_CONFIG_MISSING`: operator UI/serviceを同じ`SAXO_OAUTH_APP_KEY`で再起動する。
- `AUTH_LOGIN_REQUIRED`: Web UIから人間がSaxoへ再loginする。24時間tokenの自動採取へfallbackしない。
- `AUTH_KEYCHAIN_*`: Keychainのlock、access control、credential破損を調査し、token値をterminalへ出さない。
- `BLOCKED_STALE_PID`: PID、command、cwd、start fingerprintを照合し、別processをsignalしない。
- process開始時刻はlocale表示文字列を直接比較せず、`LC_ALL=C`で取得した正規形を使う。旧日本語／英語stateはPID、cwd、command SHA-256、module・portが全一致する場合だけservice managerに移行させる。
- `DATA_NOT_READY`: deadline内はschedulerが再試行する。deadline超過後は`sla_status=MISS`として扱い、quality FAILへ変更しない。
- `BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED`: total-return current providerをfreezeするまで既存development snapshotを昇格しない。

ETF freshnessは`catalog.session_interval`内で完全に閉じる1Hだけを期待し、通常close日の15:30 ET開始barを要求しない。EURUSDは`SBFX_24X5`のverified sessionと16:59〜17:04 New York maintenanceを使い、weekend／maintenance中の未生成barをquality FAILにしない。

coverage／freshnessのRead API計算はmigration `0022`／`0023`で全履歴のslot展開を回避している。`series-status`が30秒でtimeoutする場合はdata quality FAILではなくinterface／operational blockとし、view語義、migration checksum、calendar件数、DB負荷を確認する。

### DB4 backup・restore失敗

`ops.v_backup_status`と対応manifestを確認し、FAILED/RUNNINGを手動でPASSへ変更しない。`.partial`、SHA-256不一致、`pg_restore --list`失敗は使用不可とする。一時restore DBが残った場合は、今回の`saxo_db4_restore_`名と作成runを確認してから削除し、既存3 DBをdropしない。

## 15. プロジェクト境界と後続作業

- DB2: CSV import、inventory/lineage登録、research snapshot。PASS。
- DB3: Saxo増分更新、4H/1D派生、session calendar、freshness監視。PASS。
- DB4: read API、実backup/restore、retention、Parquet、runbook運用ゲート。PASS。
- DMUI4: データ在庫、期間、品質、lineage、OHLC/total-return表示。PASS。
- DMI0/DMI1A: consumer fail-closed、安定identity、quality review contract。PASS。
- DMI1B: 旧eventの根拠付きreview。PASS。
- DMI2A/DMI2B/DMI3: atomic status、snapshot-bound read、stable total-return。PASS。
- DMI4: cursor・consumer contract kit。PASS。
- DMI5: Read API lifecycle・non-data operational preflight。PASS。
- 今後の作業はデータ取得、品質・鮮度、API契約、backup、運用性の改善に限定する。
- 旧計画のRT0以降にあるstrategy rule、cost、PnL、WFO、Holdout、portfolioは履歴資料として残すが、別の戦略プロジェクトで実施する。

DB2 snapshot dumpは固定証跡として保持し、DB4 retentionで削除しない。DB1〜DB4の完了はデータ基盤の品質証明であり、戦略優位性や収益性の証明ではない。
