# saxo_db Read API運用準備 改善指示書

- 文書日: 2026-07-21
- 状態: `IMPLEMENTATION_REQUESTED`
- 宛先: `saxo_db`の実装・運用担当AI
- Phase名: `DMI5_READ_API_OPERATIONAL_READINESS`
- 優先度: P0（次の一回限りHoldoutを設計する前に必須）

## 1. この指示書の結論

`saxo_db`のRead APIについて、**データ取得許可の前に、データ値や期間別metadataを読まずにserviceの稼働・read-only境界・API契約を確認できるoperational readiness gate**を実装してください。

今回の障害はPostgreSQL停止ではなく、Read API processが起動していなかったことです。手動起動手順が文書に存在するだけでは、一回限りの外部評価を安全に支える運用契約として不十分でした。

実装の中心は次の4点です。

1. Read APIの安全な`start`、`status`、`stop`をrepo-local commandとして提供する。
2. `/health`と非データendpointだけで判定するmachine-readable preflightを提供する。
3. API未起動、port競合、DB unhealthy、read-only違反、contract不一致をfail-closedで区別する。
4. 次回の外部consumerが不可逆なデータ読取を開始する前に、preflightの`PASS`証跡を保存できるようにする。

本Phaseでは市場データ、snapshot、quality event、watermarkを変更しません。strategy、PnL、WFO、Holdout性能、H01〜H10、portfolio、order logicも実装しません。

## 2. 障害事実

2026-07-21、外部project `saxo_trading_strategy_analysis`で明示許可後にS5一回読取を開始しました。

観測結果は次のとおりです。

| 項目 | 観測結果 |
|---|---|
| Target base URL | `http://127.0.0.1:8766` |
| Request結果 | `URLError: [Errno 61] Connection refused` |
| PostgreSQL container | running / healthy |
| `market_db.read_api` process | 未起動 |
| TCP 8766 listener | なし |
| source rows受信 | 0 |
| source bundle | 未生成 |
| Holdout performance | 未評価 |
| consumer側decision | `CONSUMED_ERROR_CLOSE_MODEL_ID` |

直接原因は `READ_API_NOT_STARTED` です。DB内容または戦略性能のFAILではありません。

根拠は次の外部consumer成果物です。内容を変更しないでください。

- `../saxo_trading_strategy_analysis/outputs/equity_reit_phase_s5_holdout_decision.json`
- `../saxo_trading_strategy_analysis/outputs/equity_reit_phase_s5_execution_audit.json`
- `../saxo_trading_strategy_analysis/config/equity_reit_phase_s5_holdout_lock.json`
- `../saxo_trading_strategy_analysis/docs/equity_reit_phase_s5_holdout_execution_report.md`

## 3. Project責務境界

### 3.1 `saxo_db`が担当すること

- PostgreSQLとRead APIの稼働確認
- Read API process lifecycle
- loopback bind、reader role、read-only transaction、timeoutの維持
- `/health`とAPI contractの提供
- inventory、coverage、freshness、quality、lineage、snapshot、total-returnの管理
- OpenAPI、fixture、test、manifest、運用runbook
- consumerが利用できるmachine-readable readiness result

### 3.2 `saxo_db`が担当しないこと

- strategy、signal、position sizing、leverage判断
- cost model、PnL、WFO、Holdout性能評価、H01〜H10
- model IDの採用・閉鎖判断
- Live/shadow tradingの承認
- consumer repositoryのlock解除または読取回数変更

`saxo_db`は取得・品質・read interfaceを担当します。戦略判断をDB側へ移さないでください。

## 4. 絶対に行ってはいけないこと

1. 閉鎖済みmodel ID `equity_reit_strategic_tilt_formal_v2::S4R-A04-SPY40`を再実行しない。
2. consumer側の`holdout_reads=1`、`CONSUMED_ERROR`、`CLOSE_MODEL_ID`を0または未読へ戻さない。
3. 今回の障害確認を理由にHoldout row、期間別件数、min/max日時、価格、returnを取得しない。
4. preflightから次のendpointを呼ばない。
   - `/api/v1/bars`への有効なdata query
   - `/api/v1/total-return`への有効なdata query
   - `/api/v1/series-status`
   - `/api/v1/operations/*`
   - `/api/v1/manifests`
   - `/api/v1/layer-counts`
