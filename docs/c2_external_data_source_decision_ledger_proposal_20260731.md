# C2外部データ正規提供元・決定台帳案

作成日: 2026-07-31
対象bundle: `c2_strategy_external_data_contract_v1`
対象universe: SPY / IWM / EFA / EEM / VNQ / SHY / IEF / TLT / TIP / LQD / GLD
状態: **PROPOSAL / USER_DECISION_REQUIRED**

## 1. 目的と作業境界

本書は、C2が使用する外部データroleごとに、正規提供元の候補、採用推奨、受入証拠、ユーザーが決める事項を一つの台帳に固定するための提案書である。`saxo_db`は取得、品質、lineage、receipt、Read APIを所有し、Strategy Analysisは受け取ったデータを消費する。

この調査では、repository内の仕様・manifestと公開一次資料だけを読んだ。次は実施していない。

- Saxo OAuthまたはSaxo API呼び出し
- 市場値・口座値・外部データの取得
- provider契約、資格情報の追加、外部送信
- production DBの参照・書込み、migration、receipt登録
- Read APIへのlive GET、service再起動、scheduler変更
- 注文、precheck、口座・資金・session capabilityの変更

したがって、本書はsource採用の決定案であり、migration 0034の適用状態、Read APIの稼働、accepted receipt件数を証明しない。同日作成の既存資料には「Read API確認済み」と「connection refusedで未確認」が併存するため、live運用状態は**未確認**として分離する。

## 2. 結論

推奨するsource-of-record構成は次のとおりである。

1. current signal total returnとofficial valuation closeは、11 ETF、訂正履歴、point-in-time identity、利用許諾、SLAを同時に証明できる**licensed market-data provider**を選ぶ。特定vendorは未選定であり、候補名だけで採用済みにしない。
2. common calendarはNYSE ArcaとNasdaqの公式calendarを正本にし、11銘柄共通sessionは両市場のregular sessionのintersectionとしてversion化する。Saxo trading scheduleはbroker-side parity evidenceであり正本にしない。
3. distribution declarationはETF発行体の公式公表、口座へ実際に計上されたcash/correctionはSaxo Historical Transactionsを正本にする。宣言と入金を一系列に混同しない。
4. instrument/account context、proposal quote、currency quantumはSaxoのread-only responseを正本候補にする。ただしquoteはInfoPriceであり、取引可能価格ではない。遅延・品質・ageの受入規則を先に決める。
5. fee estimateはaccount-specificな公式fee schedule、actual feeはbooked transactionを正本にする。Saxo公式資料はInfoPriceでcommission取得が非対応としているため、InfoPriceをfee estimateの正本にしない。
6. revision/SLAは独立データ源を設けず、各accepted receiptのsource versionと時刻から導出する。provider未選定のまま数値SLAをPASSにしない。

この構成でも、provider・ライセンス・口座context・数値SLAをユーザーが承認し、後続のread-only receipt検証が完了するまで、該当roleは`BLOCKED_EXTERNAL_CONTRACT`または`NOT_EVALUATED`のままである。

## 3. 証拠の評価基準

| 観点 | accepted receiptで必要な証拠 | 未充足時の扱い |
|---|---|---|
| completeness | 11銘柄、必要期間・session、必須field、pagination完了 | `BLOCKED_EXTERNAL_CONTRACT_COVERAGE` |
| uniqueness | provider record ID、session date、instrument keyの一意性 | `DATA_QUALITY_BLOCK` |
| validity | price/amount/date/currency、bid/ask、枚数・小数桁の契約適合 | `DATA_QUALITY_BLOCK` |
| integrity | provider identity、version、raw/normalized lineage、content hash | `BLOCKED_EXTERNAL_CONTRACT_LINEAGE` |
| timeliness | published/observed/available/accepted/expected-byの比較 | `NOT_EVALUATED_SLA`または`STALE` |
| rights | 使用、保存、内部Read API提供、監査保持の許諾 | `BLOCKED_EXTERNAL_CONTRACT_LICENSE` |

