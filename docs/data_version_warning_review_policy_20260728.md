# DataVersion warning・review・明示apply方針

適用日: 2026-07-28  
policy ID: `data_version_revision_warning_v2`

## 1. 結論

Saxo Chartの`DataVersion`変更は、今後は履歴訂正の可能性を示す警告として扱う。検知だけでは、schedulerの停止、instrumentの隔離、bounded reconcile、full-refetch、curated置換、watermark更新を実行しない。

検知・証跡保存、operator review、apply reconcileは独立した操作である。acceptedデータを変更できるのは、reviewで`APPROVE_APPLY`を記録した後の明示`apply`だけである。

SPY、SHY、GLDの既存`APPLIED` eventは履歴として保持する。migration 0029は既存eventへ`bounded_data_version_reconciliation_v1 / LEGACY_STATE`を付与し、意味を変更しない。IEF、TLT、USDJPY、追加FX候補3ペアの既存状態もmigrationだけでは変更しない。

## 2. 状態契約

| 段階 | revision status | review status | availability | curated/watermark | scheduler |
|---|---|---|---|---|---|
| 新DataVersion検知 | `REVIEW_PENDING` | `PENDING_REVIEW` | `AVAILABLE_WITH_REVISION_WARNING` | 不変 | 通常継続 |
| current維持をreview | `REVIEW_PENDING` | `REVIEWED_KEEP_CURRENT` | `AVAILABLE_WITH_REVISION_WARNING` | 不変 | 通常継続 |
| applyをreview承認 | `REVIEW_PENDING` | `APPLY_APPROVED` | `AVAILABLE_WITH_REVISION_WARNING` | 不変 | 通常継続 |
| 明示apply中 | `READY_TO_APPLY` | `APPLY_APPROVED` | `RECONCILING` | 対象instrumentだけtransaction内で変更 | 他lane継続 |
| apply成功 | `APPLIED` | `APPLIED` | `AVAILABLE` | 新accepted stateへatomic更新 | 通常継続 |

DataVersion警告だけでは`.runtime/periodic_update/state.json`へterminal blockerを作らず、`RUNNING_DEGRADED`へ遷移しない。サービス全体停止は認証不能、DB接続不能、lock破損などの共通運用障害に限定する。

## 3. 検知時の処理

通常incrementalは、取得した1H sampleのversionがaccepted watermarkと異なる場合に次だけを行う。

1. Saxoから取得済みのChart JSONを従来のimmutable acquisition run配下へ保存する。
2. accepted curated sampleと取得sampleを時刻keyで比較し、matched、content difference、version-only、new、removedの件数と比較期間を記録する。
3. `ops.data_version_revision_event`へ最初の検知時刻、old/new version、policy、review状態、証拠manifestを記録する。
4. 反復検知はeventのold/new identityを上書きせず、`ops.data_version_revision_step`へ`WARNING_RECORDED` evidenceを追記する。
5. ingestion runを`PASS`かつ`warning_code=DATA_VERSION_REVISION_REVIEW_PENDING`で終了する。

この経路は`staging.market_bar`、`raw.market_bar_revision`、`curated.market_bar`、`ops.watermark`、`derived.market_bar_4h`、`derived.market_bar_1d`を変更しない。raw Chart JSONはrevision evidenceであり、current curatedへ混在させない。

## 4. freshnessとRead API

`GET /api/v1/series-status`は次を区別する。

- `last_accepted_data_version`、`last_accepted_complete_time_utc`、`last_accepted_ingestion_run_id`: current curated/watermarkの正本
- `new_data_version`、`latest_evidence_at_utc`、`latest_provider_observed_time_utc`: 未承認のprovider evidence
- `provider_evidence_curated=false`
- `availability_status=AVAILABLE_WITH_REVISION_WARNING`
- `review_status=PENDING_REVIEW`等
- `freshness_basis=LAST_ACCEPTED_CURATED`

acceptedデータが古くなれば、freshnessは実際のaccepted時刻を基準に`STALE`となる。provider evidence時刻をaccepted freshnessとして偽装しない。`GET /api/v1/bars`は既存accepted rowsを返せるが、consumerはseries-statusのwarningとaccepted identityを同じrunへ保存し、「最新確定値」と表現してはならない。

`GET /api/v1/service-status`はwarning系列を`warning_series`へ分離する。DataVersion warningだけなら`service_status=PASS`、`degraded_series_count=0`であり、全停止または部分障害とは扱わない。

API revisionは`1.2`を維持し、OpenAPI document versionを`1.2.2`へ上げる。既存fieldと`PASS / PARTIALLY_DEGRADED / BLOCKED`は維持し、warning metadataをadditiveに追加する。

## 5. 手動reviewとapply

reviewはmarket dataを変更しない。

```bash
.venv/bin/python -m market_db.data_version_reconcile review \
  --revision-event-id <event-id> \
  --decision KEEP_CURRENT \
  --reviewer '<operator-id>' \
  --note '<判断根拠>'
```

applyする場合も、まず別操作として承認を記録する。

```bash
.venv/bin/python -m market_db.data_version_reconcile review \
  --revision-event-id <event-id> \
  --decision APPROVE_APPLY \
  --reviewer '<operator-id>' \
  --note '<変更範囲と承認根拠>'
```

その後だけ、対象eventとinstrumentを明示してapplyする。

```bash
.venv/bin/python -m market_db.data_version_reconcile apply \
  --instrument-key <key> \
  --revision-event-id <event-id> \
  --confirm APPLY_RECONCILE \
  --auth-mode keychain \
  --callback-port 8765
```

applyはevent、old/new version、instrument、price basis、review status、accepted watermarkを再検証する。guard不一致ならcurated DELETE前に失敗する。自動full-refetchへのfallbackは行わない。

Operator UIの旧汎用reconcile endpointは`REVISION_REVIEW_REQUIRED_USE_EXPLICIT_APPLY`で拒否する。これにより、review記録を経由しないtoken入力またはOAuth reconcileを防止する。

## 6. 監査・安全条件

- Saxo endpointはGET allow-listだけ。注文、precheck、account writeは0。
- 値の補間、Bid/Ask交換、clamp、手作業watermark変更は禁止。
- revision event identityと最初の検知時刻は不変。反復証拠はstepとして追記する。
- reviewはreviewer、note、時刻を必須とする。
- applyだけが対象instrumentのcurated/watermark/derivedをatomic変更できる。
- 認証・DB・HTTP障害はinterface/operational、価格値違反はdata quality、未完成barはdata not readyとして分類する。

## 7. migration・rollback・dry-run

migration 0029はfuture policy列、review procedure、warning availability view、apply guardを追加する。実DB適用前は次を行う。

```bash
.venv/bin/python -m pytest -q \
  tests/test_migration_runner.py \
  tests/test_db3_unit.py \
  tests/test_periodic_update.py \
  tests/test_read_api.py \
  tests/test_data_version_reconcile.py
```

dry-runではmigration SQLの構造、pure sample comparison、scheduler非reconcile、Read API warning分類、manual guardを検査し、live DBへmigrationまたはrevision applyを行わない。

rollbackはアプリをmigration 0028互換コードへ戻す。0029適用後にDB schemaを物理削除せず、future warning policyの書込みを停止して追加列・viewを保持するforward-compatible rollbackを使う。既存event、raw evidence、review auditを削除しない。
