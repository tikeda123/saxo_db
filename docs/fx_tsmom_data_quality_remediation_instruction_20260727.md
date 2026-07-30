# FX時系列モメンタム向けデータ取得・品質判定改善指示書

- 作成日: 2026-07-27 JST
- 対象repository: `saxo_db`
- 依頼元consumer: `saxo_trading_fx`
- 対象系列: EURUSD / USDJPY、`FxSpot`、1H、`bid_ask_mid`
- 現在判定: `BLOCKED_DATA_OPERATIONAL_REMEDIATION_REQUIRED`
- 実行環境: Saxo SIM / GET only / localhost Read API

## 1. 目的

マルチアセットのトレンドフォローへ組み込むFX時系列モメンタム研究の最初の2市場として、EURUSDとUSDJPYを同一の取得・品質・公開契約で継続運用できる状態にする。

この作業では、次を一つの連続したデータ管理契約として実装・検証する。

```text
Saxo SIM OpenAPI
  -> immutable raw response
  -> normalized 1H bid/ask and bid_ask_mid
  -> curated.market_bar
  -> coverage / freshness / quality / watermark / lineage
  -> atomic Read API series-status
  -> saxo_trading_fx consumer
```

本指示は単なる価格値の「品質改善」ではない。取得運用、DataVersion復旧、scheduler、quality-event scope、coverage原因分類、Read API公開を対象とする。

## 2. repository境界

### `saxo_db`が所有するもの

- Saxo SIM OpenAPIからの市場データ取得
- raw response、request metadata、SHA-256、run manifestの保存
- rawからcurated、4H、1Dへの派生
- coverage、freshness、quality、watermark、DataVersion、lineage
- scheduler、retry、catch-up、reconciliation
- read-only Read API、`series-status`、bars公開
- 運用run、quality event、manifest、監査証跡

### 本作業の対象外

- 時系列モメンタムsignal、ルックバック、volatility target
- WFO、Holdout、Shadow、PnL、position sizing
- spread、slippage、rolloverを使う戦略損益計算
- precheck、order、fill、口座残高処理
- `saxo_api` repositoryの修正
- Saxoへの問い合わせや障害認定。ただしrawレスポンスでprovider側異常を再現した場合は、証拠を添えて別blockerとして報告する

`saxo_trading_fx`側に取得処理を複製しないこと。consumerは`series-status`で利用可否を確認してからRead APIのbarsを読む。

## 3. 2026-07-27確認時点の事実

### 3.1 正常なinterface・認証

- Read API `/health`: `PASS`
- database role: `saxo_app_reader`
- transaction: read only
- periodic service: `RUNNING`
- authentication: `AUTH_READY`
- environment: `SIM`
- Saxo smoke test `users_me`: HTTP `200`
- order / precheck / write request: `0`

したがって、現時点ではSaxo OpenAPI outageまたはRead API outageを原因としない。

### 3.2 EURUSD 1H

2026-07-27T00:56:39Zのatomic `series-status`:

- history start: `2010-06-17T21:00:00Z`
- actual rows: `102,739`
- expected rows: `92,422`
- calendar-aligned rows: `92,017`
- missing rows: `405`
- duplicate rows: `0`
- coverage: `WARN`
- latest complete: `2026-07-24T19:00:00Z`
- expected latest complete: `2026-07-26T23:00:00Z`
- watermark data status: `STALE_DATA_VERSION`
- freshness: `FAIL`
- quality: `FAIL`
- eligibility: `BLOCKED`
- current blockers: 223件
  - EURUSD固有: 2件
  - global: 221件

periodic runはSaxo smoke testに成功した後、Chart取得前に次で停止している。

```text
BLOCKED_CANONICAL_WATERMARK_SET
```

直近runは`request_count=1`、`successful_series=0`、Chart artifact 0である。現在の直接原因はprovider応答失敗ではなく、ローカルのcanonical watermark preconditionである。

### 3.3 USDJPY 1H

2026-07-27T00:56:41Zのatomic `series-status`:

- history start: `2010-06-18T00:00:00Z`
- actual rows: `102,730`
- expected rows: `92,420`
- calendar-aligned rows: `92,014`
- missing rows: `406`
- duplicate rows: `0`
- coverage: `WARN`
- latest complete: `2026-07-24T18:00:00Z`
- expected latest complete: `2026-07-26T23:00:00Z`
- watermark data status: `ACTIVE`
- freshness: `STALE`
- quality: `FAIL`
- eligibility: `BLOCKED`
- current blockers: 221件
  - USDJPY固有: 0件
  - global: 221件