endpointやschemaの存在は「取得候補」の証拠であり、対象口座の権限、値の完全性、ライセンス、SLAを証明するaccepted receiptではない。

## 4. role別source候補・採用推奨

### 4.1 EDC-01 current signal total return / EDR-01

| 項目 | 内容 |
|---|---|
| candidate A | 11 ETFのmarket-price adjusted total-return、corporate action、revision identityを一契約で提供するlicensed market-data provider |
| candidate B | Saxo unadjusted close + issuer distribution/splitから自前計算 |
| candidate C | 既存Yahoo Finance adjusted closeの研究snapshot |
| 採用推奨 | **A**。provider名はRFI証拠が揃うまで未決定。Bは再投資時点、税・端数、訂正、公式終値の定義を新たに所有するため第一候補にしない。Cは`SIM_RESEARCH_ONLY`かつ再配布・production-feed非保証なのでcurrent運用receiptへ昇格させない |
| repository evidence | `manifests/etf11_source_dataset_manifest.json`はCを「research snapshot only」、source adjusted closeを100へ正規化、dividend/splitを二重加算しないと記録する。`specs/total_return_full_history_research_contract_v1.json`は固定研究向けでfreshness不要とする |
| 受入条件 | 11 ETF完全coverage、adjustment/return definition、point-in-time version、訂正履歴、source/ordered-content hash、保存・内部提供権、publication SLA |
| ユーザー決定 | D-01: licensed providerを調査・契約するか。D-02: provider選定までC2 current signalをBLOCKEDに維持するか |

### 4.2 EDC-02 official valuation close / EDR-02

| 項目 | 内容 |
|---|---|
| candidate A | primary listing exchangeのOfficial Closing Priceを含むlicensed feed |
| candidate B | issuerの公式fund pageに掲載されるprimary-exchange close |
| candidate C | Saxo Chart `Horizon=1440`のdaily OHLC |
| 採用推奨 | **A**。Bは独立parity evidence、Cはbroker OHLC parity evidenceに限定する |
| official evidence | NYSE ArcaのOfficial Closing Priceはclosing auction、eligible last sale、prior close等の規則で決定される。単純な「最後のbar」と同義ではない。State StreetのSPY pageもClosing Priceをprimary listing exchangeのofficial closeと定義する |
| Saxo evidence | Chart APIはUIC/AssetTypeのtimestamped OHLCと1440分horizonを提供するが、primary-exchange official closeであるとは記載していない |
| 受入条件 | ticker/listing venue/date/official-close definition、all 11 coverage、currency、version、published/available timestamp、Saxo parityを使う場合の不一致判定 |
| ユーザー決定 | D-03: valuation markを`UNADJUSTED_PRIMARY_EXCHANGE_OFFICIAL_CLOSE`へ固定するか。D-04: 取得providerと不一致時の停止単位を決める |

### 4.3 EDC-03 common calendar / EDR-03

| 項目 | 内容 |
|---|---|
| candidate A | NYSE/NYSE Arca公式hours・holiday・early-close + Nasdaq公式calendar |
| candidate B | Saxo instrument trading schedule |
| 採用推奨 | **A**を正本、Bをbroker-side parity/halt evidenceにする |
| official evidence | NYSE Arca core sessionは9:30–16:00 ET。NYSEとNasdaqは2026年のholidayと13:00 ET early-close日を公表する |
| normalization | venue別intervalを保持し、共通calendarは11銘柄が同時にregular sessionであるUTC intersection。timezone database version、source URL、published time、normalized hashを固定する |
| 受入条件 | XNYS/ARCXとXNASの対象年完全coverage、DST、holiday、early close、臨時休場/訂正、Saxo schedule mismatch event |
| ユーザー決定 | D-05: intersection規則を承認するか。D-06: official calendarとSaxo scheduleが不一致のとき該当sessionだけをHALTするか |