5. `0.0.0.0`へbindしない。portを外部公開しない。
6. Saxo token、AccountKey、ClientKey、AccountIDをfile、DB、log、fixture、command argumentへ保存しない。
7. PostgreSQL table、market data、quality event、watermark、snapshotをpreflight都合で更新・削除しない。
8. `docker compose down -v`、volume削除、database drop、既存migration書換えを行わない。

## 5. 実装開始前の確認

担当AIは最初に次を読み、現状との差分を記録してください。

- `README.md`
- `docs/read_api_interface.md`
- `docs/database_operations_runbook.md`
- `market_db/read_api.py`
- `specs/read_api_v1_openapi.yaml`
- `tests/test_read_api.py`
- `tests/test_dmi4_contract.py`
- `tests/test_dmi4_fixture_contract.py`
- `tests/test_dmi4_pagination.py`

さらに、次のread-only確認だけを実施してください。

```bash
git status --short
docker compose -p saxo-market-data ps
pgrep -alf 'market_db.read_api'
lsof -nP -iTCP:8766 -sTCP:LISTEN
```

ここでは市場data endpointを呼びません。既存の未コミット変更があれば所有者不明のまま上書き・削除しないでください。

## 6. DMI5A — Operational readiness contract freeze

実装前にmachine-readable contractを固定してください。

候補成果物:

- `specs/read_api_operational_readiness_v1.json`
- `specs/read_api_operational_readiness_v1.schema.json`
- `tests/fixtures/read_api_operational_readiness_v1/`

最低限、次の出力を提供します。

```json
{
  "schema_version": 1,
  "status": "PASS",
  "checked_at_utc": "2026-07-21T00:00:00Z",
  "service": {
    "host": "127.0.0.1",
    "port": 8766,
    "process_running": true,
    "port_listening": true
  },
  "health": {
    "http_status": 200,
    "api_status": "PASS",
    "database_name": "saxo_market",
    "role_name": "saxo_app_reader",
    "transaction_read_only": "on",
    "statement_timeout": "30s"
  },
  "contract": {
    "api_version": 1,
    "contract_revision": "1.2",
    "bars_route_present": true,
    "total_return_route_present": true
  },
  "data_inspection": {
    "performed": false,
    "market_rows_received": 0,
    "metadata_rows_received": 0
  }
}
```

実時刻やprocess情報の追加は可能ですが、市場dataの件数、期間、watermark、price、returnを加えないでください。

### 必須status code

少なくとも次を区別してください。

| Code | 条件 |
|---|---|
| `PASS` | process、port、health、read-only、contractがすべて正常 |
| `BLOCKED_READ_API_NOT_RUNNING` | processまたはlistenerが存在しない |
| `BLOCKED_PORT_CONFLICT` | 8766を別processが使用している |
| `BLOCKED_DATABASE_UNHEALTHY` | PostgreSQLまたはAPI healthが非PASS |
| `BLOCKED_READ_ONLY_BOUNDARY` | role、transaction read-only、timeoutが契約外 |
| `BLOCKED_API_CONTRACT_MISMATCH` | version、revision、必須routeが契約外 |
| `FAILED_PREFLIGHT_INTERNAL` | preflight自身の予期しない失敗 |

UNKNOWN状態をPASSへ倒さないでください。

## 7. DMI5B — Non-data preflight command

次のようなrepo-local commandを実装してください。最終名称は既存CLI命名に合わせてよいですが、意味を変えないでください。

```bash
.venv/bin/python -m market_db.read_api_preflight --format json
```

### 許可する確認

- OS processの存在
- `127.0.0.1:8766` listener
- `GET /`
- `GET /health`
- local OpenAPI fileとのversion・route照合
- 必須parameterを省略したrequestが想定どおり`400 INVALID_REQUEST`となり、routeが存在することの確認

route probeで`/api/v1/bars`または`/api/v1/total-return`を使用する場合、**有効なinstrument、start、end、cursorを与えない**でください。0 market rowを保証します。

### 禁止する確認

- inventory、coverage、freshness、quality、series-statusの取得
- 有効なmarket data query
- databaseへの任意SQL
- preflight結果をPASSにするためのservice自動再起動

preflightは検査だけを行い、service lifecycle commandとは分離します。

### Exit code

- `0`: PASS
- `2`: 既知のBLOCKED状態
- `1`: preflight実装または予期しないsystem error

