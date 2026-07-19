# saxo_db データ管理IF 改善提案書

- Status: PROPOSED
- Evidence captured: 2026-07-19
- Primary audience: `saxo_db` の実装・運用担当AI
- Scope: Read API、operational view、品質event、snapshot、total-return、consumer契約
- Out of scope: 戦略、シグナル、PnL、WFO、Holdout、ポートフォリオ、注文ロジック

## 1. 結論

現行Read APIのread-only、loopback、allow-list、no-tokenという安全境界は維持する。一方、品質情報については、銘柄識別子とeventの現在有効性が統一されておらず、consumerが重要な品質eventを黙って除外できる。

最初に次のP0を完了する。

1. 品質eventを含む全operations responseで銘柄識別子を統一する。
2. OPEN/ACKNOWLEDGED eventが現在の利用を止めるeventか、履歴上未解決なだけかを明示する。
3. consumerをfail-closedへ変更し、未対応・未判定のERROR/CRITICALを無視させない。

P0完了までは正式なデータゲートを `BLOCKED_DATA_RECONCILIATION` とする。凍結済みデータによる履歴分析はdevelopment-onlyとし、live/shadowへの昇格は認めない。

## 2. 現在確認されている問題

2026-07-19に株式・REIT対象のSPY、IWM、EFA、EEM、VNQについてRead API responseを照合した。

| 観測項目 | 結果 | 対象数 | 解釈 |
|---|---:|---:|---|
| Read API health/read-only | PASS | 1 API | `saxo_app_reader`、read-only、timeout設定は正常 |
| Coverage | WARN | 5/5銘柄 | 各銘柄にmissing 50–57、out-of-session 101–108 |
| Freshness | STALE | 5/5銘柄 | live/shadow用途ではblocking候補 |
| CRITICAL eventのidentity | 不統一 | 5/5銘柄 | quality rowは`instrument_id`のみで、`instrument_key`/`symbol`がない |
| 関連OPEN ERROR/CRITICAL event | currentness不明 | 7 event | 現在のblockerか履歴上未解決かをAPI responseだけでは確定できない |
| 凍結研究データ | consumer側でhash保存 | 3,489 rows | `/bars`自体はsnapshot-boundではない |

上表のOPEN eventを「現在の品質FAIL」と断定してはならない。管理UIはこれらをhistorical open eventsとして表示するが、operations APIには現在の適用可否を示す契約がない。この不明確さ自体が本提案のP0対象である。

### Silent omissionが起きる経路

1. `coverage`と`freshness`は`symbol`を返す。
2. `quality.v_open_event`は`instrument_id`を返すが、`symbol`や`instrument_key`を返さない。
3. consumerが対象銘柄を`symbol`で絞る。
4. quality rowが対象銘柄へjoinされず、関連eventが品質判定から抜ける。
5. transportやschemaのテストはPASSしても、formal data gateが誤って進行し得る。

これは市場データ値の異常とは別の、IF契約とconsumer実装の問題である。

## 3. 目標と非目標

### 目標

- 同じ系列を全endpointで同じidentityにより参照できる。
- current stateとhistorical unresolved eventを区別できる。
- inventory、coverage、freshness、qualityを同一data versionで判断できる。
- snapshot指定時はserverがsnapshotを固定し、IDとSHA-256をresponseへ返す。
- total-returnをUI専用helperではなく、安定したread-only contractで取得できる。
- consumerが不明な品質状態をPASSへ倒せない。

### 非目標

- `saxo_db`へ売買戦略、バックテスト、PnL、WFO、Holdout、注文処理を追加しない。
- strategy projectから`market_db`へ書き込ませない。
- Saxo取得token、AccountKey、口座識別子をconsumerへ共有・保存しない。
- provider由来の異常値を根拠なく補間・置換しない。
- 既存の品質eventやmarket dataを移行の都合で削除しない。

## 4. 目標IF契約

### 4.1 共通identity

instrument-boundなinventory、coverage、freshness、quality、bars、total-return responseは、原則として次を返す。

```json
{
  "instrument_id": 9,
  "instrument_key": "spy",
  "symbol": "SPY:arcx",
  "category": "equity_reit",
  "layer": "1h",
  "price_basis": "native_ohlc"
}
```

`instrument_id`はDB内部joinの基準、`instrument_key`は安定したAPI指定子、`symbol`は表示・照合用とする。consumerは`symbol`だけをjoin keyにしてはならない。

### 4.2 品質eventのcurrentness

quality eventには少なくとも次を追加する。

- `affected_layer`
- `price_basis`
- `applicability`: `CURRENT` / `HISTORICAL` / `UNKNOWN`
- `current_blocker`: boolean
- `applicability_reason`
- `applicability_reviewed_at_utc`
- `applicability_reviewed_by`

legacy OPEN eventを推測だけで`HISTORICAL`またはRESOLVEDへ変更しない。operator照合が完了するまでは`UNKNOWN`とし、ERROR/CRITICALの`UNKNOWN`はconsumer側でblockingとする。