### 4.4 EDC-04/05 distribution declaration and cash / EDR-05

| 項目 | 内容 |
|---|---|
| declaration candidate | iShares公式product/distribution資料、State Street公式ETF distributions、Vanguard公式VNQ distributions、必要時primary-exchange corporate-action notice |
| actual cash candidate | Saxo Historical Transactions `GET /hist/v1/transactions` |
| 採用推奨 | **issuer declaration + Saxo booked cashの二層**。宣言額と口座入金額は税、currency conversion、訂正等で異なり得るため別receiptにする |
| official evidence | State Streetはex/record/payable dateとdistribution amountを公表し、Vanguard VNQ pageも$/share、ex/record/payable dateを公表する。Saxo transaction schemaはCorporateActionId、CorrectionReason、CurrencyDecimals、BookedAmountを持ち、corporate-action correctionは同一CorporateActionIdに含まれる |
| 受入条件 | issuer source revision/as-of、distribution type、gross per share、currency、dates。cashはaccount fingerprint、booking/corporate-action ID、booked amount、tax/conversion、reversal/correctionを保持 |
| 注意 | GLDの既存Yahoo snapshotでdividend eventが0でも、将来の宣言がないことの証明にはならない。issuer一次資料のnegative/changed stateをreceipt化する |
| ユーザー決定 | D-07: issuer source一覧と訂正監視を承認するか。D-08: SIMまたはLiveのどちらのaccount booked cashをC2 contextにするか |

### 4.5 EDC-06 instrument/account context / EDR-06

| 項目 | 内容 |
|---|---|
| candidate | Saxo Instrument Details、Trading Schedule、Accounts/Balances、Session Capabilitiesの**GETのみ** |
| 採用推奨 | C2のenvironmentと一致する一つのSaxo account contextを正本にする。SIMとLiveを同一bundle/receiptで混在させない |
| official evidence | Instrument APIはuser access rightsで制限されたuniverse、specific UIC/AssetType details、trading scheduleを返す。Account/Balanceはaccount currencyとCurrencyDecimalsを返す。Session Capabilities GETは認証・data/trade levelを返す |
| 受入条件 | environment、匿名化account fingerprint、11 UIC/AssetType/listing/currency、TradableOn/eligibility、market state、session capability、source observation time |
| 安全境界 | capabilityのPUT/PATCHは他sessionのdata levelを下げ得るため、本契約の検証では使用しない |
| ユーザー決定 | D-09: C2 contextをSIMまたはLiveのどちらに固定するか。D-10: account変更時に旧receiptを失効させる規則を承認するか |

### 4.6 EDC-07 proposal quotes / EDR-04

| 項目 | 内容 |
|---|---|
| candidate | Saxo InfoPrice GET/list |
| 採用推奨 | C2の非発注proposal向けbid/ask・spread観測に採用する。official close、execution price、order validationには流用しない |
| official evidence | SaxoはInfoPriceをnon-tradableと明記する。QuoteにはBid/Ask、size、DelayedByMinutes、PriceTypeBid/Ask、PriceSource、market stateがあり、feed subscriptionによりno data/delayed/realtimeが変わる |
| 受入条件 | error none、positive bid/ask、ask >= bid、LastUpdated、PriceSource、PriceType、DelayedByMinutes、market state、11銘柄snapshot span。account/session capabilityも同じreceipt chainに固定する |
| proposed numeric policy | current proposalは`DelayedByMinutes=0`、quote age 5秒以下、11銘柄のfirst-last observation span 5秒以下を初期案とする。これはprovider SLAではなくユーザー承認待ちの運用値 |
| ユーザー決定 | D-11: 上記age/spanを採用するか。D-12: SIMでrealtimeが得られない場合、`SIM_ONLY_AVAILABLE_WITH_WARNINGS`を許すか、proposalをBLOCKEDにするか |

### 4.7 EDC-08 fees / EDR-07

