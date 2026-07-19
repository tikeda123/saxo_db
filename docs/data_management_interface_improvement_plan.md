# saxo_db データ管理IF改善 実装計画書

作成日: 2026-07-19 JST

状態: **DMI0 PASS / DMI1A PASS / DMI1B PASS / DMI2A PASS / DMI2B NEXT / DMI3–DMI4 LOCKED**

基準提案: [データ管理IF改善提案書](data_management_interface_improvement_proposal.md)

## 1. 目的

外部consumerが`saxo_db`のデータを安全かつ再現可能に利用できるよう、Read API、operational view、quality event、research snapshot、total-return、consumer契約を段階的に強化する。

本計画が解決する中心課題は次の4点である。

1. endpointごとにinstrument identityが異なり、quality eventを対象系列へ確実に関連付けられない。
2. OPEN/ACKNOWLEDGED eventが現在のblockerか、履歴上未解決なeventかを区別できない。
3. consumerがquality responseを利用可否判定へ含めず、重要eventを黙って除外できる。
4. current data、frozen snapshot、total-returnの取得契約が分離・固定されていない。

既存のloopback、read-only、allow-list、no-token、no-order境界は変更しない。

## 2. プロジェクト境界

### 2.1 `saxo_db`が担当する範囲

- instrument・series identity
- raw / curated / derived / total-returnの管理
- coverage、freshness、quality、lineage、ingestion runの状態管理
- quality eventのscope、applicability、operator review、監査履歴
- current dataとfrozen snapshotのread-only提供
- Read API、Web UI、OpenAPI/JSON Schema、consumer fixture
- backup、restore、retention、migration、manifest

### 2.2 外部consumerが担当する範囲

- Read API responseのfail-closedな利用可否判定
- strategy、feature、signal、cost、PnL、WFO、Holdout、portfolio、order
- consumer自身のrun manifestとsource query記録

consumerは`saxo_db`へ書き込まず、DB password、Saxo token、AccountKey、ClientKeyを共有しない。

### 2.3 状態の分離

`BLOCKED_DATA_RECONCILIATION`は`saxo_db`全体の稼働状態ではなく、対象consumerまたは対象seriesのpromotion gateとして扱う。DBの取得、監査、backup、read-only参照は継続できる。

## 3. 現行baseline

実装開始時に必ず再取得するが、2026-07-19の確認baselineは次のとおりである。

| 項目 | baseline | 問題 |
|---|---:|---|
| OPEN/ACK quality event | 22件 | APIだけではcurrentnessを判定できない |
| 株式・REIT関連ERROR/CRITICAL | 7件 | 5 CRITICAL + 2 ERROR。identityとscopeが不十分 |
| equity/REIT coverage | 5/5 WARN | missing 50–57、out-of-session 101–108 |
| equity/REIT freshness | 5/5 STALE | live/shadowではblocking候補 |
| quality response identity | `instrument_id`のみ | key、symbol、category、layer、basisなし |
| UI event表示 | 全OPENをhistorical扱い | 未分類eventを過去と断定している |
| consumer gate | qualityを取得するが未評価 | coverage/freshnessだけでDATA_PASS判定可能 |
| research snapshot inventory | raw / curated / metadata | snapshot 1に4H/1D derivedなし |

baseline件数は実装の恒久的な固定値ではない。各Phase開始時に再取得し、差分をphase artifactへ記録する。

## 4. 設計判断

元提案に対して次を正式な修正方針とする。

1. DMI0はstrategy consumer側の緊急containment、DMI1以降は主に`saxo_db`側の恒久対策とする。
2. `quality.event`の観測事実を直接書き換えず、scopeとapplicability reviewを別のappend-only台帳へ記録する。
3. `current_blocker`は保存値にせず、event status、severity、applicabilityからviewで導出する。
4. instrumentを持たないrun/global eventをidentity欠落として破棄せず、`scope_kind`で表現する。
5. atomicityは「全componentが同じdata_version」ではなく、「同一read-only transactionのMVCC snapshot」とcomponent別high-watermarkで保証する。
6. snapshot 1のread endpointは存在するdata layerだけを公開する。4H/1Dを捏造せず、必要なら新snapshotを別Phaseで作る。
7. total-returnのinstrument対応はsymbol文字列joinではなく、明示的catalog mappingで管理する。
8. contract fixtureと変更endpointのschema testをDMI4まで延期せず、各Phaseへ含める。
9. migrationはappend-onlyとし、適用済み0001–0014を変更しない。