USDJPY固有のcurrent quality blockerは0件である。現在の`quality=FAIL`をUSDJPYの価格値異常と解釈しないこと。

### 3.4 schedulerの非対称

現行`market_db.periodic_update`は次の固定値を持つ。

```python
FX_KEYS = ("eurusd",)
```

定期FX slot、expected watermark、catch-upはEURUSDだけを対象とし、USDJPYは含まれていない。USDJPYのSTALEはSaxo API障害ではなく、現行scheduler scopeの不足である。

### 3.5 coverage WARNの位置付け

EURUSDのmissing 405本は既存のS6V5A remediationで、説明可能な非blocking `COVERAGE_WARN`として扱われ、`freshness=PASS`および`quality=PASS`と両立した実績がある。

今回のEURUSD／USDJPYのBLOCKEDを、missing 405／406本だけで説明しないこと。coverage WARN、freshness、DataVersion、quality-event scopeを別々に復旧・判定する。

## 4. 必須修正

### R1. 同一blocked runの30秒再実行とquality-event増殖を止める

#### 現状

`BLOCKED_CANONICAL_WATERMARK_SET`がoperator介入なしには解消しない状態でも、同一slotが約30秒ごとに再実行される。各attemptが新しいingestion runとglobal CRITICAL／ERROR eventを生成し、無関係なUSDJPYにもquality FAILを波及させている。

#### 修正要件

1. retry対象をerror taxonomyで分ける。
   - transient retry対象: timeout、接続切断、有限429、token refresh後の単発401、未確定bar
   - operator/reconciliation待ち: `BLOCKED_CANONICAL_WATERMARK_SET`、`BLOCKED_FULL_REFETCH_REQUIRED`、instrument drift、DataVersion復旧失敗
2. operator/reconciliation待ちcodeでは、同一`slot_id + selected instrument set + error_code + watermark revision`を30秒ごとに再実行しない。
3. 最初のblocked attemptを監査証跡として保存し、service stateには`BLOCKED_OPERATOR_ACTION_REQUIRED`相当の明示状態、必要action、対象系列、最初と最後の観測時刻を出す。
4. 同じ状態の再観測は新しいCRITICAL eventを無制限に追加せず、既存eventの観測回数またはlast-seenを更新する設計にする。
5. watermark revision、token/auth state、operator reconcile完了など、再試行可能条件が変化した場合だけ次attemptを許可する。
6. fail-closedは維持する。retry停止をPASS、完了slot、watermark前進として扱わない。
7. service restart後も同じterminal blockerを忘れてretry stormへ戻らない。

#### 受入条件

- terminal blockerを10分監視しても、同一条件のingestion run／quality eventが1件を超えて増殖しない
- `next retry at`ではなく、operator actionと再開条件がstateへ表示される
- transient errorは既存の有限retryを維持する
- blocked slotをdeadline内PASSとして数えない
- restart後もidempotencyが維持される

### R2. quality eventの影響範囲をrun対象系列へ限定する

#### 現状

EURUSDだけを選択したFX slotのprecondition failureが、`instrument_id=NULL`のglobal blockerとして記録され、USDJPYのatomic `series-status`まで`quality=FAIL`にしている。

#### 修正要件

1. run作成時にselected instrument keys／instrument IDsを不変のscope evidenceとして保存する。
2. Chart取得前のprecondition failureでも、対象がEURUSDだけならEURUSD scopeのeventとして記録する。
3. canonical全体に実際に影響するエラーだけをglobalとする。単に`failed_instrument_id`が未設定という理由でglobalへ昇格しない。
4. `series-status`は、対象instrumentへ適用可能なCURRENT／UNKNOWN ERROR・CRITICALだけをcurrent blockerとして数える。
5. 過去に生成済みのglobal eventは削除しない。修正後のscope再判定または成功runのsupersession evidenceで`RESOLVED`／非current化する。
6. historical unresolved countとcurrent blocker countを分離した既存契約を維持する。
7. interface、operational、data qualityを別domainとして公開する。provider疎通失敗を価格値異常へ分類しない。

#### 受入条件

- EURUSDだけのblocked runでUSDJPYのquality statusがFAILにならない
- USDJPY固有blocker 0、global適用可能blocker 0をatomic responseで確認できる
- canonical全体runの真のglobal failureは全対象系列をfail-closedにする
- scope修正前の監査event、run ID、時刻、元error codeは保持される