| 項目 | 内容 |
|---|---|
| estimate candidate A | 対象account tier、venue、instrument、amountに適用されるSaxo公式fee/commission schedule |
| estimate candidate B | Saxo InfoPrice commission field |
| actual candidate | Saxo Historical Transactionsのbooked cost/booking details |
| 採用推奨 | **Aをestimate、actual transactionをactual**。Bは不採用。Saxoのofficial learning pageがInfoPriceでcommission取得は非対応と明記する |
| 2026-07-31確認 | Saxo公式の一般料金ページはHTTP取得・content SHA-256固定済み。ただし口座tierへの適用条件を証明しないためestimateとして未受入。actual transaction GETも現processの認証未設定により未観測 |
| 受入条件 | schedule revision/effective dates、account tier、venue、side、amount、minimum、currency、tax/FX、rounding。actualはbooking ID、cost class/subclass、amount/currency、correction |
| failure policy | 未確定feeは`UNKNOWN`。0、前回値、一般公開の最安feeへ置換しない |
| ユーザー決定 | D-13: account-specific official scheduleを提供・承認するか。D-14: C2でfee estimateがUNKNOWNならP9をBLOCKEDにするか |

### 4.8 EDC-09 currency and amount quantum / EDR-09

| 項目 | 内容 |
|---|---|
| candidate | Saxo account metadata/balance + Instrument Details + Historical Transactions |
| 採用推奨 | account currency/decimalsはaccount metadata、instrument currency/display/minimum/amount rulesはinstrument details、実際のroundingはbooked transactionでparity確認する |
| official evidence | Saxo BalanceはCurrencyとCurrencyDecimalsを返す。Instrument Detailsはcurrency、display/format、minimum/trade関連fieldを提供する候補である |
| 受入条件 | account currency、currency decimals、instrument trading currency、quantity type、fractional eligibility、minimum amount/value、rounding version、account fingerprint |
| failure policy | repositoryの`USD` mappingだけからaccount base currency、整数株、minimum、decimalsを推定しない |
| ユーザー決定 | D-15: account metadataを正本とするか。D-16: fractional未確認時は整数数量に狭めるか、全proposalをBLOCKEDにするか |

### 4.9 EDC-10 revision and SLA / EDR-10

| 項目 | 内容 |
|---|---|
| candidate | EDC-01〜09のaccepted receiptに含まれるprovider versionとpublished/observed/available/accepted timestamp |
| 採用推奨 | 独立providerを追加せず、role別receiptから導出する。訂正は旧receiptを更新せず、`supersedes_receipt_id`を持つappend-only receiptにする |
| state separation | interface/operational error、data quality failure、not ready、stale、revision pending、source scopeを別fieldで返す |
| proposed SLA draft | total-return: 次のC2 daily cycle開始30分前、official close: 16:15 ET、calendar: 有効日の30日前かsource公表後1営業日、declaration: issuer公表後1営業日、booked cash: booking後1営業日、quote: D-11、reference/quantum/fee: run開始前かsource revision後1営業日 |
| 注意 | 上記時刻はprovider保証の証拠ではない。provider契約とC2 run時刻を決めた後に確定する |
| ユーザー決定 | D-17: role別numeric SLAを採用・修正するか。D-18: late/revision時の該当role/該当instrument単位のHALTを承認するか |

## 5. 決定台帳

`status`はD-05だけ2026-07-31の運用指示と公式source hash検証により`ACCEPTED_WITH_WARNING`へ更新した。他は未決定である。

