# DMI5 Read API運用準備 実装結果

- 実施日: 2026-07-21 JST
- Phase: `DMI5_READ_API_OPERATIONAL_READINESS`
- 最終status: `PASS`
- 次ゲート: `CONSUMER_PRE_HOLDOUT_OPERATIONAL_PREFLIGHT`

## 1. 結論

Read APIをforeground terminalへ依存せず安全に起動・状態確認・停止できるrepo-local lifecycleと、市場データを取得せずにprocess、port、DB health、read-only境界、API契約を検証するmachine-readable preflightを実装した。DMI5-AC01〜AC09はすべてPASSした。

DMI5は新しい外部consumerがデータ取得前にoperational readinessを確認できる状態だけを解放する。過去に閉鎖したmodel、消費済みHoldout、S6、Live、strategy性能は解放・再評価していない。

## 2. 実装commandと安全境界

```bash
.venv/bin/python -m market_db.read_api_service start
.venv/bin/python -m market_db.read_api_service status --format json
.venv/bin/python -m market_db.read_api_service stop
.venv/bin/python -m market_db.read_api_preflight --format json
```

- bindは`127.0.0.1:8766`固定。
- startはPostgreSQL healthyとport所有を確認し、同じhealthy processへの2回目は冪等PASS。
- stopはPID、開始fingerprint、command SHA-256、cwd、portが一致するrepo管理processだけへSIGTERMを送る。
- state/logはGit管理外の`.runtime/read_api/`に0700/0600で保存する。
- 子processからSaxo token、AccountKey、ClientKey、AccountIDを除去する。
- preflightはserviceを自動起動せず、UNKNOWNまたは異常をPASSへ倒さない。
- API route/responseは変更していないためOpenAPI revisionは`1.2`のまま。

## 3. Incident再現と修正後smoke

Read API停止、PostgreSQL healthy、8766 listenerなしの状態でpreflightを実行し、`BLOCKED_READ_API_NOT_RUNNING`、exit code 2を確認した。市場row、metadata rowの受信はともに0だった。これは2026-07-21 incidentの`READ_API_NOT_STARTED`をデータ品質FAILと混同せず再現する。

修正後の実process smokeは次の順でPASSした。

```text
stop (idempotent PASS)
  -> preflight BLOCKED_READ_API_NOT_RUNNING
  -> start PASS
  -> second start PASS / idempotent=true
  -> status and preflight PASS
  -> POST bars 405 READ_ONLY_API
  -> stop PASS
  -> PostgreSQL healthy
  -> preflight BLOCKED_READ_API_NOT_RUNNING
```

## 4. Non-data request証拠

PASS判定で送信したpathは次の4件だけである。

1. `GET /`
2. `GET /health`
3. parameterなしの`GET /api/v1/bars`（`400 INVALID_REQUEST`）
4. parameterなしの`GET /api/v1/total-return`（`400 INVALID_REQUEST`）

instrument、start、end、cursorを与えていない。`series-status`、operations、manifests、layer-counts、UI endpointは呼んでいない。`data_inspection.performed=false`、market rows 0、metadata rows 0をruntime evidenceへ保存した。

## 5. DB不変性

DMI5 lifecycle smokeの直前・直後に、対象16市場tableの`pg_stat_user_tables` DML counterと`ops.schema_migration`履歴をread-onlyでfingerprint比較した。

| 項目 | 前 | 後 | 結果 |
|---|---:|---:|---|
| 市場table DML fingerprint | `7e0a3626...d87d` | `7e0a3626...d87d` | delta 0 |
| migration件数 | 17 | 17 | 不変 |
| migration fingerprint | `a56bacba...765` | `a56bacba...765` | 不変 |

preflight/lifecycleが発行したDB mutation commandは0である。詳細は`manifests/read_api_operational_readiness_runtime_evidence.json`に保存した。

## 6. Test結果

| Test | 結果 |
|---|---|
| DMI5 unit/fixture | 15 passed |
| DMI5 localhost integration smoke | 1 passed |
| 全通常test | 116 passed / 38 integration skipped |
| 全実DB統合回帰 | 154 passed / 154 total（667.64秒） |
| DB4 validator / DMI5 extension | PASS / PASS（artifact mismatch 0） |
| write method | 405 `READ_ONLY_API` |
| bind | `127.0.0.1` only |

mockだけで完了扱いにせず、localhost実processと実PostgreSQLを使った統合smokeを実行した。

## 7. Acceptance

| Gate | 結果 |
|---|---|
| DMI5-AC01 Process | PASS |
| DMI5-AC02 Port | PASS |
| DMI5-AC03 Health | PASS |
| DMI5-AC04 Contract | PASS |
| DMI5-AC05 Non-data | PASS |
| DMI5-AC06 Lifecycle | PASS |
| DMI5-AC07 Security | PASS |
| DMI5-AC08 Regression | PASS |
| DMI5-AC09 Evidence | PASS |

## 8. 主要artifact SHA-256

| Artifact | SHA-256 |
|---|---|
| `specs/read_api_operational_readiness_v1.json` | `e4306688daf07dd7957321dd67da8f609d529f8ecfd8bbd559e7298efd526489` |
| `specs/read_api_operational_readiness_v1.schema.json` | `d303eec2eefedc540174ceb19222edfeb4cc0cf972728968d8f117d36baecff2` |
| `market_db/read_api_preflight.py` | `6b9cbd6f358281772cbc7e361c56a988492e3174621f26528c0cfc3d85365088` |
| `market_db/read_api_service.py` | `0399c646c0555ebf7f185ac6e0d33d26fc9488f131f4b5e5bde86ae2b093fa85` |
| `manifests/read_api_operational_readiness_runtime_evidence.json` | `9a374ff4c98ac519184f1deebaec3fe68257d95ed96c20ef8a2b0dc8760e5fd4` |
| `specs/read_api_v1_openapi.yaml`（変更なし） | `b61895a3dcd1d667eab648525075908c6a69e36e3e8cdee35c081ceb3bb14a25` |

変更artifactの完全なsize/SHA-256台帳は`manifests/read_api_operational_readiness_implementation_manifest.json`を正本とする。

## 9. 未解決事項

- macOS LaunchAgentはinstallしていない。自動起動を採用する場合はoperatorが別途判断する。
- 可用性SLA、remote公開、TLS、認証は現行localhost契約の対象外。
- operational preflight PASSはcoverage、freshness、quality、snapshot完全性、戦略性能を保証しない。データ取得後のgateは従来どおり別に実行する。

## 10. Rollback

1. 新しいpreflight/lifecycle commandの利用を停止する。
2. `.runtime/read_api/state.json`と実processのPID、開始fingerprint、command、cwd、portが一致する場合だけservice commandで停止する。
3. 必要なら既存のforeground `.venv/bin/python -m market_db.read_api --port 8766`へ戻す。
4. runtime state/logだけを除去し、PostgreSQL、volume、migration、市場data、quality event、snapshot、consumer lockは変更しない。
5. rollback後の起動方法をREADMEとrunbookへ反映する。

rollbackしても閉鎖済みmodelまたは消費済みHoldoutを再開しない。