## 5. Phase構成

| Phase | Priority | 主担当 | 内容 | 開始条件 | 終了条件 |
|---|---:|---|---|---|---|
| DMI0 | P0 | strategy consumer | quality gate containment | 計画承認 | silent omission 0、UNKNOWN/未対応ERROR以上をBLOCK |
| DMI1A | P0 | `saxo_db` | identity・scope・review schema/API/UI | DMI0仕様凍結 | identity完全、未分類をhistorical表示しない |
| DMI1B | P0 | `saxo_db` + operator | legacy event照合・将来lifecycle | DMI1A runtime PASS | pre-existing blocking-scope eventのUNKNOWN 0 |
| DMI2A | P1 | `saxo_db` | atomic series status | DMI0/DMI1 PASS | 同一MVCC snapshot、component revision整合 |
| DMI2B | P1 | `saxo_db` | snapshot-bound read | DMI2A PASS | current DB更新に対してsnapshot内容不変 |
| DMI3 | P1 | `saxo_db` | stable total-return API | DMI1 PASS | mapping一意、source parity 100% |
| DMI4 | P2 | `saxo_db` + consumers | cursor・契約kit完成 | DMI2/DMI3 PASS | page欠落・重複0、compatibility PASS |

DMI0とDMI1を完了するまでDMI2以降を開始しない。DMI2A、DMI2B、DMI3は個別に実装・検証し、まとめて一つの巨大変更にしない。

## 6. DMI0 — Consumer containment

対象repository: `saxo_trading_strategy_analysis`などの外部consumer。`saxo_db`へstrategyロジックを追加しない。

### 6.1 実装内容

1. consumerのsource preflightにquality response評価を追加する。
2. 対象universeは`instrument_id`でjoinする。
3. `instrument_id IS NULL`のERROR/CRITICALはglobal/run scope候補として破棄しない。
4. 次をblockingとする。
   - `applicability=CURRENT`のERROR/CRITICAL
   - `applicability=UNKNOWN`またはfield未提供のERROR/CRITICAL
   - identity/scopeを解決できないERROR/CRITICAL
5. WARN、ERROR、CRITICALを同じ扱いへ丸めず、件数と理由を保存する。
6. report/run manifestへ次を記録する。
   - APIから取得した全event数
   - universeへ関連付いたevent数
   - global/run event数
   - unmapped event数
   - UNKNOWN event数
   - blocking event数とID
7. 同一frozen inputでdata gateを再実行する。市場分析結果は再計算してよいが、P0完了まではpromotionしない。

### 6.2 Consumer fixture

最低限、次のfixtureを作成する。

| Case | 期待結果 |
|---|---|
| mapped CURRENT CRITICAL | BLOCKED |
| mapped HISTORICAL CRITICAL | eventを報告し非blocking |
| mapped UNKNOWN ERROR | BLOCKED |
| global UNKNOWN CRITICAL | 全対象をBLOCKED |
| unmapped ERROR | BLOCKED |
| WARNのみ | policyに従いWARN、暗黙PASSにしない |
| quality response空 | emptyの根拠を記録。schema欠落とは区別 |
| quality field欠落 | contract failureとしてBLOCKED |

### 6.3 DMI0 exit gate

- quality responseがdata gateの入力として実際に評価される。
- fixtureのERROR/CRITICAL fail-closed率100%。
- eventのsilent omission 0件。
- frozen inputのhash、snapshot metadata、query parameterが変更されていない。
- consumerのpromotion statusが`BLOCKED_DATA_RECONCILIATION`または明示的な非blocking結果になる。

## 7. DMI1A — Identity・scope・review contract

### 7.1 Migration

候補migrationは`0015_read_api_contract_hardening.sql`とする。実装開始時に最新番号を再確認し、競合があれば次の未使用番号へ変更する。

既存`quality.event`を削除・再作成・一括更新しない。次のappend-only構造を追加する。

#### `quality.event_scope`

eventの影響範囲を保持するappend-only台帳。再分類時は既存rowを更新せず新しいrowを追記し、current viewが最新scopeを選択する。

| Column | 要件 |
|---|---|
| `scope_id` | identity PK |
| `quality_event_id` | FK `quality.event` |
| `scope_kind` | `GLOBAL`, `RUN`, `DATASET`, `INSTRUMENT`, `SERIES`, `BAR` |
| `source_dataset_id` | nullable FK。推測で埋めない |
| `affected_layer` | nullable。例:`raw`, `1h`, `4h`, `1d`, `total_return` |
| `price_basis` | nullable。未知ならnull |
| `scope_evidence` | JSONB。run ID、rule、根拠を格納 |
| `recorded_at_utc` | UTC timestamp |
| `recorded_by` | 固定processまたはoperator label |