| Decision | 優先度 | ユーザーが決めること | 推奨案 | status | 未決定時の影響 |
|---|---:|---|---|---|---|
| D-01 / EDR-01 | P0 | current total-return providerの調査・契約を進めるか | 11 ETF、official close、revision、SLAを一契約で証明できるlicensed providerをRFI選定 | PROPOSED | current signal BLOCKED |
| D-02 / EDR-01 | P0 | provider決定までの扱い | 既存Yahoo系列は研究専用のまま。currentへ昇格しない | PROPOSED | 誤ったcurrent利用の危険 |
| D-03 / EDR-02 | P0 | valuation price basis | primary listing exchange official close | PROPOSED | valuation/NAV BLOCKED |
| D-04 / EDR-02 | P0 | official close source・不一致policy | licensed official-close feed。不一致は該当instrument/sessionのみHALT | PROPOSED | valuation/NAV BLOCKED |
| D-05 / EDR-03 | P0 | common calendar規則 | ARCX/XNAS regular-session intersection | ACCEPTED_WITH_WARNING | `SOURCE_PUBLISHED_AT_NOT_EXPOSED`をconsumerへ公開 |
| D-06 / EDR-03 | P1 | calendar mismatch policy | 該当sessionのみHALT、全service停止にしない | PROPOSED | 誤ったsession利用 |
| D-07 / EDR-05 | P1 | declaration source | issuer official source + append-only correction | PROPOSED | distribution declaration BLOCKED |
| D-08 / EDR-05 | P0 | booked cashのaccount context | C2で選んだ単一environment/account | PROPOSED | actual cash BLOCKED |
| D-09 / EDR-06 | P0 | account environment | C2がSIM demoならSIM。Liveは別bundle | PROPOSED | account roles BLOCKED |
| D-10 / EDR-06 | P1 | account変更時policy | 新receipt承認まで旧contextをAVAILABLEにしない | PROPOSED | context混在の危険 |
| D-11 / EDR-04 | P0 | quote age/atomic span | age <= 5秒、span <= 5秒、delay=0 | PROPOSED | proposal quote BLOCKED |
| D-12 / EDR-04 | P0 | SIM delayed/indicative quote | 明示的なSIM warningを許すか選択。無承認ならBLOCKED | PROPOSED | proposal quote BLOCKED |
| D-13 / EDR-07 | P0 | fee schedule | account-specific official schedule | PROPOSED | fee UNKNOWN |
| D-14 / EDR-07 | P0 | fee UNKNOWNのgate | net resultを使う段階はBLOCKED | PROPOSED | net PnLを過大評価する危険 |
| D-15 / EDR-09 | P0 | currency/quantum source | Saxo account/instrument read receipt | PROPOSED | order proposal BLOCKED |
| D-16 / EDR-09 | P1 | fractional未確認時 | quantityを整数に狭め、minimum不明ならBLOCKED | PROPOSED | invalid proposalの危険 |
| D-17 / EDR-10 | P0 | role別numeric SLA | 4.9のdraftをprovider capabilityに合わせて確定 | PROPOSED | freshness NOT_EVALUATED |
| D-18 / EDR-10 | P1 | late/revisionの隔離単位 | role + instrument + session単位 | PROPOSED | 過剰な全停止または見逃し |

### 最小のユーザー決定パケット

一度に18項目を個別回答する必要はない。次の4件を先に決めれば、後続のreceipt検証設計を確定できる。

1. **市場データ**: D-01〜04 — licensed providerを選定し、total-returnとofficial closeを同一契約で受ける。
2. **calendar**: D-05〜06 — ARCX/XNAS intersectionとsession単位HALTを採用する。
3. **account context**: D-08〜09 — C2はSIMかLiveかを一つに固定する。
4. **proposal gate**: D-11〜14 — quote age/span、SIM warning、fee UNKNOWNの扱いを決める。

残りは、この4件が確定した後にsource receiptとともに決定できる。

## 6. 証拠台帳

### Repository evidence

