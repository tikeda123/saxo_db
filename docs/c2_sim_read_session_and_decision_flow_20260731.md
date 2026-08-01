# C2 SIM Read短命session・外部provider決定・運用gate手順

作成日: 2026-07-31
対象: C2外部データ契約 EDC-01/02/05/06/07/08/09/10
現在状態: **AUTH_READY / SIM_OBSERVATION_START_READY / NO C2 SAXO GET EXECUTED**

## 1. 結論

初回のSaxo SIM OAuth認証後にrefresh credentialをmacOS Keychainだけへ保存し、access tokenを**プロセスメモリ内だけ**で自動更新する。既存の15分以下の短命session coreでcapability、account currency、11 ETF instrument reference、11 ETF atomic InfoPriceをGETし、秘密情報を含まないreceipt候補へ変換する。

初回OAuthは利用者操作により`AUTH_READY`まで完了した。初回SIM観測は、利用者がSIM限定・trading disabledを確認して画面の開始ボタンを押した場合だけ、15件のGET-only技術観測を行う。provider、official close、total-return、SLA、fee、distribution、receipt登録は初回観測の開始条件にしない。

今回の実装・検証中は、Saxo API呼出し、DB receipt登録、scheduler、refetch、注文、precheck、取消、資金・口座操作を行わない。

## 2. 段階管理

| 段階 | 成果物 | 現在状態 |
|---|---|---|
| 1. SIM Read session | ephemeral core + `c2_saxo_sim_oauth_keychain_v1` | 実装済み。`AUTH_READY` |
| 2. `SIM_OBSERVATION_START` | 明示クリックによる15 GET技術観測 | `READY`。provider/gate未決定でも開始可能 |
| 3. `SIM_ALLOCATION/PAPER_EVALUATION` | current total-return / official-close / quote / fee / distribution / SLA / receipt | `DECISION_REQUIRED` |
| 4. `LIVE_ORDER_ELIGIBILITY` | 注文・precheck・account操作 | `PROHIBITED` |

未決定templateはallocation、PnL、paper evaluationへ進む前にfail-closedする。nullを初期値や推奨値として解釈しない。ただし初回OAuth、refresh chain維持、`SIM_OBSERVATION_START`は先行できる。初回観測結果をprovider承認、current-data readiness、receipt acceptance、注文能力へ昇格させない。

## 3. 認証受領contract

machine-readable正本は`specs/c2_saxo_sim_ephemeral_read_session_v1.json`である。

現在の認証・gate・UI実行準備結果は
[`c2_sim_read_auth_execution_readiness_20260731.md`](c2_sim_read_auth_execution_readiness_20260731.md)
を参照する。readiness専用CLIは次で、OAuthやSaxo GETを開始しない。

```bash
.venv/bin/python -m market_db.c2_sim_read_readiness status
```

### 認証供給

- 初回PKCE後にmacOS Keychainへ保存したrotating refresh credential
- process memory内access tokenとUTC expiry
- raw identifierを保存しないKeychain保護HMAC account binding
- userが`ACCEPTED`にした運用gate document

### 禁止する入力・保存

- tokenをcommand line、環境変数、repository file、receipt、logへ渡すこと
- refresh token、Authorization header、AccountKey、ClientKeyのreceipt化
- raw account/balance/quote responseの保存
- access tokenの手動paste、環境変数、argv、browser storage

Pythonのimmutable stringを完全にzeroizeできないため、session終了時にclient/token参照を解放し、session自体を15分以下に制限する。tokenを呼出し元がlog、例外、argvへ出さないことも契約の一部である。

## 4. 利用者の明示開始後に1回実行するGET-only技術観測

