# FX追加3通貨ペア 実装計画

## 1. 目的と開始判定

既存EURUSDに加え、AUDUSD、USDCAD、USDCHFのFxSpot 1時間足を、Saxo OpenAPIから`saxo_db`の既存raw→curated→Read API経路で管理できるようにする。

事前試験では3ペアとも直近1,200本が`PASS_SAMPLE`だった。一方、全履歴、DB登録、Read APIは未評価である。本計画の開始判定は次のとおりとする。

- 設計・full-history取得試験: `GO`
- 定期取得・Read API current公開: `NO-GO`のまま
- 有効化条件: 3ペアすべてが本計画の全品質gateにPASSすること

1ペアでもFAILまたはUNKNOWNなら、候補3ペア全体の定期取得とcurrent公開を有効化せず、ペア別blockerを報告する。

## 2. 対象と禁止事項

### 対象identity

| Instrument key | Symbol | UIC | AssetType | Horizon | Price basis | Calendar |
|---|---|---:|---|---:|---|---|
| `audusd` | AUDUSD | 4 | FxSpot | 60分 | `bid_ask_mid` | `SBFX_24X5` |
| `usdcad` | USDCAD | 38 | FxSpot | 60分 | `bid_ask_mid` | `SBFX_24X5` |
| `usdchf` | USDCHF | 39 | FxSpot | 60分 | `bid_ask_mid` | `SBFX_24X5` |

### `saxo_db`の責任

- Saxo OpenAPI GET取得
- immutable raw body、request metadata、hash、run manifest
- 重複排除、正規化、curated生成
- coverage、freshness、quality、gap、DataVersion、watermark、lineage
- candidate schedulerの隔離実行、retry、運用ログ
- read-only Read API公開

### 対象外・禁止

- signal、strategy、WFO、Holdout、PnL、position
- precheck、order、口座操作、Saxo write request
- 欠損値の補間、forward fill、別provider fallback
- Bid/Askの交換、clamp、異常値の書換え
- DataVersionを跨いだcurrent系列の混在
- USDJPYの隔離解除、再取得、再評価
- 追加3ペアを既存EURUSDと同じatomic slotへ即時投入すること

## 3. 品質契約

品質判定はinstrument単位で行い、provider interface、acquisition operation、data quality、data not readyを混同しない。

### 3.1 Identityと取得契約

- instrument search、details、trading scheduleのsymbol、UIC、AssetTypeが凍結specと一致する。
- Chart requestはGET、`AssetType=FxSpot`、`Horizon=60`、明示UICだけを許可する。
- raw manifestにinstrument key、symbol、UIC、AssetType、environment、request mode、page countを記録する。
- identity driftは`BLOCKED_INSTRUMENT_DRIFT`としてDB更新前に停止する。

### 3.2 履歴coverage

- `ChartInfo.FirstSampleTime`は探索境界の参考値とし、coverage PASSの根拠には単独使用しない。
- `Mode=UpTo`と`Count=1200`でprovider boundaryまでpageを遡る。
- verified `SBFX_24X5` calendarと完成1H規則を用い、取得された最小・最大時刻間のexpected slotを再現する。
- weekend、holiday、New York 16:59–17:04 maintenance、provider非提供、取得漏れ、curated rejectを区別する。
- 全missing timestampを分類し、`UNCLASSIFIED=0`を必須とする。
- provider非提供の歴史gapは証拠付きWARNにできるが、価格は生成しない。
- `UNKNOWN`、curated reject未解決、取得漏れ未回収はblockする。

### 3.3 DataVersionとpaging

- 1回のfull-history runに含む全Chart pageのDataVersionを一致させる。
- page途中のDataVersion変化はrun全体をrollbackし、raw evidenceだけを保持する。
- SaxoのDataVersionが既存currentから変わった場合は、warning-only policyでrevision
  evidenceを隔離し、明示承認までcurrent curated、水位、派生を変更しない。自動
  full-refetchまたは自動停止を行わず、旧新DataVersionをcurrentで混在させない。
- `Mode=UpTo`のinclusive page境界重複はrawに保持し、curated mergeではrequest順のfirst-seen完成sampleを採用する。
- page duplicateの同値・不一致件数をmanifestへ記録する。

### 3.4 Completeness、uniqueness、order

- raw/curatedの必須timestampとBid/Ask OHLCはnull不可。
- curated grainは`instrument_id + horizon_minutes + time_utc + price_basis`で一意とする。
- 重複排除後の時刻はUTCでstrict increasingとする。
- future barと形成中barをcurrent完成watermarkへ含めない。

### 3.5 Bid/AskとOHLC

- Bid/Askの全Open、High、Low、Closeは正値とする。
- 各sideで`Low <= min(Open, Close) <= max(Open, Close) <= High`を満たす。
- midpointは同timestampのBid/Askから決定的に算出し、同じOHLC規則を満たす。
- Open/Closeの`Bid > Ask`はfatalとする。
- High/Low extremaの`Bid > Ask`は既存のbounded quarantine契約、最大10 unique rowかつ観測数の0.01%以下に限り、値を変更せず隔離できる。いずれか超過でrun全体をFAILする。
- 閾値は観測結果に合わせて緩和しない。

