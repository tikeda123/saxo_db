# Saxo DB データ管理・運用ランブック

更新日: 2026-07-16 JST
対象: Phase DB1 infrastructure
状態: DB1手順は実装対象、import・増分更新・backup/restore・API運用はLOCKED

## 1. 安全境界

- すべてrepository rootから実行する。Pythonコードはhost固有の絶対pathを前提にしない。
- passwordは`.secrets/`の権限`0600`のfileだけに置き、terminal、log、manifestへ表示しない。
- Saxo token、AccountKey、ClientKey、口座識別子は本プロジェクトへ保存しない。
- Phase DB1ではCSVをdatabaseへ投入しない。市場データtableが0件であることが正常である。
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

exit codeは正常`0`、接続・設定・query失敗`1`、`--fail-on-alert`で警告条件を検出した場合`2`。calendarや期待更新間隔が未登録ならcoverage/freshnessは`NOT_EVALUATED`となり、根拠なしにPASSへしない。`saxo_forward_v13`のinspectは評価ゲートまで拒否される。

## 5. Qualityとbackup状態の限定更新

quality noteは引数へ書かず、promptまたはstdinから入力する。

```bash
python3 -m market_db.operate acknowledge-quality 123 --operator operator-label
python3 -m market_db.operate resolve-quality 123 --operator operator-label
python3 -m market_db.operate start-backup saxo_market backups/example.dump
python3 -m market_db.operate finish-backup 1 FAILED --error-code MANUAL_CHECK
```

CLIは`saxo_ops_operator`で固定済み4 procedureだけを実行する。table直接更新、任意procedure、任意SQL、市場データ変更は許可されない。DB1のbackup procedure検証はrollbackするfixtureだけで行い、実backupは作らない。

## 6. Migration運用

適用とchecksum検証:

```bash
python3 -m market_db.migrate apply
python3 -m market_db.migrate validate
```

同一番号・同一checksumはskipされる。同一番号・異なるchecksumはDDL前に停止する。checksum不一致時は適用済みfileを修正せず、Git差分と適用履歴を確認し、修正migrationを新番号で作る。

## 7. DB1総合検証

```bash
SAXO_DB_INTEGRATION=1 python3 -m pytest
python3 -m market_db.validate --phase db1
```

統合testをskipした結果はDB1 PASSにしない。validatorは69 CSVのsize/hash、migration checksum、3 database、UTC、health、市場table 0件を確認する。

## 8. Secret rotation

DB1では自動rotation commandを提供しない。rotationが必要な場合はservice影響を評価し、対象roleごとに次を実施する。

1. maintenance windowを確保し、対象roleを利用するprocessを停止する。
2. 新passwordを権限`0600`の一時fileとして生成する。値をshell historyへ書かない。
3. `postgres` emergency接続でparameter bindingまたは安全な対話入力を用い、対象roleのpasswordを変更する。
4. 対応する`.secrets/<role>_password`をatomicに置換し、modeを再確認する。
5. 対象roleだけで接続検証する。

bootstrap用`postgres_password`のfileだけを先に変更しても既存cluster passwordは変わらない。fileとdatabase roleを必ず同じmaintenance作業で同期する。

## 9. 障害対応

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

## 10. 後続PhaseのLOCK

- DB2: CSV import、inventory/lineage登録、research snapshot。DB1 PASS後だけ解放可能。
- DB3: Saxo増分更新、4H/1D派生、freshness監視。DB2 PASSまでLOCKED。
- DB4: read API、実backup/restore、retention、runbook運用ゲート。DB3 PASSまでLOCKED。
- RT0: strategy PnL、WFO、Holdout、portfolio。DB4 PASSまでLOCKED。

DB1時点ではimport、API、実backup、restore、retentionを実行可能であるかのように扱わない。