### R3. EURUSDのDataVersion／watermarkを正規手順で復旧する

#### 修正・運用要件

1. 実行前にread-onlyで次を保存する。

```bash
.venv/bin/python -m market_db.incremental_update status
.venv/bin/python -m market_db.inspect freshness --format json
.venv/bin/python -m market_db.inspect quality --format json
curl --fail --get 'http://127.0.0.1:8766/api/v1/series-status' \
  --data-urlencode 'instrument_key=eurusd' \
  --data-urlencode 'layer=1h' \
  --data-urlencode 'price_basis=bid_ask_mid'
```

2. 稼働schedulerとreconcileを並行実行しない。service identityが正規か確認し、必要な場合だけ既存manager経由で安全にpause／stopする。PID kill、lock削除、state削除で回避しない。
3. session-only credentialを保持する同一operator sessionで、既存runbookの`reconcile`を使用する。
4. EURUSDが`STALE_DATA_VERSION`なら、guard付きfull-refetchで対象1系列を最古まで再取得する。
5. raw revision、DataVersion、query parameters、取得時刻、response SHA-256、old/new lineageを保存する。
6. 手動DELETE、watermark直接UPDATE、DataVersion無視、既存raw上書きは禁止する。
7. full-refetch後、canonical通常runを連続2回PASSさせる。
8. 同一銘柄でDataVersionが再変化、履歴が既存最古まで届かない、raw／curated parity不一致の場合は停止し、別blockerとして報告する。

#### 受入条件

- EURUSD watermark `data_status=ACTIVE`
- current DataVersionとwatermark DataVersionが一致
- latest ingestion run `PASS`
- freshness `PASS`
- quality `PASS`
- current／unknown blocker 0
- `BLOCKED_CANONICAL_WATERMARK_SET`再発0
- raw old revisionとnew revisionの両方が追跡可能

### R4. USDJPYをEURUSDと同じhourly scheduler contractへ追加する

#### 修正要件

1. FX scheduler対象をEURUSDとUSDJPYの両方にする。
2. 各FX hourly slotの`instrument_keys`と`expected_latest_complete`へ両系列を含める。
3. 毎UTC時03分取得、10分deadline、FX weekend／DST／17:00 New York maintenanceの同一contractを適用する。
4. scheduler restart時のcatch-upにも両系列を含める。
5. 片方だけ失敗した場合、atomic commit、rollback、event scope、retryを明示する。別系列の成功を偽装しない。
6. USDJPY UIC 42／`FxSpot`／`bid_ask_mid`を固定し、自動的な代替symbolやForwardへ切り替えない。
7. `S6V5A_PRIORITY_INSTRUMENT_KEYS`という既存名・契約を無理にFX研究全体へ流用せず、必要なら汎用のscheduled FX universeを明示的に導入する。
8. README、collection spec、scheduler state schema、関連manifestを実装後の実態へ同期する。

#### 受入条件

- 次回FX slotに`eurusd`と`usdjpy`が表示される
- 連続2つの期限内hourly slotで両系列のrunがPASS
- 両系列のlatest completeが同じexpected-slot contractを満たす
- USDJPY freshness `PASS`
- restart／catch-up後もUSDJPYが欠落しない
- write request、precheck、orderは0

### R5. missing 405／406本を時刻単位で分類する

#### 修正要件

1. verified `SBFX_24X5` calendarと完成足規則から、各系列のexpected slotを再現する。
2. expected slotとcurated 1Hをanti-joinし、EURUSD 405本、USDJPY 406本のtimestamp一覧を生成する。
3. 各missing slotに少なくとも次のcause codeを付ける。
   - `CALENDAR_EXPECTATION_FALSE_POSITIVE`
   - `WEEKEND_OR_HOLIDAY_CLOSURE`
   - `DAILY_MAINTENANCE_BOUNDARY`
   - `SAXO_RAW_NO_SAMPLE`
   - `ACQUISITION_RUN_MISSED`
   - `RAW_PRESENT_CURATED_REJECTED`
   - `QUARANTINED_VALUE_ANOMALY`
   - `UNCLASSIFIED`
4. raw artifactとrun manifestが存在する期間は、rawにsampleがあるか、curatedだけで失われたかを照合する。
5. 年、月、weekday、UTC hour、New York local hour、主要相場急変日への集中を集計する。
6. EURUSDとUSDJPYの共通missingと片側だけのmissingを分離する。
7. 欠損値をmid、forward fill、別provider、反対側価格から補間しない。
8. calendar誤判定ならcalendar/versionを新しいmigrationで修正し、既存migrationを改変しない。
9. Saxo raw自体にbarがない場合はsource coverageとして保持し、無理にPASSへ変更しない。