#### `quality.event_applicability_review`

operator判断をappend-onlyで保存する監査台帳。

| Column | 要件 |
|---|---|
| `review_id` | identity PK |
| `quality_event_id` | FK `quality.event` |
| `applicability` | `CURRENT`, `HISTORICAL`, `UNKNOWN` |
| `reason` | 空文字禁止 |
| `superseded_by_ingestion_run_id` | nullable FK |
| `reviewed_at_utc` | UTC timestamp |
| `reviewed_by` | 空文字禁止 |

既存reviewをUPDATEせず、再判定は新しいreview rowを追記する。current viewは最新reviewを選択する。

### 7.2 Event status view

新規`quality.v_event_status`を作成し、最低限次を返す。

```text
quality_event_id
event_status
severity
rule_id
scope_kind
instrument_id
instrument_key
symbol
category
affected_layer
price_basis
source_dataset_id
time_utc
applicability
applicability_reason
applicability_reviewed_at_utc
applicability_reviewed_by
superseded_by_ingestion_run_id
current_blocker
action
created_at_utc
```

`current_blocker`は次の条件から導出する。

```text
event_status IN (OPEN, ACKNOWLEDGED)
AND severity IN (ERROR, CRITICAL)
AND applicability IN (CURRENT, UNKNOWN)
```

reviewが存在しないeventの`applicability`は`UNKNOWN`とする。

既存`quality.v_open_event`は削除せず、既存8 fieldを維持したうえでidentity、scope、applicability、current blockerを末尾へadditiveに追加する。

### 7.3 共通identity

instrument-boundなinventory、coverage、freshness、quality、bars responseへ次を揃える。

```text
instrument_id
instrument_key  # catalog.instrument.market_key
symbol
category
layer
price_basis
```

規則:

- internal joinは`instrument_id`を使用する。
- API指定は`instrument_key`を使用する。
- `symbol`だけをjoin keyにしない。
- GLOBAL/RUN/DATASET eventのinstrument fieldはnullを許容し、`scope_kind`を必須とする。
- layer/basisを根拠なしに推測しない。
- metadata inventoryやtotal-return mapping未完了行は、instrument-bound ACの分母へ混ぜない。

### 7.4 API contract

`GET /api/v1/operations/quality`の既存fieldを維持し、additive fieldを返す。operations response envelopeへ次を追加する。

```json
{
  "api_version": 1,
  "contract_revision": "1.1",
  "generated_at_utc": "...",
  "command": "quality",
  "row_count": 0,
  "rows": []
}
```

既存consumerが`command`、`row_count`、`rows`を読む動作を壊さない。

### 7.5 Operator procedure・CLI

`saxo_ops_operator`だけが実行できる固定procedureを追加する。

候補:

```text
quality.record_event_scope(...)
quality.review_event_applicability(...)
```

CLI候補:

```bash
.venv/bin/python -m market_db.operate review-quality <event-id> \
  --applicability CURRENT \
  --operator <label>
```

reasonはshell引数へ直接書かず、既存quality操作と同様にpromptまたはstdinから入力する。任意SQL、任意table、event削除、market data更新を許可しない。

### 7.6 Web UI

`/ui/quality`を次のように変更する。

- 「過去OPEN event」を「未解決event」へ変更する。
- `CURRENT`、`HISTORICAL`、`UNKNOWN`をbadge表示する。
- UNKNOWN ERROR/CRITICALをcurrent blockerとして表示する。
- current quality matrixへquality blockerを統合する。
- historical eventは件数を残すがcurrent FAILへ混入させない。
- operator reviewの実行ボタンはDMUIへ追加しない。既存operator権限CLIに限定する。

## 8. DMI1B — Legacy reconciliation・将来lifecycle

### 8.1 Legacy event review

実装時点の全OPEN/ACK eventをexportし、次の根拠をevent単位で照合する。

- event rule、severity、action、observed value
- instrument、source dataset、layer、price basis
- event生成runのstatus/error code
- 後続full-refetchまたはnormal PASS run
- current watermark、data version、coverage、freshness
- raw archiveだけを対象とするeventかcanonicalを対象とするeventか