1. `AUTH_READY`、SIM endpoint固定、kill switch OFF、same-origin/CSRF、利用者の`SIM_APP_TRADING_DISABLED_GET_ONLY`確認を検証する。provider/gate文書は初回観測のpreconditionにしない。
2. token残存時間5秒超、session上限900秒を確認する。
3. `GET /root/v1/sessions/capabilities`でdata capabilityを確認する。trade capabilityは使用しない。
4. `GET /port/v1/accounts/me`と`GET /port/v1/balances/me`でaccount identity fingerprint、currency/decimalsの形式を確認する。account/client identifierとbalance amountは出力しない。accepted base currencyやfee tierは判定しない。
5. canonical 11 ETFについてinstrument detailsをGETし、UIC、AssetType、symbol、currency、exchange identityを照合する。tradability、minimum、quantity ruleは後続gateで扱い、初回観測を止めない。
6. 11 UICを一つの`GET /trade/v1/infoprices/list`へ渡す。response内UIC set完全一致、重複0を検査する。`Quote.ErrorCode`、`PriceTypeBid/Ask`、`MarketState`、`InstrumentPriceDetails.IsMarketOpen`を価格fieldより先に解釈する。C2は四半期リバランスの低頻度用途なので、`Mid`、`Bid`、`Ask`の少なくとも一つが正値ならreference priceとして扱い、二方向Bid/Askやreal-timeを要求しない。両sideがある場合だけ`ask >= bid`を検査する。`Indicative`と`DelayedByMinutes > 0`は正常な低頻度観測値である。`NoAccess`、`NoMarket`、`Pending`、閉場などproviderが価格未提供を明示した場合は`PASS_WITH_WARNINGS`とし、通常監視・低頻度paper評価は日次終値fallbackへ切り替える。提供された値の非正値、crossed、identity不一致だけをdata-quality FAILとする。
7. 価格値やraw responseは返さず、account currency、11 instrument/quote件数、PriceType/Source、delay/age/span、request/write counterだけをsanitized resultとして表示する。画面再読込後も最終1件を確認できるよう、状態、実行回数、時刻、sanitized resultだけをowner-only・atomicな`.runtime/c2/sim_observation_status.json`へ保存する。raw response、token、account/client identifier、account fingerprint、価格・残高値、receipt、DB rowは保存しない。
8. sessionをcloseし、client/token/fingerprint key参照を解放する。
9. receipt化・登録が後続段階で承認された場合だけ、別stepで再観測・schema検証・append-only登録を行う。初回観測結果を自動登録しない。

自動登録を初回観測に含めないのは、技術接続確認とdata contract acceptanceを分離するためである。

Historical TransactionsはGET allow-list済みだが、今回のatomic reference/quote runには含めない。配当cash訂正lookbackが`ACCEPTED`となり、明示date rangeとpagination上限が確定した後に、独立receipt runとして実行する。

## 5. 状態分類

| 事象 | 状態 | data-quality扱い |
|---|---|---|
| token expired、401/403、network、capability/account access不能 | `BLOCKED_INTERFACE_OPERATIONAL` | しない |
| provider、official close、total-return、SLA、fee、distribution、receipt未承認 | 初回観測は継続。後続段階を`DECISION_REQUIRED` | しない |
| quote遅延、age、atomic span | 初回観測では観測値/warning。後続承認gate超過時のみ`DATA_NOT_READY` | しない |
| UIC欠落/重複、invalid timestamp、非正値、Bid > Ask、instrument identity drift | `FAIL_DATA_QUALITY` | する |

初回観測の失敗時はsanitized status、error code、endpoint、request/write counterだけをprocess memoryに返す。receipt候補を作らず、例外本文、response body、token、account identifierは保存しない。既存のlast-good accepted receiptは更新・削除しない。

### 2026-07-31 `QUOTE_BID_INVALID` 調査

初回観測1回は15 GETまで実行され、旧validatorが`QUOTE_BID_INVALID`を返した。write、DB/receipt、注文・precheckは0である。raw responseと価格値は安全契約どおり保存していないため、旧実行のBidが欠落・0・非数値のどれだったか、個別の`PriceTypeBid`が何だったかは復元しない。