| Evidence | 内容 | 証明すること | 証明しないこと |
|---|---|---|---|
| R-01 `specs/strategy_external_data_contract_v1.json` | EDC-00〜10、required receipt fields、EDR registry | contract/schemaの存在 | operational source availability |
| R-02 `manifests/strategy_external_data_contract_manifest_v1.json` | bundleとsecurity boundary | contract package identity | production migration/status |
| R-03 `manifests/etf11_source_dataset_manifest.json` | Yahoo raw/derived hash、research-only、adjustment説明 | frozen research lineage | current license/SLA |
| R-04 `specs/total_return_full_history_research_contract_v1.json` | full-history research window/definition/hash | fixed research利用可否 | current signal freshness |
| R-05 `specs/instrument_reference_v1.json` | 11 ETFのissuer official URLs | issuer候補一覧 | distribution revision監視の完了 |
| R-06 `docs/strategy_external_data_contract_handoff_20260730.md` | Read API surface、fail-closed、receipt policy | intended handoff contract | live accepted receipt |

### Public primary sources

- [Saxo Chart API](https://www.developer.saxo/openapi/referencedocs/chart/v3/charts/get__chart): UIC/AssetTypeのtimestamped OHLC、60/1440 horizon、最大1200 sample。
- [Saxo Instruments API](https://www.developer.saxo/openapi/referencedocs/ref/v1/instruments): access-rightsで制限されたinstrument detailsとtrading schedule。
- [Saxo Historical Transactions API](https://www.developer.saxo/openapi/referencedocs/hist/v1/transactions/get__hist)、[Transaction correction semantics](https://www.developer.saxo/openapi/learn/transactions): Personal Read、CorporateActionId、CorrectionReason、訂正の扱い。
- [Saxo InfoPrice](https://www.developer.saxo/openapi/referencedocs/trade/v1/infoprices)、[Quote schema](https://www.developer.saxo/openapi/referencedocs/trade/v1/infoprices/get__trade/schema-quote)、[Pricing learning](https://www.developer.saxo/openapi/learn/pricing): non-tradable quote、delayed/realtimeの区別、InfoPrice commission非対応。
- [Saxo Session Capabilities](https://www.developer.saxo/openapi/learn/session-capabilities): capabilityとmarket-data level、変更が他sessionへ及ぼす影響。
- [Saxo Balance](https://www.developer.saxo/openapi/referencedocs/port/v1/balances/get__port): CurrencyとCurrencyDecimals。
- [NYSE trading information](https://www.nyse.com/trade/trading-information?os=io_)、[NYSE holidays and hours](https://www.nyse.com/trade/hours-calendars): NYSE Arca core session、holiday、early close。
- [Nasdaq trading calendar](https://www.nasdaqtrader.com/trader.aspx?id=Calendar): Nasdaq holiday、early close。
- [NYSE Arca official closing-price rule](https://www.nyse.com/publicdocs/nyse/regulation/nyse-arca/NYSE_Arca_Rule_1.1.pdf)、[NYSE Arca closing-auction explanation](https://www.nyse.com/network/article/nyse-arca-closing-auction-enhancements): official closeの決定規則。
- [State Street SPY](https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy)、[State Street ETF distributions](https://www.ssga.com/us/en/individual/resources/documents/etf-dividend-distributions): official closing-price定義とdistribution公表。
- [Vanguard VNQ distributions](https://investor.vanguard.com/investment-products/etfs/profile/vnq): distribution amountとex/record/payable date。
- iSharesの対象8銘柄の公式product URLはR-05に固定する。個別URLの存在だけでcurrent revision監視が完了したとは扱わない。

## 7. 決定後の実施順序

本書の範囲外であり、別作業として次の順に行う。

1. ユーザーが最小決定パケットを承認する。
2. selected providerの11銘柄coverage、利用許諾、revision、SLAを文書で確認する。
3. 明示された単一account contextでread-only receiptを取得する。OAuthやsecretをreceiptへ保存しない。
4. rawをimmutableに保存し、completeness/uniqueness/validity/integrity/timeliness/rightsを検証する。
5. accepted receiptをappend-only登録し、Read API statusをrole別に更新する。
6. `AVAILABLE`または明示承認済み`AVAILABLE_WITH_WARNINGS`になったroleだけをStrategyへ渡す。

いずれの段階でも注文、precheck、account mutation、session capability変更をsource検証に混ぜない。