### 4.3 Atomic series status

追加候補endpoint:

```text
GET /api/v1/series-status?instrument_key=spy&layer=1h
```

同一read-only transactionから、次を1 responseで返す。

```json
{
  "contract_revision": "v1.1",
  "generated_at_utc": "...",
  "series": {
    "instrument_id": 9,
    "instrument_key": "spy",
    "symbol": "SPY:arcx",
    "category": "equity_reit",
    "layer": "1h",
    "price_basis": "native_ohlc"
  },
  "state": {
    "data_version": 29732294,
    "coverage_status": "WARN",
    "freshness_status": "STALE",
    "current_quality_status": "NOT_EVALUATED",
    "current_blockers": [],
    "historical_unresolved_event_count": 2
  },
  "snapshot": {
    "snapshot_id": 1,
    "snapshot_sha256": "c275...d6b",
    "bound_to_response": false
  }
}
```

`current_blockers`が空でも、`current_quality_status=NOT_EVALUATED`やERROR/CRITICALの`applicability=UNKNOWN`がある場合はPASSにしてはならない。

### 4.4 Snapshot-bound bars

追加候補endpoint:

```text
GET /api/v1/snapshots/{snapshot_id}/bars
```

要件:

- current `/api/v1/bars`と明確に区別する。
- serverが指定snapshotの存在とSHA-256を検証する。
- responseに`requested_snapshot_id`、`resolved_snapshot_id`、`snapshot_sha256`を返す。
- current DB更新後も、同じsnapshot requestのrow count、内容、SHAが変化しない。
- snapshot不一致、未検証、破損はfail-closedとする。

### 4.5 Stable total-return API

追加候補endpoint:

```text
GET /api/v1/total-return
```

UI helperから独立させ、instrument identity、date、value、provider/source dataset、quality status、data versionを返す。multi-day株式・REIT分析では、native OHLC price returnとtotal returnを混同しない。

## 5. 実装ロードマップ

| Phase | Priority | 実装内容 | 主対象 | Exit gate |
|---|---|---|---|---|
| DMI0 | P0 | consumerを`instrument_id` join + fail-closedへ修正し、株式・REITのデータゲートを再実行 | strategy consumer | 関連7 eventがreportに残り、silent omission=0 |
| DMI1 | P0 | quality view/contractへidentity、scope、applicability、current_blockerを追加し、legacy eventを照合 | `saxo_db` + operator | identity完全率100%、canonical 13のERROR/CRITICAL applicability UNKNOWN=0 |
| DMI2 | P1 | atomic series-statusとsnapshot-bound barsを追加 | `saxo_db` | data version不一致=0、snapshot hash drift=0 |
| DMI3 | P1 | stable total-return endpointを追加 | `saxo_db` | curated sourceとのdate/value/quality parity=100% |
| DMI4 | P2 | opaque cursor、JSON Schema/OpenAPI、consumer fixtureを追加 | `saxo_db` + consumer | paginationのmissing=0、duplicate=0、compatibility test PASS |

DMI0とDMI1を完了するまでDMI2へ進まない。DMI0はconsumer側の緊急封じ込め、DMI1はserver側の恒久対策であり、どちらか一方だけではP0完了としない。

## 6. Phase別の実装指示

### DMI0 — Consumer containment

- quality responseを`instrument_id`で対象universeへjoinする。
- identityが解決できないERROR/CRITICAL rowを破棄しない。
- `CURRENT`のERROR/CRITICAL、および`UNKNOWN`のERROR/CRITICALをblockingとする。
- reportへ取得event数、対象event数、unmapped event数、blocking event数を記録する。
- 修正後に同一frozen inputで株式・REITのデータゲートを再実行する。

### DMI1 — Quality contract hardening

- 新規migration候補: `db/migrations/0015_read_api_contract_hardening.sql`
- `quality.v_open_event`の互換性を維持したまま、共通identityとcurrentnessをadditiveに公開する。
- base eventを削除しない。必要なら新viewを作成し、旧viewから段階移行する。
- operatorがlegacy eventをCURRENT/HISTORICALへ分類できるprocedureを用意する。
- `/api/v1/operations/quality`へcontract revisionを追加する。
- API unit testとDB migration testへidentity欠落、UNKNOWN、権限、read-onlyのcaseを追加する。

### DMI2 — Atomic status and snapshots

- status構成要素を別requestで組み合わせず、同一transaction/data versionから返す。
- snapshotはmanifestを表示するだけでなく、bars readそのものをsnapshotへbindする。
- 既存`/api/v1/bars`は削除・型変更せず維持する。

### DMI3 — Total return

- `curated.etf_total_return_daily`の公開可能な列とprovider policyを確定する。
- UI固有responseから独立したstable contractを実装する。
- native OHLCとtotal-returnを別系列・別price basisとして返す。

### DMI4 — Contract kit

- limit超過時の`truncated`だけでなく、opaque `next_cursor`を返す。
- OpenAPIまたはJSON Schemaでresponseを固定する。
- consumer fixtureでmissing identity、unknown event、snapshot mismatch、複数pageを検証する。

