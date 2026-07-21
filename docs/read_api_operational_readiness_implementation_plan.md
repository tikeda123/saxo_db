# DMI5 Read API運用準備 実装計画

更新日: 2026-07-21 JST

状態: **DMI5A-D PASS（2026-07-21）**

## 1. 目的と事故境界

Read APIの市場データ取得前に、process、loopback listener、DB health、read-only境界、API v1 contractを非データrequestだけで確認する。2026-07-21の`READ_API_NOT_STARTED`を、PostgreSQL障害やデータ品質FAILと混同せずfail-closedで分類する。

本Phaseは市場データ、snapshot、quality event、watermark、strategy、PnL、WFO、Holdout、model IDを変更しない。閉鎖済みconsumer modelを再実行・再開しない。

## 2. 実装前の観測

- branch: `main`
- PostgreSQL: `saxo-market-data-postgres-1` healthy
- `market_db.read_api` process: なし
- `127.0.0.1:8766` listener: なし
- data endpoint request: 0
- 既存未追跡file: `docs/read_api_operational_readiness_improvement_instruction.md`のみ。指示書として保持する。

## 3. DMI5A — Contract

正本:

- `specs/read_api_operational_readiness_v1.json`
- `specs/read_api_operational_readiness_v1.schema.json`
- `tests/fixtures/read_api_operational_readiness_v1/cases.json`

readiness statusは`PASS`、5つの既知BLOCKED、内部FAILを区別する。UNKNOWN、欠落field、timeoutはPASSへ倒さない。preflightのrequest allow-listは`GET /`、`GET /health`、parameterなしの`GET /api/v1/bars`、`GET /api/v1/total-return`だけとする。

## 4. DMI5B — Non-data preflight

command:

```bash
.venv/bin/python -m market_db.read_api_preflight --format json
```

判定順序:

1. 8766 listener PIDを列挙する。
2. PIDのcommand、cwd、portを照合して`saxo_db` Read APIか別processかを区別する。
3. `GET /`からAPI version/revisionを確認する。
4. `GET /health`からDB、role、read-only、30秒timeoutを確認する。
5. parameterなしのbars/total-returnが`400 INVALID_REQUEST`であることを確認する。
6. local OpenAPIに必須routeが存在することを確認する。

stdoutは単一JSON、既知BLOCKEDはexit 2、内部FAILはexit 1、PASSはexit 0とする。market/metadata rowは受信しない。

## 5. DMI5C — Lifecycle

command:

```bash
.venv/bin/python -m market_db.read_api_service start
.venv/bin/python -m market_db.read_api_service status --format json
.venv/bin/python -m market_db.read_api_service stop
```

runtime stateはGit管理外の`.runtime/read_api/`へmode 0700で作成し、state/logは0600とする。stateはPID、process start fingerprint、固定command、cwd、portを保持する。stopは全項目が一致するrepo管理processだけへSIGTERMを送り、別process・PID再利用・unmanaged processをsignalしない。

startはPostgreSQL healthyとport非競合を先に確認し、固定commandを`127.0.0.1:8766`で起動する。有限timeout内にnon-data preflightがPASSしなければ、所有権を再検証して当該processだけを終了しstateを残さない。healthyな同一serviceへの再startは冪等PASSとする。

## 6. DMI5D — Test・証跡

- unit: status分類、HTTP parsing、OpenAPI照合、PID fingerprint、stale PID、stop signal safety
- fixture: PASSと全BLOCKED code、schema、data row 0
- HTTP: Flask test clientでroot/health/invalid-route probe、write 405
- local integration: `stop -> BLOCKED_READ_API_NOT_RUNNING -> start PASS -> status PASS -> stop PASS`
- regression: 全unit/DB integration、DB4 validator
- invariant: lifecycle前後のmarket relation DML counter delta 0、migration count不変
- evidence: result JSON、result document、SHA-256 manifest

## 7. Acceptance gate

DMI5-AC01〜AC09がすべてPASSし、localhost実process smokeとartifact hash検証が完了した場合だけDMI5 PASSとする。コードのみ完成した場合は`IMPLEMENTED_NOT_VALIDATED`とする。

次ゲートは`CONSUMER_PRE_HOLDOUT_OPERATIONAL_PREFLIGHT`であり、過去のmodel、Holdout、S6、Liveは解放しない。

## 9. 実施結果

- DMI5A: contract/schema/fixture固定 — PASS
- DMI5B: non-data preflightとfail-closed分類 — PASS
- DMI5C: repo-owned start/status/stop、冪等性、PID再利用防止 — PASS
- DMI5D: unit 116 PASS、実DB統合154 PASS、localhost smoke、DML/migration不変性、docs/manifest同期 — PASS

詳細証跡は`docs/read_api_operational_readiness_implementation_result.md`と`manifests/read_api_operational_readiness_runtime_evidence.json`を正本とする。

## 8. Rollback

新しいpreflight/lifecycle commandを使用停止し、既存foreground commandへ戻す。`.runtime/read_api/state.json`が指す所有processだけを検証後に停止し、runtime stateだけを除去する。DB、volume、migration、market data、quality event、snapshot、consumer lockは変更しない。