#### 受入条件

- 405／406本すべてがtimestamp付きでaccounted for
- `UNCLASSIFIED=0`、または残件ごとにowner・必要証拠・block/nonblock判定が明記される
- duplicate 0を維持
- calendar修正が必要な場合、coverage／freshness双方で同一versionを使用
- 説明可能な履歴WARNは`ELIGIBLE_WITH_WARNINGS`を許容するが、freshness／quality FAILをcoverage WARNで隠さない

### R6. atomic series-statusと運用証跡を完成させる

#### 修正要件

1. EURUSD／USDJPYのidentity、coverage、freshness、quality、watermark、latest runを同一repeatable-read snapshotで返す。
2. `coverage_status=WARN`でも、blocking条件がなければ`ELIGIBLE_WITH_WARNINGS`を返せる既存契約を維持する。
3. `NOT_EVALUATED`をPASSへ昇格しない。
4. CURRENT／UNKNOWN ERROR・CRITICALが1件でも適用される場合はfail-closedにする。
5. scheduler state、DB watermark、Read API expected latest completeを同じcalendar contractへ揃える。
6. reportにはprovider/interface、operational、coverage、content qualityを別フィールドで示す。
7. 最終コードfreeze後にimplementation manifestのsize、SHA-256、test件数を機械的に更新する。

#### 受入条件

EURUSDとUSDJPYの両方が次を満たす。

```text
data_status = ACTIVE
freshness_status = PASS
quality_status = PASS
unknown_blocker_count = 0
current applicable ERROR/CRITICAL = 0
duplicate_rows = 0
eligibility_status = ELIGIBLE or ELIGIBLE_WITH_WARNINGS
```

## 5. 実装順序

次の順序を変更しない。

1. 現在state、atomic `series-status`、run manifest、quality eventをread-only保存
2. R1 retry stormのunit testを追加
3. R2 event scopeのunit／integration testを追加
4. R1／R2を実装し、過去eventを削除せずscope／supersessionを是正
5. R4 USDJPY scheduler追加とcalendar testを実装
6. manifestはまだ更新しない
7. 正規service identityを確認し、並行runを止めた状態でR3 reconcileを実行
8. R5 gap classificationを実行し、reportを保存
9. serviceを正規manager経由で再開
10. 連続2 hourly slotのruntime acceptanceを取得
11. R6 Read API照合
12. 最終コードfreeze後にmanifest、README、runbookを同期

## 6. Test要求

最低限、次を実行する。

```bash
cd /Users/tikeda/workspace/trade/saxo_db

.venv/bin/python -m pytest -q \
  tests/test_db3_unit.py \
  tests/test_periodic_update.py \
  tests/test_periodic_update_service.py \
  tests/test_read_api.py \
  tests/test_operator_ui.py

.venv/bin/python -m pytest

SAXO_DB_INTEGRATION=1 .venv/bin/python -m pytest

.venv/bin/python -m market_db.validate --phase db4
.venv/bin/python -m market_db.read_api_preflight --format json
```

追加必須test:

- terminal blockerは同一条件でretryされない
- watermark revision変化後はretry可能になる
- service restart後もblocked slot idempotencyを維持
- EURUSDだけのprecondition failureがUSDJPYへ波及しない
- 真のglobal failureは選択された全系列をblockする
- historical eventを保持したままcurrent blockerだけ解消できる
- FX slotがEURUSD／USDJPYの両方を含む
- 通常日、weekend、DST切替、17:00 New York maintenance
- 片系列失敗時のatomic rollback
- DataVersion変化、guard付きfull-refetch、連続2 PASS
- gap classificationの全cause codeと件数整合
- atomic `series-status` snapshot consistency

test PASSはruntime acceptanceの代替ではない。実データの連続hourly slot、freshness、watermark、raw artifactを別途確認する。

## 7. Runtime acceptance

### 7.1 実行前安全確認

- `git status`と対象diffを保存
- service managerの`managed=true`とprocess identityを確認
- authはstatusとfingerprintだけを表示し、token値を表示しない
- reconciliationとschedulerが並行しないことを確認
- backup／rollback pointとDB migration stateを確認

### 7.2 復旧run

- EURUSD controlled full-refetch/reconcile PASS
- canonical通常run連続2回PASS
- raw response、SHA-256、run manifest、DB run、watermark、quality eventを照合
- rejected／revision／removed rowを理由付きで報告