コードだけでHISTORICALへ一括分類しない。AIは候補と根拠を作成できるが、最終reviewには明示的なoperator labelとreasonを必要とする。

特に次を区別する。

- `source_series_quality_gate`: legacy/raw archive品質とcurrent canonical品質を分離する。
- `db3_atomic_run_gate`: block発生後の復旧runを照合する。
- `instrument_id IS NULL`: token/run/global failureとしてscopeを確定する。

### 8.2 Future lifecycle

新規event生成時は、同じtransactionまたは直後の固定処理で`quality.event_scope`も作成する。

ruleごとに次を機械仕様へ記録する。

- default scope
- default applicability
- blocking severity
- supersession condition
- automatic reviewを許可するか
- operator reviewを必須とするか

自動supersessionを導入する場合も、元eventを削除・改変せずreview rowを追記する。

### 8.3 DMI1 exit gate

- instrument-bound operations identity完全率100%。
- GLOBAL/RUN eventのscope表現率100%。
- pre-existing OPEN/ACK ERROR/CRITICALのapplicability未分類0件。global eventも含む。
- UNKNOWN ERROR/CRITICALが存在する間、API/UI/consumerがblockingと判定する。
- UIが未分類eventをhistoricalと表示しない。
- read-only、loopback、security header、role privilege回帰PASS。
- base `quality.event`件数と内容がreview実装によって失われていない。

## 9. DMI2A — Atomic series status

追加endpoint:

```text
GET /api/v1/series-status?instrument_key=spy&layer=1h&price_basis=native_ohlc
```

同一read-only transactionからidentity、coverage、freshness、quality、watermark、latest runを取得する。

responseのconsistency情報:

```json
{
  "contract_revision": "1.1",
  "generated_at_utc": "...",
  "series": {},
  "consistency": {
    "read_at_utc": "...",
    "watermark_data_version": 0,
    "latest_ingestion_run_id": 0,
    "quality_event_high_watermark": 0
  },
  "state": {
    "coverage_status": "WARN",
    "freshness_status": "STALE",
    "quality_status": "NOT_EVALUATED",
    "eligibility_status": "BLOCKED",
    "current_blockers": [],
    "historical_unresolved_event_count": 0
  }
}
```

`quality_event_high_watermark`などはcomponent revisionであり、全componentへ同じdata versionを捏造しない。

DMI2A exit gate:

- 全componentが同一DB transaction snapshotから取得される。
- component別high-watermarkがresponseにある。
- 別requestのclient-side joinを正式preflightに使用しない。
- UNKNOWN ERROR/CRITICALがある場合は`eligibility_status=BLOCKED`。
- 既存operations endpointを削除しない。

## 10. DMI2B — Snapshot-bound read

追加endpoint候補:

```text
GET /api/v1/snapshots/{snapshot_id}/bars
```

### 10.1 実装方式

- `saxo_research_v13`を`v13_research_reader`で直接読む。
- current `saxo_market`のbarをcutoff条件だけで切り出す実装は禁止する。
- FDW、dblink、cross-database linkを追加しない。
- snapshot DB内の`ops.research_snapshot`とmanifest情報を検証する。
- current APIとは別の固定connection poolを使い、read-only、statement timeout、pool上限を設定する。
- responseへsnapshot ID、cutoff、snapshot SHA-256、source database、query parameterを返す。

### 10.2 Layer制約

snapshot 1の現行inventoryにはraw、curated、research metadataがあり、DB3 derived 4H/1Dは存在しない。

- 初期snapshot endpointは受理済みcurated 1Hを対象とする。
- total-returnはDMI3のsnapshot contractから提供する。
- 4H/1D requestは`SNAPSHOT_LAYER_NOT_AVAILABLE`でfail-closedにする。
- frozen 4H/1Dが必要な場合は、既存snapshotを変更せず、新しいsnapshot IDとmanifestを作る別計画を承認する。

### 10.3 Exit gate

- current DB更新前後で同一snapshot queryのrow count、ordered content digest、snapshot SHAが不変。
- snapshot未検証、破損、不明ID、未収録layerをfail-closedで拒否する。
- current `/api/v1/bars`との混同がない。
- research DBのdefault read-onlyとcutoffを維持する。

## 11. DMI3 — Stable total-return API

追加endpoint候補:

```text
GET /api/v1/total-return
GET /api/v1/snapshots/{snapshot_id}/total-return
```

parameter:

```text
instrument_key
start
end
source_dataset_id  # optional。複数候補時は必須
limit
eligibility        # eligible / stored_complete
```

### 11.1 Explicit mapping

symbol文字列joinを正式契約にしない。候補migrationで次を追加する。

```text
catalog.series_instrument_mapping
```

最低列:

```text
source_dataset_id
external_series_key
instrument_id
mapping_kind
mapping_reason
approved_at_utc
approved_by
```

`(source_dataset_id, external_series_key)`を一意にし、明示review後だけAPI公開対象にする。TLTのように同一symbolを複数catalog instrumentが持つ場合も、symbolの一致だけで選択しない。

### 11.2 Response

最低限次を返す。

```text
instrument_id
instrument_key
symbol
category
source_dataset_id
provider/source
session_date
value
volume
quality_status
price_basis=etf_total_return
```

native OHLC、adjusted close、total-return indexを同一fieldへ混ぜない。endpointの`value`はtotal-return indexであることを固定する。

### 11.3 Exit gate

- mapping一意率100%、未承認mapping 0件で公開しない。
- curated sourceとのdate/value/volume/quality parity 100%。
- `eligible`はPASSだけ、`stored_complete`は警告付きでWARN/NOT_EVALUATEDを許可する。
- native OHLC endpointとprice basisを混同しない。
- UI helperを壊さず、stable endpointへ段階移行できる。

## 12. DMI4 — Contract kit・pagination

### 12.1 Contract artifacts

各Phaseで変更したendpointのschemaはそのPhase内でtestする。DMI4では全v1 endpointを統合する。

候補成果物:

```text
specs/read_api_v1_openapi.yaml
tests/fixtures/read_api_contract_v1/
docs/read_api_interface.md
```

### 12.2 Cursor

- cursorはquery条件、last composite key、snapshot/state revisionへbindする。
- query条件と異なるcursorを拒否する。
- snapshotまたはstate revisionが変わった場合は安定したerror codeを返す。
- current dataのbounded time-window取得は維持し、cursor利用を必須にしない。
- cursorを単なるtimestampにせず、`time/session_date + price_basis + series identity`の複合順序を保持する。

### 12.3 Exit gate

- 全page連結結果が同一snapshotのdirect queryと一致する。
- missing 0、duplicate 0、order reversal 0。
- cursor改変、期限切れ、query mismatchを拒否する。
- OpenAPI/JSON Schema compatibility suite PASS。

## 13. Test計画

### 13.1 Unit/static

- identity normalization
- scope/applicability/current blocker導出
- latest review選択
- global/run event処理
- API response schemaと後方互換性
- total-return mapping ambiguity
- snapshot layer allow-list
- cursor query binding
- error responseにSQL、secret、絶対pathがないこと

### 13.2 Database integration

- migration初回適用、再実行skip、checksum不一致拒否
- `quality.event`不変、scope/review append-only
- role privilege: reader DML拒否、operator固定procedureのみ
- current blocker viewとfixture parity
- identity completeness
- research snapshot read-only/cutoff
- total-return source parity
- existing DB1–DB4・DMUI回帰

### 13.3 API/UI

- GET/HEAD以外405
- loopback固定、CSP/no-store/security headers
- operations旧field維持
- series-status atomic read
- snapshot hash drift 0
- UIのCURRENT/HISTORICAL/UNKNOWN表示
- UNKNOWN ERROR/CRITICALのblocking表示
- TradingView chart、inventory、run、backup画面回帰

### 13.4 Runtime gate

Phase完了時はunit testだけでなく、実DBまたは明示fixtureで受入thresholdを確認する。統合testをskipした実行だけでPASSにしない。

## 14. Migration・後方互換性・rollback

- 既存migrationを変更しない。
- 新table/view/field/endpointによるadditive changeを基本とする。
- v1既存fieldを削除、rename、型変更しない。
- destructive down migrationを作らない。
- rollbackはアプリrouteを旧contractへ戻す。追加台帳とreview historyは残す。
- base quality event、market data、snapshot、total-returnをrollbackで削除しない。
- consumer旧releaseへ戻した場合もformal promotion gateを再開しない。
- migration前後のschema、row count、event count、privilege、view columnをmanifestへ保存する。

## 15. Security gate

全Phaseで次を維持する。