2026-07-28のユーザー承認により、AUDUSDの既知14件だけは一般閾値変更ではなく、
件数・期間・High/Low field・content fingerprintが完全一致する研究用例外として扱う。
rawを保持し、curatedから無補間で除外する。件数増加、範囲拡大、Open/Close異常、
別規則違反には適用しない。USDCAD/USDCHFはeffective coverage startを
`2010-06-18T00:00:00Z`とし、provider表示の2002年からの空白を補間しない。

### 3.6 Freshnessと完成足

- Saxoが返す最新sampleは形成中とみなし、終了時刻と現在時刻を照合して完成足だけを公開する。
- 現行EURUSDは各UTC時03分due、10分deadlineを維持する。
- 候補3ペアの定期化案は、実測所要時間とrate limitを確認した後、別slotで各UTC時06分due、15分deadlineを初期案とする。
- freshnessはverified calendar上の最新完成expected slotとcurrent watermarkの一致でPASS判定する。

### 3.7 Lineageと公開gate

manifestには次を含める。

- source=`Saxo OpenAPI`、environment=`SIM`
- instrument key、symbol、UIC、AssetType、horizon、price basis
- DataVersion、first/last sample、latest completed sample
- request/page count、各raw artifact path・size・SHA-256
- raw/accepted/rejected/duplicate/quarantine row counts
- coverage・gap分類artifactとSHA-256
- ingestion run ID、watermark revision、親子run関係
- order/precheck/write requestが0であること

## 4. 実装フェーズ

### FXA0: Candidate registryの追加

候補identityをspecへ登録するが、`enabled=false`、`publication_status=CANDIDATE`、`schedule_status=DISABLED`とする。

想定変更:

- `specs/source_collection/v13_db3_incremental_collection.json`
- `specs/instrument_reference_v1.json`
- 新規candidate onboarding spec
- append-only catalog migration
- instrument registry、catalog UI、MCP説明のtest

catalog migrationではprovider、environment、UIC、AssetTypeの一意性を検証する。登録だけでRead APIのcurrent eligibilityを与えない。

### FXA1: Discovery preflight

各ペアについてinstrument search、details、trading schedule、Chart H60をGET-onlyで再確認する。レスポンス本文をcredential-safeに保存し、identity mismatch、HTTP failure、JSON/schema failureをDB品質FAILとは別のinterface/operational errorとして扱う。

### FXA2: Full-history raw取得

- schedulerとは分離したonboarding jobを使う。
- AUDUSD→USDCAD→USDCHFの順に1ペアずつ実行する。
- provider boundaryまで`Mode=UpTo`をpage取得する。
- DB transaction開始前にraw responseとhashをimmutable保存する。
- full-history merge、DataVersion、page boundaryを検査する。
- 失敗したペアはfail-closedで停止し、他ペアの証拠を上書きしない。
- このフェーズではactive scheduler scopeを変更しない。

### FXA3: Curated・品質・gap gate

各ペアに対しraw→normalized→curatedを同一atomic runで行い、3章の契約をすべて評価する。gap詳細JSON/CSVとsummaryを生成し、全missing timestampを証拠に結びつける。

`market_db/fx_gap_report.py`は現在EURUSD/USDJPYの2ペア固定であるため、任意の明示instrument keysを受け取れるよう一般化する。

- hard-coded `FX_KEYS`をCLI/spec由来の明示setへ変更
- `eurusd_only_rows`、`usdjpy_only_rows`を削除
- per-instrument集計を主契約にする
- 2ペア以上の共通gap intersectionは任意の補助情報にする
- requested keyが未登録・データ0件ならfail-closed
- USDJPYを指定しない候補runがUSDJPYを照会しないtestを追加

### FXA4: Read API staging検証

catalog登録後もcurrent publish gateがPASSするまでは404または明示的非eligible状態を維持する。候補用staging検証で次を確認する。

- `GET /health`: PASS、DB roleはread-only
- `GET /api/v1/series-status`: pair別にatomicな状態を返す
- `quality_status=WARN`かつ承認済みresearch policyと`AVAILABLE_WITH_WARNINGS`が一致
- `freshness_status=PASS`
- `coverage_status`が`NOT_EVALUATED/UNKNOWN/FAIL`ではない
- `current_blockers=[]`
- `unknown_blocker_count=0`
- `GET /api/v1/bars`: UTC昇順、`bid_ask_mid`、cursor/snapshot/hashが安定
- manifestからUIC、DataVersion、raw hash、run、watermarkまで追跡可能

APIの既存hard-coded FX判定も監査し、EURUSD/USDJPYの集合に候補が暗黙除外されないよう、AssetTypeまたはcatalog metadataを正本にする。

### FXA5: 連続normal run受入

full-refetch PASS後、各候補について通常incremental runを2回連続で実行し、重複0、DataVersion安定、watermark前進、gap未分類0を確認する。これはcandidate-specificな受入であり、既存EURUSD/ETF slotを停止しない。