根本原因は、旧validatorが`Quote.ErrorCode`、`PriceTypeBid/Ask`、`MarketState`、`InstrumentPriceDetails.IsMarketOpen`より先にBid/Askを正値必須としていたことである。Saxo公式のPriceQualityには`NoAccess`、`NoMarket`、`Pending`があり、その場合は価格が提供されないことがある。`OldIndicative`は閉場時等の古い有効価格である。InfoPriceは非取引価格であり、feed権限によりno data、delayed、real-timeが変わる。観測結果のruntime捕捉は04:43Z（00:43 ET）で、米国ETF市場のEarly Session開始前だった。捕捉時刻は実行終了時刻ではないが、閉場による通常のquote unavailableと整合する。したがってこの旧結果は実価格異常の証拠ではなく、quote availabilityの実装上の誤分類と判定する。

修正版は次のように分離する。

- exact UIC set、重複、instrument identity、response object/Quote shapeはfail-closed。
- providerが提供した`Mid`/`Bid`/`Ask`の非正値、両sideがある場合のcrossed quote、invalid UTCはdata-quality FAIL。
- `NoAccess`、`NoMarket`、`Pending`、明示的なquote error、closed marketで価格sideがない場合、初回の技術観測は`PASS_WITH_WARNINGS`。銘柄、欠落side数、provider状態だけをsanitized表示する。
- `OldIndicative`、delayed quoteは初回観測ではwarning。allocation/PnL・paper evaluationの後続gateでは別途受入判定する。
- providerが利用可能と宣言した場合も、正値の`Mid`/`Bid`/`Ask`が一つあれば低頻度referenceとしてwarning付きで利用できる。三つともない場合だけFAIL。
- 旧履歴は書き換えず`FAILED`のまま保持する。再実行は自動化せず、利用者の明示クリックだけで行う。