- bind `127.0.0.1`
- `saxo_app_reader` / `v13_research_reader`のread-only transaction
- parameterized queryと固定allow-list
- connection pool上限とstatement timeout
- 任意SQL、任意relation、任意pathなし
- token、AccountKey、ClientKey、口座識別子保存0
- HTTP database write route 0
- Saxo write/order/precheck 0
- Web UIからoperator procedureを実行しない

## 16. 成果物

### DMI1

```text
db/migrations/0015_read_api_contract_hardening.sql
market_db/read_api.py
market_db/inspect.py
market_db/data_ui.py
market_db/operate.py
market_db/static/data-ui/data-ui.js
tests/test_read_api.py
tests/test_data_ui.py
tests/test_operate_cli.py
tests/test_dmi1_integration.py
specs/data_management_interface_improvement_spec.json
docs/data_management_interface_improvement_plan.md
docs/dmi1_implementation_result.md
docs/read_api_interface.md
docs/database_operations_runbook.md
manifests/dmi1_implementation_manifest.json
```

DMI2–DMI4は各Phaseで別migration、result、manifestを作る。実装番号は開始時に最新migrationを再確認して確定する。

## 17. Manifestと証跡

DMI1 manifestは次を最低限記録する。

- parent DMUI4/DB4 evidenceのpathとSHA-256
- migration番号、filename、SHA-256、適用DB
- pre/post quality event件数
- scope/applicability分類件数
- CURRENT/HISTORICAL/UNKNOWN内訳
- global/run/instrument/series/bar scope内訳
- identity completeness
- blocking event数
- operator review labelと時刻。reason本文にsecretを含めない
- unit/integration/runtime test結果
- read-only/security回帰
- orders/prechecks 0、credential保存0
- 全成果物のsizeとSHA-256

liveに変化するrow countを固定manifestの恒久制約にしない。実装時baselineと現在状態を分離する。

## 18. 実施順序

1. Git差分と未追跡fileを確認し、既存変更を保護する。
2. 現行schema、view、API/CLI、quality event、consumer gateを再取得する。
3. DMI0/DMI1の機械仕様とfixtureを先に凍結する。
4. 外部consumerでDMI0 containmentを実装・検証する。
5. DMI1 migration、view、procedure、API、UIを実装する。
6. legacy event review候補と根拠を生成する。
7. operator承認後、applicability reviewをappendする。
8. DMI0/DMI1のunit、integration、runtime、security gateを実行する。
9. result文書とmanifestを作成し、DMI1をPASS/FAIL/BLOCKED判定する。
10. DMI1 PASS後にDMI2Aだけを開始する。
11. DMI2A PASS後、DMI2B、DMI3を個別に進める。
12. DMI2/DMI3 PASS後にDMI4を開始する。

## 19. 総合受入基準

| ID | Area | Threshold | Blocking |
|---|---|---:|---|
| AC-01 | instrument identity | instrument-bound rowのID/key/symbol/category 100% | YES |
| AC-02 | non-instrument scope | GLOBAL/RUN/DATASET eventのscope 100% | YES |
| AC-03 | quality currentness | pre-existing blocking-scope ERROR/CRITICALのUNKNOWN 0 | YES |
| AC-04 | fail-closed consumer | unmapped/UNKNOWN ERROR以上のfixture BLOCK 100% | YES |
| AC-05 | UI semantics | 未分類eventをhistorical表示するcase 0 | YES |
| AC-06 | atomic status | 1 transaction、component revision mismatch 0 | YES |
| AC-07 | snapshot | current DB更新後のordered content/hash drift 0 | YES |
| AC-08 | total return | mapping一意、source parity 100% | multi-dayでYES |
| AC-09 | compatibility | v1既存field・read-only・security回帰PASS | YES |
| AC-10 | pagination | missing 0、duplicate 0 | DMI4 |

テストコードが存在するだけではPhaseを閉じない。実データまたは明示fixtureでthresholdを満たし、証跡をmanifestへ保存したときだけPASSとする。

## 20. LOCKと未決定事項

DMI1 PASSまで次をLOCKする。

- atomic statusの正式consumer移行
- snapshot-bound APIの正式利用
- stable total-return APIの正式利用
- cursor pagination
- live/shadow promotion

実装者が暗黙に決めてはならない事項:

1. legacy eventの最終operator承認者。
2. 新規eventのrule別automatic supersession policy。
3. frozen 4H/1Dを含む新snapshotを作るか。
4. total-returnでcanonicalとするsource dataset。
5. cursorをv1 additiveにするかv2で固定するか。

これらは各Phase開始時の仕様凍結または明示承認で決定する。