### FXA6: 条件付き定期取得・公開

FXA0–FXA5を3ペアすべてがPASSした場合だけ、次の有効化案を提示する。

- 新slot kind: `fx_research_candidates_hourly`
- instruments: `audusd, usdcad, usdchf`
- calendar: `SBFX_24X5`
- 初期案: due UTC minute 06、deadline minute 15
- atomicity: 1候補ペアごとのsingleton slot
- retry/terminal blocker: 失敗したcandidate instrumentだけへscope
- existing `fx_hourly` EURUSD: minute 03/10を変更しない
- ETF category slots: 変更しない
- USDJPY: active scopeに含めず、provider-quality quarantineを維持

候補slotを既存EURUSDと分離する理由は、候補のDataVersion・品質blockerがEURUSD current更新を停止させないためである。初期時刻はrate limitとfull/incremental実測時間を確認してからfreezeする。

## 5. テスト計画

### Unit

- 3ペアのsymbol/UIC/AssetType正規化
- Count 1200 paging、inclusive境界、first-seen merge
- DataVersion driftとrun rollback
- null、非正値、side OHLC、mid OHLC、Bid/Ask交差
- 形成中bar除外とUTC strict order
- arbitrary-key gap reportと`UNCLASSIFIED=0`
- USDJPY非選択・EURUSD/ETF非干渉
- candidate slotと既存slotのterminal blocker分離

### DB integration

- append-only migrationと一意制約
- raw artifact→source_file→revision→curated→watermark lineage
- 失敗runでcurrent curated/watermarkが不変
- Read API roleのread-only、timeout、snapshot consistency
- pair別`series-status`のblocker scope

### Live GET-only acceptance

- full-history 1回/ペア
- incremental normal run連続2回/ペア
- all page DataVersion、raw hash、row reconciliation
- Read API bars/series-status/manifest hash
- Saxo write/precheck/orderが0
- 既存EURUSDとETF11の次slotが従来どおりPASS

## 6. 合格表

| Gate | AUDUSD | USDCAD | USDCHF | 全体有効化条件 |
|---|---|---|---|---|
| Identity/UIC/AssetType/1H | PASS_SAMPLE | PASS_SAMPLE | PASS_SAMPLE | 3/3 PASS |
| Full-history raw | 未実施 | 未実施 | 未実施 | 3/3 PASS |
| DataVersion | 標本のみ | 標本のみ | 標本のみ | 3/3 stable |
| Coverage/gap | 未評価 | 未評価 | 未評価 | 3/3 no unknown blocker |
| Curated quality | 未評価 | 未評価 | 未評価 | 3/3 PASS |
| Freshness/watermark | 未評価 | 未評価 | 未評価 | 3/3 PASS |
| Read API | 409 | 409 | 409 | full-history gate後に3/3 STAGING |
| Normal run x2 | 未実施 | 未実施 | 未実施 | 3/3 PASS |

## 7. Retry・障害分類

- HTTP timeout、OAuth、rate limit、JSON/schema: interface/operational error。data-quality FAILにしない。
- transient error: bounded retryし、slot deadlineを超えたらcandidate slotをBLOCKEDにする。
- identity/DataVersion/content quality: terminal fail-closed。状態変化まで同一原因を連打しない。
- 最新bar未確定: `DATA_NOT_READY`。品質FAILにしない。
- gap/重複/非正値/OHLC不整合: data-quality blocker。
- 1ペアの失敗理由はinstrument scopeで記録し、EURUSD、ETF、USDJPYへ波及させない。

## 8. Rollback

- candidate schedule kindだけを停止し、既存EURUSD/ETF scheduleは維持する。
- raw、manifest、quality event、DataVersion evidenceは削除・上書きしない。
- database migrationはforward-onlyとする。
- current publish eligibilityをappend-only状態変更で解除し、旧snapshotを改変しない。
- 不完全runではtransaction rollbackによりcuratedとwatermarkを不変にする。
- USDJPY隔離状態には一切触れない。

## 9. 実装順序と報告成果物

1. candidate spec・catalog migration・identity test
2. gap reportとhard-coded FX判定の一般化
3. pair別full-history immutable raw取得
4. curated・品質・gap・lineage gate
5. staging Read API検証
6. pair別normal run連続2回
7. 全3ペア合格時だけscheduler/Read API有効化案を最終提示

各段階で、変更ファイル、run ID、DataVersion、期間、row counts、quality結果、gap分類、Read API結果、テスト、orders/prechecks/write=0、利用可能またはblock理由を報告する。

## 10. 現在のGo/No-Go

`GO: 設計、candidate登録実装、full-history取得試験`

`NO-GO: 定期取得有効化、current Read API公開、consumer利用可能判定`

FXA0のmigration・registry・publication gate、FXA3のgap一般化、FXA4のstaging API gate、FXA6のdormant singleton scheduler profileは実装済み。現在はFXA2のlive全履歴取得が`AUTH_CONFIG_MISSING`で開始前停止しており、候補バー・watermarkは未作成である。候補3ペアが全品質gateに合格するまでは、active scheduler scopeを変更しない。