公式根拠: [Saxo Pricing](https://www.developer.saxo/openapi/learn/pricing)、[PriceQuality schema](https://www.developer.saxo/openapi/referencedocs/trade/v1/infoprices/get__trade/schema-pricequality)、[InfoPrice Quote schema](https://www.developer.saxo/openapi/referencedocs/trade/v1/infoprices/get__trade/schema-quote)、[NYSE取引時間](https://www.nyse.com/markets/hours-calendars)。

### 通常取引時間内の再観測結果

利用者が明示承認した再観測は米国通常取引時間中に1回だけ実行され、`SUCCEEDED / PASS_WITH_WARNINGS`、GET 15、write/DB/receipt/order 0となった。ETF11 identityは一致したが、11件すべてが`PriceType=NoAccess`でBid/Ask未提供だった。これにより旧結果で未確定だった原因は、市場閉場ではなく現在のSIM利用者・applicationにおける米国ETF OpenAPI price-feed entitlement不足と判定できる。

初回技術観測は完了であり、追加再試行は不要である。C2は1時間ごとの遅延`Indicative`価格またはsaxo_dbの正規日次終値を使い、`NoAccess`では日次終値fallbackを選ぶため、低頻度SIM allocation/paper評価全体を止めない。App Keyのtrading disabledやRead権限は変更せず、Developer PortalでのApp Key再作成を対処にしない。詳細な公式根拠、代替PriceType/endpointの評価は[`c2_sim_quote_noaccess_resolution_20260731.md`](c2_sim_quote_noaccess_resolution_20260731.md)を参照する。

## 6. provider決定入力

Operator UIの「3. provider / 運用gate決定」から判断する。保存ボタンを押した時だけ、`specs/c2_external_provider_decision_template_v1.json`を正本としてGit管理外の`.runtime/c2/provider_decision.json`へowner-only・atomic writeする。値はbrowser storage、DB、Gitへ保存しない。

- `SIGNAL_TOTAL_RETURN_DAILY`: 11 ETF adjusted total-returnの定義、分配金、revision、coverage、SLA、lineage、content identity、license/redistribution
- `VALUATION_PRICE_DAILY`: primary-listing-exchange official closeの定義、venue、currency、revision、coverage、SLA、lineage、content identity、license

`APPROVED`に必要なfieldが一つでも空ならvalidatorは`C2_PROVIDER_DECISION_EVIDENCE_MISSING`で拒否する。今回providerの選定・契約・外部取得はしていない。

UIは各roleに「保留」「証拠付き承認」「不採用」を表示する。承認者、server生成UTC時刻、判断根拠を記録し、証拠付き承認ではprovider legal name、source contract、license/redistribution、definition、coverage、SLA、revision、lineage、content identityをすべて必須にする。

以下は`SIM_ALLOCATION/PAPER_EVALUATION`段階の推奨であり、初回SIM観測をブロックしない。

- `SIGNAL_TOTAL_RETURN_DAILY`: 11 ETF、利用許諾、訂正履歴、point-in-time identity、SLAを一契約で証明できるlicensed providerを選定する。それまでは保留。既存Yahoo snapshotをcurrentへ昇格しない。
- `VALUATION_PRICE_DAILY`: primary listing exchange official closeを明示するlicensed sourceを選定する。それまでは保留。Saxo Chart 1Dと発行体ページはparity evidenceに限定する。

## 7. 運用gate決定入力

同じOperator UIから、`specs/c2_external_operational_gate_decision_template_v1.json`を正本としてGit管理外の`.runtime/c2/operational_gate_decision.json`へowner-only・atomic writeし、次を決定する。

- account context: accepted base currency、SIM固定、11 ETF全件必須
- low-frequency price: 通常cadenceは1時間遅延または日次終値、最大age/span 90,000秒、最大delay 60分、`Indicative`許容、二方向Bid/Ask不要
- fee: `UNKNOWN`でconsumerを止めるか、SIM研究限定warningを許容するか
- distribution: issuer revision lookback営業日、cash correction lookback暦日、negative-event state必須性
- SLA: role別最大lag秒、late/interface/data-qualityの状態分類

`status=ACCEPTED`でも、数値、方針、承認者、承認時刻が不足すれば実行しない。

低頻度C2の現在提案はprice age/span 90,000秒以下、delay 60分以下、`Indicative`/`Tradable`許容、SIM delayed許容、二方向Bid/Ask不要、fee `UNKNOWN`はSIM研究warningである。これは四半期リバランスのpaper評価用であり、実約定確認やLIVE注文の基準へ流用しない。pure SIM `NoAccess`では日次終値fallbackを使う。

## 8. UIの初回SIM観測開始

UIは、`AUTH_READY`、SIM/trading disabledの利用者確認、kill switch `OFF`を開始条件として表示する。計画はallow-list 15 GETであり、write、注文、precheck、取消、資金・口座変更、raw保存、DB receipt登録、periodic開始を含めない。

開始actionはloopback、same-origin、CSRF、no-store固定である。確認checkboxと開始ボタンを利用者が操作した場合だけ`POST /api/c2/sim-read/observe`を1回実行する。自動開始、page load時開始、provider decision保存による連動開始はしない。

画面は観測実行状態を`IDLE / READY / RUNNING / SUCCEEDED / FAILED`で表示し、`GET /api/c2/sim-read/observation`から最終1件のsanitized runtime監査を再読込する。`READY`は再実行可能性であり、過去の成功・失敗を意味しない。過去の結果は別の観測実行状態・実行回数・最終結果欄で確認する。

## 9. 実装・テスト対象

- `market_db/c2_sim_read_session.py`
- `market_db/c2_external_decisions.py`
- `specs/c2_saxo_sim_ephemeral_read_session_v1.json`
- `specs/c2_external_provider_decision_template_v1.json`
- `specs/c2_external_operational_gate_decision_template_v1.json`
- `specs/c2_sim_read_operator_input_contract_v1.json`
- `market_db/c2_sim_read_readiness.py`
- `tests/test_c2_sim_read_session.py`
- `tests/test_c2_external_decisions.py`

unit testは、正常13 receipt、認証失敗のinterface blocker、11 ETF欠落のdata-quality failure、未承認gateでclient生成/API 0件、token/account/`TradableOn`/balance非露出、write counter 0、既存receipt schema照合を検証する。C2 session、decision、既存external contract/receipt、Saxo GET client、migration、Read APIを含む関連回帰は`122 passed`である。

## 10. 安全境界

今回の実行counter: Saxo GET 0、OAuth 0、DB write 0、receipt registration 0、scheduler change 0、refetch/backfill 0、order/precheck/cancel/account/fund mutation 0。

Strategyのシグナル、WFO、PnL、allocation、注文判断は対象外である。