### 7.3 定期運転

- EURUSD／USDJPYを含むFX hourly slotを連続2回、期限内PASS
- catch-up PASSとdeadline内PASSを混同しない
- service restart後の次slotも両系列を含む
- 同一blockerのretry storm 0

### 7.4 consumer向け最終確認

```bash
for pair in eurusd usdjpy; do
  curl --fail --get 'http://127.0.0.1:8766/api/v1/series-status' \
    --data-urlencode "instrument_key=${pair}" \
    --data-urlencode 'layer=1h' \
    --data-urlencode 'price_basis=bid_ask_mid'
done
```

両responseについて、Section R6の受入条件を機械検証する。coverage WARNが残る場合は、R5 reportへの参照とwarning acceptance理由をresponse／manifestへ残す。

## 8. Rollback

1. migrationはdownward destructive rollbackを前提にしない。新旧schema互換を保ち、必要ならforward fixする。
2. raw response、run manifest、quality event、old DataVersion revisionを削除しない。
3. scheduler変更を戻す場合も、USDJPY取得済みraw／curated／watermarkを削除しない。
4. service停止・再開は正規managerとidentity確認を使用する。
5. full-refetch失敗時はtransaction rollbackを確認し、旧ACTIVE dataを手動で改変しない。
6. event scope migration失敗時はfail-closedを維持し、誤ってPASSを公開しない。

## 9. 禁止事項

- `docker compose down -v`、DB drop、volume削除
- watermark、quality event、curated rowの手動DELETE／直接修正
- DataVersion差の無視、raw上書き、lineage切断
- 欠損barの黙示的補間
- USDJPYを別symbol、Forward、外部providerへ自動代替
- token、refresh token、AppKey、口座識別子のlog／DB／manifest／Git保存
- identity不明processのkill、lock／state fileの強制削除
- test PASSだけでruntime PASSと宣言
- coverage WARNをfreshness／quality PASSへ読み替える
- `saxo_trading_fx`の戦略、WFO、Holdout、PnL、order logicを`saxo_db`へ実装

## 10. 成果物

最低限、次を提出する。

1. R1〜R6の実装diff
2. 追加・更新testと全test結果
3. DB migrationとchecksum
4. retry／event scopeの設計説明
5. EURUSD DataVersion reconciliation result
6. USDJPY scheduler onboarding result
7. EURUSD 405本／USDJPY 406本のgap classification CSVまたはJSON
8. gap classification summary Markdown
9. 連続2 hourly slotのruntime evidence
10. 修正後のatomic `series-status` response
11. raw／curated／watermark／manifest lineage照合
12. 更新したREADME、runbook、implementation manifest
13. 未解決事項と、provider側調査が必要な場合のraw evidence

## 11. 最終報告形式

最終報告は少なくとも次の表を含める。

| Gate | EURUSD | USDJPY | Evidence |
|---|---|---|---|
| Saxo interface | PASS/FAIL | PASS/FAIL | HTTP status / run manifest |
| Scheduler enrolled | PASS/FAIL | PASS/FAIL | slot state |
| DataVersion / watermark | PASS/FAIL | PASS/FAIL | watermark / lineage |
| Coverage | PASS/WARN/FAIL | PASS/WARN/FAIL | expected / aligned / missing |
| Freshness | PASS/STALE/FAIL | PASS/STALE/FAIL | expected / latest complete |
| Quality | PASS/FAIL | PASS/FAIL | applicable current blockers |
| Duplicate | PASS/FAIL | PASS/FAIL | duplicate rows |
| Atomic eligibility | ELIGIBLE/WARN/BLOCKED | ELIGIBLE/WARN/BLOCKED | series-status |
| Runtime consecutive slots | n/2 | n/2 | run IDs / deadlines |

最終statusは次のいずれかに限定する。

- `PASS_FX_DATA_GATE`
- `PASS_WITH_NONBLOCKING_COVERAGE_WARN`
- `BLOCKED_DATA_VERSION_RECONCILIATION`
- `BLOCKED_SCHEDULER_RUNTIME_EVIDENCE`
- `BLOCKED_DATA_QUALITY`
- `BLOCKED_INTERFACE`

複数層の状態を一つの`FAIL`へ潰さない。Saxo interfaceがPASSでもデータgateがBLOCKEDであること、coverage WARNでもfreshness／qualityがPASSなら非blockingであることを明示する。