## 7. Blocking受入基準

| Gate | 基準 | Threshold | Blocking |
|---|---|---:|---|
| AC-01 Identity | instrument-bound operations rowが`instrument_id`、`instrument_key`、`symbol`、`category`を返す | 100% | YES |
| AC-02 Quality | OPEN/ACK ERROR・CRITICALのcurrent applicabilityが確定している | UNKNOWN=0 for canonical 13 | YES |
| AC-03 Consumer | unmappedまたはUNKNOWNのERROR以上をfail-closedで停止する | fixture 100% BLOCKED | YES |
| AC-04 Atomicity | status内のinventory/coverage/freshness/qualityが同一data version | mismatch=0 | YES |
| AC-05 Snapshot | server検証済みsnapshot responseがcurrent DB更新で変化しない | hash drift=0 | YES |
| AC-06 Total return | stable endpointとcurated sourceのdate/value/qualityが一致する | parity=100% | multi-dayではYES |
| AC-07 Compatibility | 既存v1 fields、loopback、read-only、security headersを維持する | regression PASS | YES |
| AC-08 Pagination | 全page連結結果がdirect queryと一致する | missing=0、duplicate=0 | NO/P2 |

テストコードがPASSしただけではPhaseを閉じない。上記のblocking thresholdが実データまたは明示的fixtureで満たされたことをartifactへ保存する。

## 8. 後方互換性とrollback

- v1の既存fieldを削除・rename・型変更しない。
- 新field、新view、新endpointによるadditive changeを基本とする。
- DMI1 rollbackでは新view/routeを停止して旧contractへ戻す。base eventは残す。
- DMI2/DMI3 rollbackでは新endpointを停止する。market dataやtotal-return dataは削除しない。
- consumer rollbackは旧releaseへ戻せるようにするが、旧consumerでformal gateを再開してはならない。
- migration前後のschema、row count、event count、privilegeをmanifestへ保存する。

## 9. セキュリティ・運用制約

- bind先はloopbackを維持する。
- `saxo_app_reader`のread-only transactionとstatement timeoutを維持する。
- query対象はallow-listされたview/tableに限定する。
- 任意SQL、任意path、任意schema指定を公開しない。
- Saxo token、AccountKey、ClientKey、口座識別子をAPI response、DB、log、fixture、文書へ保存しない。
- quality eventのACK/RESOLVE/applicability reviewは既存のoperator権限モデルに合わせる。

## 10. 実装時に確認するファイル

- `README.md`
- `docs/read_api_interface.md`
- `docs/database_operations_runbook.md`
- `market_db/read_api.py`
- `market_db/inspect.py`
- `market_db/data_ui.py`
- `db/migrations/0002_market_schema.sql`
- `db/migrations/0006_operational_views.sql`
- `db/migrations/0007_operational_procedures.sql`
- `db/migrations/0010_db3_incremental_support.sql`
- `db/migrations/0011_db3_coverage_refinement.sql`
- `tests/test_read_api.py`

## 11. AI実装時の進行ルール

この文書を受け取ったAIは、いきなり全Phaseを実装しない。

1. 最初に現行schema、view、API response、既存testを再確認し、観測事実のdriftを記録する。
2. DMI0とDMI1だけの実装計画を提示する。
3. 既存の未コミット変更を所有者不明のまま上書き・削除しない。
4. migrationはappend-onlyの新番号を使用し、既存migrationを書き換えない。
5. legacy eventのcurrentnessをコードだけで推測しない。operator判断が必要なら`BLOCKED_DATA_RECONCILIATION`を維持する。
6. DMI0/DMI1のblocking受入基準と回帰テストを実行する。
7. 実装成果物、テスト結果、未解決event数、rollback方法を報告する。
8. DMI1がPASSするまでDMI2以降を開始しない。

## 12. 未決定事項

- legacy OPEN eventのcurrentnessを最終承認するoperator。
- snapshot-bound endpointがfrozen DBを直接読むか、verified Parquetを読むか。
- stable total-return endpointで許容するprovider/source dataset。
- cursorをv1 additive changeとするか、v2 contractとして固定するか。

これらは実装者が暗黙に決めず、DMI1または各Phase開始時の設計判断として明記する。

## 13. 根拠資料

本提案は、次の実response、コード、view定義、既存仕様を照合して作成した。

- 分析側取得状態: `../saxo_trading_strategy_analysis/data/raw/source_state.json`
- 分析コード: `../saxo_trading_strategy_analysis/src/equity_reit_analysis.py`
- 詳細提案artifact: `../saxo_trading_strategy_analysis/artifacts/saxo_db_data_management_if_improvement_proposal.json`
- 本プロジェクトのRead API実装・migration・test: 「実装時に確認するファイル」の一覧

上記の相対pathは`saxo_db`リポジトリrootを基準とする。取得状態は2026-07-19時点のsnapshotであり、実装開始時に再取得して差分を確認すること。