stdoutは単一JSONだけにし、diagnostic logはstderrへ出してください。秘密情報を出力しないでください。

## 8. DMI5C — Read API lifecycle command

手動foreground commandだけに依存せず、次の操作を安全に行えるrepo-local interfaceを追加してください。

```bash
.venv/bin/python -m market_db.read_api_service start
.venv/bin/python -m market_db.read_api_service status --format json
.venv/bin/python -m market_db.read_api_service stop
```

名称は調整可能ですが、最低限次を満たしてください。

### Start

- PostgreSQL containerがhealthyでなければ起動せずBLOCKする。
- 8766が別processに使用されていれば起動せずBLOCKする。
- bindは`127.0.0.1`固定。
- 起動後、有限timeout内で`/health` PASSを待つ。
- 起動済みかつ同一serviceがhealthyなら冪等PASS。
- 起動に失敗した場合、stale PIDや中途半端な状態を残さない。

### Status

- PIDの存在だけでPASSにしない。
- process identity、listener、`/health`、read-only境界、contractを照合する。
- market dataまたは期間別metadataを読まない。
- machine-readable JSONを返す。

### Stop

- repoが起動・記録したRead API processだけを対象にする。
- PID再利用や別processを誤停止しないようcommand identityを照合する。
- まずSIGTERMと有限waitを使用する。
- PostgreSQL container、volume、DB、operator UI 8765を停止しない。
- stop後も市場dataとmigration履歴が不変であることを確認する。

PID、runtime state、logを保存する場合はGit管理外の限定directoryを使用し、相対pathをrunbookへ記載してください。broadな`pkill`または未検証PIDへの`kill`は禁止します。

### macOS自動起動

必要ならLaunchAgent templateと明示的install/uninstall手順を提案できます。ただし、担当AIが自動でuser環境へinstallしないでください。自動起動を採用するかは、repo-local lifecycleとtestがPASSした後にoperator判断とします。

## 9. DMI5D — Contract、test、runbook同期

次を一つの変更setとして同期してください。

- lifecycle/preflight実装
- unit testとHTTP integration test
- `README.md`
- `docs/read_api_interface.md`
- `docs/database_operations_runbook.md`
- `specs/read_api_v1_openapi.yaml`（API responseを変更する場合）
- readiness JSON Schemaとconsumer fixture
- implementation result document
- SHA-256 manifest

OpenAPI routeそのものを変更しない場合でも、operational readiness commandと外部consumerの利用順序をrunbookへ明記してください。

外部consumerの標準順序は次です。

```text
DB container healthy
  -> Read API service start/status
  -> non-data preflight PASS artifact保存
  -> model/spec/source query freeze
  -> explicit one-time authorization
  -> atomic data acquisition and evaluation
```

preflight PASSはdata quality、coverage、freshness、または戦略性能のPASSではありません。serviceを安全に呼び出せることだけを証明します。

## 10. 必須テスト

最低限、次を自動化してください。

| Test case | 期待結果 |
|---|---|
| PostgreSQL healthy、Read API停止 | `BLOCKED_READ_API_NOT_RUNNING` |
| Read API正常起動 | `PASS` |
| 8766を別processが使用 | `BLOCKED_PORT_CONFLICT` |
| `/health`が503 | `BLOCKED_DATABASE_UNHEALTHY` |
| roleが`saxo_app_reader`以外 | `BLOCKED_READ_ONLY_BOUNDARY` |
| transaction read-onlyがoff | `BLOCKED_READ_ONLY_BOUNDARY` |
| contract revision不一致 | `BLOCKED_API_CONTRACT_MISMATCH` |
| bars routeが404 | `BLOCKED_API_CONTRACT_MISMATCH` |
| total-return routeが404 | `BLOCKED_API_CONTRACT_MISMATCH` |
| startを2回実行 | 同一healthy processで冪等PASS |
| stale PID | 誤processを停止せずfail-closed |
| stop | Read APIだけ停止、PostgreSQLとdataは維持 |
| preflight request log | 有効data query 0件 |
| preflight output | market row、期間、price、return 0件 |
| write method | 従来どおり`405 READ_ONLY_API` |
| bind | `127.0.0.1`のみ |
| full regression | 既存testを含め全件PASS |

mock testだけで閉じず、localhost実processを使ったintegration smokeを1回実行してください。integration smokeでもmarket data queryは不要です。

