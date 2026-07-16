# Saxo DB データ管理・運用ランブック

更新日: 2026-07-17 JST
対象: Phase DB1 infrastructure / DB2 legacy import / DB3 incremental market data
状態: **DB1・DB2 PASS、DB3 OFFLINE PASS / LIVE SIM TOKEN待ち、DB4 LOCKED**

## 1. 安全境界

- すべてrepository rootから実行する。Pythonコードはhost固有の絶対pathを前提にしない。
- passwordは`.secrets/`の権限`0600`のfileだけに置き、terminal、log、manifestへ表示しない。
- Saxo token、AccountKey、ClientKey、口座識別子は本プロジェクトへ保存しない。
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
```

通常restart:

```bash
docker compose -p saxo-market-data restart postgres
docker compose -p saxo-market-data ps
```

停止（volumeは保持される）:

```bash
docker compose -p saxo-market-data stop postgres
```

`ps`ではserviceが`running (healthy)`で、host側portは`127.0.0.1:54329`だけにbindされる。restart後はmigration履歴とschemaが保持される。

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

DB2完了時点では、market inventoryは実データを返し、lineageは69 source fileを返す。`quality --fail-on-alert`は既知のERROR/OPEN event 5件によりexit 2となるのが正常である。これらはraw 240分FX 2系列とdaily ETF 3系列の既知品質FAILであり、根拠なくresolveまたはraw修正しない。

## 5. Qualityとbackup状態の限定更新

quality noteは引数へ書かず、promptまたはstdinから入力する。

```bash
python3 -m market_db.operate acknowledge-quality 123 --operator operator-label
python3 -m market_db.operate resolve-quality 123 --operator operator-label
python3 -m market_db.operate start-backup saxo_market backups/example.dump
python3 -m market_db.operate finish-backup 1 FAILED --error-code MANUAL_CHECK
```

CLIは`saxo_ops_operator`で固定済み4 procedureだけを実行する。table直接更新、任意procedure、任意SQL、市場データ変更は許可されない。DB2 snapshot dumpは`ops.research_snapshot`と外部manifestで検証し、一般backup台帳・restore運用はDB4までLOCKEDである。

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

内容manifestは`manifests/db2_research_snapshot_content.json`、dump manifestは`manifests/db2_research_snapshot_dump.json`、Git管理外のcustom-format dumpは`backups/postgres/saxo_research_v13_db2.dump`にある。dump hashと`pg_restore --list`はDB2で検証済みである。別名DBへのrestore smoke test、一般backup、retentionはDB4まで実行しない。

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

tokenは対話terminalのprocess環境だけへ入れ、file、`.env`、shell引数、DB、manifest、Codex chatへ貼らない。次は例であり、入力値は表示されない。

```bash
read -rs SAXO_ACCESS_TOKEN
export SAXO_ACCESS_TOKEN
python3 -m market_db.saxo_smoke_test
python3 -m market_db.incremental_update run
python3 -m market_db.incremental_update run
python3 -m market_db.validate --phase db3
unset SAXO_ACCESS_TOKEN
```

通常runはcanonical 13すべてを単一transactionで処理する。Etfは最新完成実バーから20本、FxSpotは72本をoverlapし、境界を含む`Mode=From`結果を重複排除する。raw JSONは`data/acquisition/runs/<run-id>/`へatomic保存するがGit管理外で、AccountKey、ClientKey、TradableOn等は除去する。smoke response bodyは保存しない。

正常時はraw revision、curated latest、watermark、4H/1D、run statusが同時にcommitされる。品質失敗時はこれらをrollbackし、取得済みraw artifact、sanitized error code、OPEN quality eventだけを残す。直後の2回目runは、新しい完成足がなければ新規行0、形成中sampleの変化は最大1行/銘柄までを許容する。

主要な停止code:

- `BLOCKED_LIVE_SIM_TOKEN`: tokenがprocessにない。
- `BLOCKED_TOKEN_EXPIRED`: token失効。新しいsession-only tokenで再実行する。
- `BLOCKED_PERMISSION_OR_NETWORK_REPUTATION`: permissionまたはSaxo側network reputation制約を確認する。
- `BLOCKED_RATE_LIMIT`: 有限retry後も429。runを連打せずreset後に再実行する。
- `BLOCKED_INSTRUMENT_DRIFT`: UIC、AssetType、Symbol、Currency不一致。自動代替しない。
- `BLOCKED_FULL_REFETCH_REQUIRED`: DataVersion変化。通常runを続けず対象銘柄を確認する。

DataVersion block後の対象1銘柄だけ、guard付きfull refetchを使える。`data_status=STALE_DATA_VERSION`でない場合、procedureは削除前に拒否する。取得履歴が既存最古時刻まで届かなければtransactionをrollbackする。

```bash
python3 -m market_db.inspect freshness --format json
python3 -m market_db.incremental_update full-refetch --instrument-key spy
python3 -m market_db.incremental_update run
```

`full-refetch`は`Mode=UpTo`で全ページを取得し、old raw revisionを保持したままcuratedを再構築する。手動DELETE、watermarkの直接更新、DataVersion差の無視は禁止する。

## 11. DB2総合検証

```bash
SAXO_DB_INTEGRATION=1 python3 -m pytest
python3 -m market_db.validate --phase db2
```

統合testをskipした結果はDB2 PASSにしない。validatorは69 CSVのsize/hash、migration checksum、3 database、UTC、health、market実件数、lineage、quality、research cutoff/read-only、content/dump hash、`pg_restore --list`を確認する。DB2完了時の基準は`39 passed`で、DB3追加後の全suiteは`54 passed`である。

## 12. Secret rotation

自動rotation commandは提供しない。rotationが必要な場合はservice影響を評価し、対象roleごとに次を実施する。

1. maintenance windowを確保し、対象roleを利用するprocessを停止する。
2. 新passwordを権限`0600`の一時fileとして生成する。値をshell historyへ書かない。
3. `postgres` emergency接続でparameter bindingまたは安全な対話入力を用い、対象roleのpasswordを変更する。
4. 対応する`.secrets/<role>_password`をatomicに置換し、modeを再確認する。
5. 対象roleだけで接続検証する。

bootstrap用`postgres_password`のfileだけを先に変更しても既存cluster passwordは変わらない。fileとdatabase roleを必ず同じmaintenance作業で同期する。

## 13. 障害対応

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

### Disk不足

`docker system df`とhost空き容量を確認する。named volumeは削除せず、不要な別projectの資源整理について所有者確認を行う。

### Import検証失敗

`market_db.import_legacy verify`またはimportが失敗した場合は処理を停止し、missing、size、SHA-256、immutable registration mismatchの区分を確認する。CSV、inventory、適用済みmigrationをその場で書き換えない。

### Snapshot検証失敗

research DB、content manifest、dump manifest、dump本体のいずれかが不一致ならDB3へ進めない。research DBのtruncate/drop、dump削除、snapshot row手動更新は行わず、validator出力とGit差分を保存して原因を特定する。

### DB3更新中断・品質gate失敗

`market_db.inspect runs`と`market_db.inspect quality`でrun ID、last success step、sanitized codeを確認する。stagingは成功・rollback後に0件でなければ新規runを開始しない。raw artifactは削除せず、token/account情報がないことを確認する。curated、derived、watermarkを個別に手動修正しない。

## 14. 後続PhaseのLOCK

- DB2: CSV import、inventory/lineage登録、research snapshot。PASS。
- DB3: Saxo増分更新、4H/1D派生、session calendar、freshness監視。OFFLINE PASS / LIVE SIM TOKEN待ち。
- DB4: read API、実backup/restore、retention、runbook運用ゲート。DB3 PASSまでLOCKED。
- RT0: strategy PnL、WFO、Holdout、portfolio。DB4 PASSまでLOCKED。

DB2 snapshot dumpはDB2証跡として作成済みだが、汎用backup/restore機能の完成を意味しない。Saxo API増分更新はDB3、read API・一般backup/restore・retentionはDB4、戦略研究はDB4 PASS後まで、それぞれの範囲を越えて実行しない。