## 11. Blocking受入基準

| Gate | 基準 | Blocking |
|---|---|---|
| DMI5-AC01 Process | process停止を100%検出し、PASSを返さない | YES |
| DMI5-AC02 Port | listenerなし・別process競合を区別する | YES |
| DMI5-AC03 Health | HTTP 200、API PASS、DB role/read-only/timeoutが一致 | YES |
| DMI5-AC04 Contract | API v1、revision 1.2、bars/total-return route存在 | YES |
| DMI5-AC05 Non-data | preflight中のmarket/metadata row受信が0 | YES |
| DMI5-AC06 Lifecycle | start/status/stopが冪等かつ誤processを操作しない | YES |
| DMI5-AC07 Security | loopback、read-only、no-token、405を維持 | YES |
| DMI5-AC08 Regression | unit、HTTP、DB integration、contract fixtureがPASS | YES |
| DMI5-AC09 Evidence | result doc、JSON evidence、manifest hashが揃う | YES |

1つでも満たさなければ`DMI5 PASS`にしないでください。コードが実装済みでも、integration smokeと証跡がなければ`IMPLEMENTED_NOT_VALIDATED`です。

## 12. 実装成果物

最低限、次を作成してください。

1. `docs/read_api_operational_readiness_implementation_plan.md`
2. readiness contract/schema
3. preflight command
4. lifecycle command
5. unit/integration/fixture tests
6. `docs/read_api_operational_readiness_implementation_result.md`
7. `manifests/read_api_operational_readiness_implementation_manifest.json`

実装結果文書には次を含めます。

- 実装したcommandと安全境界
- incident再現結果
- 修正後のstart/status/stop smoke
- non-data request logまたは同等証拠
- test件数と結果
- DB row/data mutationが0である証拠
- 変更artifactのSHA-256
- 未解決事項
- rollback手順
- 最終statusと次ゲート

## 13. Rollback

本Phaseは原則としてDB migration不要です。

- lifecycle/preflightに問題があれば新commandを無効化し、既存foreground起動へ戻す。
- optional LaunchAgentを導入した場合はunloadしてtemplateを残すか削除する。operatorの明示判断なしに自動再installしない。
- runtime PID/log fileだけを安全に除去し、市場data、DB volume、migration、manifest原本を削除しない。
- API response contractをadditive変更した場合は新field/routeだけを停止し、既存v1 fieldを維持する。
- rollback後も`README.md`とrunbookへ現状態を反映する。

rollbackしても外部consumerの閉鎖済みmodel IDは再開しません。

## 14. 実装担当AIへの進行指示

1. 本文書と「実装開始前の確認」にある正本を読む。
2. current branch、dirty files、API/DB process状態をread-onlyで再確認する。
3. DMI5A contractと実装計画を先に作成する。
4. DMI5B non-data preflightを実装し、fixtureでfail-closedを確認する。
5. DMI5C lifecycleを実装し、誤process操作防止を確認する。
6. DMI5Dでdocs、schema、fixture、manifestを同期する。
7. unit test、HTTP integration、full regressionを実行する。
8. integration smokeで`stop -> blocked -> start -> PASS -> status PASS`を確認する。
9. 市場data query 0、data mutation 0を証拠化する。
10. implementation resultへPASS/FAIL/BLOCKEDを正確に記録する。

この順序を飛ばしてserviceを常駐化したり、外部consumerを再実行したりしないでください。

## 15. 完了時の報告形式

担当AIは次の形式で報告してください。

```text
DMI5 status: PASS | IMPLEMENTED_NOT_VALIDATED | BLOCKED | FAIL
Read API lifecycle: PASS | FAIL
Non-data preflight: PASS | FAIL
Market/metadata rows inspected by preflight: 0
Data mutation: 0
Security regression: PASS | FAIL
Full tests: <passed>/<total>
Integration smoke: PASS | FAIL
Manifest validation: PASS | FAIL
Next gate: CONSUMER_PRE_HOLDOUT_OPERATIONAL_PREFLIGHT
```

`DMI5 PASS`であっても、過去に閉鎖したstrategy model、Holdout、S6、Liveを自動解放しないでください。DMI5が解放するのは、**新しい外部consumer評価に対して、データ読取前のoperational preflightを実施できる状態**だけです。
