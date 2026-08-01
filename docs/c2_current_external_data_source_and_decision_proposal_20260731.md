# C2 current外部データ: 正規ソース候補・決定台帳案

> **位置付け:** source選定とユーザー決定事項の正本案は
> [`c2_external_data_source_decision_ledger_proposal_20260731.md`](c2_external_data_source_decision_ledger_proposal_20260731.md)
> に統合した。本書のlive Read API/migration状態に関する記述は当時の観測メモであり、現在状態の証明には使用しない。

作成日: 2026-07-31
対象: `c2_strategy_external_data_contract_v1`（EDC-00〜10）
目的: C2をcurrent運用データで評価する前に、各データroleの正規ソース、採用判断、既存Read APIだけで再確認できる候補を明確化する。
作業境界: 既存の仕様、manifest、Read APIのGET結果、および公開一次資料を参照した。新規provider契約、認証情報追加、Saxo OAuth/API呼び出し、外部取得、DB書込み、scheduler、注文、口座変更はゼロである。

## 結論

`GET /api/v1/strategy-data/status` を2026-07-31に再確認した。migration 0034は`APPLIED`、Read APIはread-onlyだが、11 roleすべてにaccepted receiptはなく、overallは`BLOCKED_EXTERNAL_CONTRACT`である。

最初に決めるべきことは、**EDR-01（current total-return）とEDR-02（valuation official close）を同じ正規市場データ契約で満たすか**である。推奨は、11 ETFを一つの利用許諾・訂正履歴・SLAで提供する市場データvendorを、market-price adjusted total returnとprimary-exchange official closeの両方について採用すること。Saxoの価格、各ETF発行体の分配情報、口座明細は、この基準系列を補完・照合する用途に限定する。

理由は、ETF発行体ごとのページをつないでmarket-price total returnを自作すると、分配、split、訂正、ETFの市場終値、再投資タイミングの定義が一つに固定されず、C2のrisk signalと会計を混同しやすいためである。既存Yahoo adjusted close研究系列は、fixed/full-history研究のlineage証跡であり、current運用receiptへ昇格させない。

## 1. 確認済みの前提

| 項目 | 確認済み事実 | 運用上の意味 |
|---|---|---|
| ETF universe | SPY/IWM/EFA/EEM/VNQ/SHY/IEF/TLT/TIP/LQD/GLD。ARCA: SPY/IWM/EFA/EEM/VNQ/TIP/LQD/GLD、NASDAQ: SHY/IEF/TLT | calendarとofficial-closeは少なくともNYSE ArcaとNasdaqを横断する |
| 既存研究系列 | total-returnのfixed/full-history contractはhash・lineageを持つ | historical WFO入力としてのみ使用。current signalやNAV markには使用不可 |
| Saxo市場データ | Chartの日次取得候補、InfoPrice、instrument details、trading schedule、Historical Transactionsのread-only endpoint候補が仕様化済み | Saxoはbroker/account context、quote、cash、instrument確認の正規候補。primary official closeやcurrent total returnを自動的には証明しない |
| カレンダー | NYSEとNasdaqの公式calendarは別々に公表される。NYSE Arca core sessionは9:30–16:00 ET | 共通calendarは二つの公式calendarのintersectionとearly-closeをversion化して初めて受入可能 |

証拠: `specs/strategy_external_data_contract_v1.json`、`manifests/collection_spec.json`、`docs/saxo_api_data_acquisition_handoff.md`、`docs/strategy_external_data_contract_handoff_20260730.md`。

## 2. role別の候補と採用推奨

| role / decision | 正規候補 | 推奨 | 既存Read APIだけで直ちに確認できること | 受入前に残る条件 |
|---|---|---|---|---|
| EDC-00 `CURRENT_NATIVE_MARKET_BAR` | Saxo Chart 60分足 | Saxoをsource of record候補として維持 | `/api/v1/bars`の11銘柄、coverage、quality、lineage、completed-bar watermark | current receipt、全11銘柄の同時coverage、SLA |
| EDC-01 `SIGNAL_TOTAL_RETURN_DAILY` / EDR-01 | A) 一つのlicensed market-data vendorのETF market-price adjusted-total-return。B) Saxo日次終値＋発行体分配＋splitを自作 | **Aを採用**。BはAを利用できない場合だけ、別の算式・訂正・再投資時刻契約を先に承認 | 既存研究total-returnのdefinition/hashのみ | provider、ライセンス、11銘柄完全coverage、return definition、revision/SLA |
| EDC-02 `VALUATION_PRICE_DAILY` / EDR-02 | ARCA/Nasdaqのprimary-exchange official closeを含むlicensed source。Saxo 1D Chartはparity照合候補 | **primary-exchange official closeを採用**。Saxo日次値は11銘柄の連続parityが証明された場合のみ代替候補 | instrument mappingとSaxo日次endpoint候補 | sourceのofficial-close定義、全11 ticker/venue、parity閾値・不一致時HALT |
| EDC-03 common calendar / EDR-03 | NYSE official holiday/early-close calendar + Nasdaq official trading calendar。Saxo trading scheduleはbroker側照合 | **NYSE Arca/Nasdaqのintersectionをversioned contract化**。片方休場・短縮日・Saxo不一致ではHALT | calendar endpointの既存interval/version/hash evidence | 当年以降のsource publication、early-close rule、Saxo schedule parity |
| EDC-04 declaration / EDR-05 | iShares（IWM/EFA/EEM/SHY/IEF/TLT/TIP/LQD）、State Street（SPY/GLD）、Vanguard（VNQ）の公式distribution/corporate-action情報 | **発行体公式を採用**。各issuerの訂正ID・取得時刻をreceipt化 | instrument referenceのissuer URLとticker master | distribution sourceのformat/revision、GLDのordinary distributionの有無を一次資料で確定 |
| EDC-05 cash transaction / EDR-05 | Saxo `GET /hist/v1/transactions` Personal Read | **Saxo booked transactionをactual cashの唯一のsource of record**にする | receipt endpointが空であること、schema/contract | 対象SIMまたはLiveの一方のaccount contextとPersonal Read承認 |
| EDC-06 instrument context / EDR-06 | Saxo instrument details + account eligibility | **Saxo read responseを採用** | canonical UIC/AssetType/symbol/currency masterとの一致 | 対象口座、TradableOn/market status/minimum fieldsを含むread receipt |
| EDC-07 proposal quote / EDR-04 | Saxo InfoPrice GET | **Saxo bid/askを提案価格・spread観測に採用**。official closeと混用しない | contract/statusのquote blocker | accepted PriceType、max quote age、11銘柄atomic span、delayed quoteのHALT |
| EDC-08 fee / EDR-07 | Saxo account-specific official fee schedule + Historical Transactions booked costs | **scheduleはestimate、booked transactionはactual**。`UNKNOWN`を0にしない | current statusがfee blockerであること | account tier、venue、currency、minimum commission、rounding、actual cost read receipt |
| EDC-09 currency/unit / EDR-09 | Saxo account metadata + instrument details + booked transaction | **Saxo account/instrument responseを採用** | canonical currency=USDと11 ETF mapping | account currency、integer/fractional quantity、minimum size/value、amount decimals |
| EDC-10 revision/SLA / EDR-10 | 上記accepted receiptのpublished/observed/available/accepted timestamp | **独立sourceを増やさず、receiptから算出** | status APIのNOT_EVALUATED state | role別expected-by、revision window、late/revision時のHALT |

### 市場データvendorについての選択肢

候補はFactSet、LSEG Data & Analytics、Bloomberg、ICE Data Servicesなど、ETFのmarket-price adjusted total return・corporate action・primary-exchange closeを**同一の利用許諾とdata version**で提供できる提供元である。ここでは特定vendorを契約済みと扱わない。

選定基準は価格ではなく、次の必須条件である。

1. 11 ETFのmarket-price adjusted total returnとunadjusted official closeを別seriesとして返せる。
2. ARCAとNASDAQのlisting/exchange、corporate action、split、distribution、訂正履歴を返せる。
3. point-in-time data version、publication/available timestamp、利用・再配布条件をreceipt化できる。
4. 日次signal用とNAV mark用を同じsource identityで監査できる。

## 3. 決定台帳案

| ID | ユーザーに決めてもらうこと | 提案する既定値 | 決定しない場合の状態 |
|---|---|---|---|
| EDR-01 | current adjusted-total-return providerを選ぶか | 単一licensed market-data vendor。Saxo+issuer自作は非採用 | C2 signal `BLOCKED` |
| EDR-02 | valuation markを何と定義するか | listing venueのprimary-exchange official close。Saxo日次値はparity確認のみ | NAV/DD `BLOCKED` |
| EDR-03 | common calendarと不一致時の扱い | XNYS/ARCAとXNAS regular-session intersection。休日・early close・Saxo不一致はHALT | rebalancing / valuation `BLOCKED` |
| EDR-04 | quoteの受入条件 | Saxo bid/ask、非遅延、age上限と11銘柄atomic spanを別途数値固定。条件外はHALT | proposal `BLOCKED` |
| EDR-05 | 分配宣言と入金のsource of record | issuer declaration + Saxo booked cash。訂正はsuperseding receipt | cash/NAV accounting `BLOCKED` |
| EDR-06 | account context | SIMまたはLiveのどちらか一つ。混在不可 | instrument/eligibility `BLOCKED` |
| EDR-07 | fee policy | account-specific official scheduleをestimate、booked costをactual。未確定=UNKNOWN | net PnL `BLOCKED` |
| EDR-09 | quantity/currency | Saxo read receiptで確定するまで、整数株・USD等を仮定しない | order proposal `BLOCKED` |
| EDR-10 | latency/revision policy | provider選定後、role別expected-byとrevision windowを数値で固定 | freshness/revision `BLOCKED` |

## 4. 既存Read APIだけでできる次の確認（書込みなし）

これはsourceを新規に取得する作業ではない。既存のDB evidenceとmigration 0034の公開状態を再確認するだけである。

1. `/health`: `saxo_app_reader` とread-onlyを確認する。
2. `/api/v1/strategy-data/contracts`: bundle revision、decision registry、manifest hashをrun evidenceへ保存する。
3. `/api/v1/strategy-data/status`: EDC-00〜10のstateを確認する。2026-07-31時点ではaccepted receiptは0、全roleがblockedまたはnot evaluatedである。
4. `/api/v1/strategy-data/receipts`: accepted receiptが0であることを再確認する。
5. `/api/v1/strategy-data/calendars/XNYS_US_EQUITY`: existing calendar evidenceのversion/hashを確認する。ただしcommon calendar acceptanceには使わない。
6. `/api/v1/bars`: EDC-00について、既存11 ETF current barのcoverage/quality/watermarkを確認する。これはcurrent market-bar receiptの候補評価であり、新しいSaxo取得ではない。

## 5. 承認後の最小実施順序

1. EDR-01〜03を決定する。ここが市場データ・valuation・calendarの入口である。
2. 選定providerの利用許諾と11 ETF coverageを確認する（別承認）。
3. EDR-05〜10を決定し、SIMまたはLiveの単一account contextを決める。
4. その後に限り、approved sourceごとに一回のread-only取得、quality/lineage/hash検証、append-only receipt登録を明示作業として実施する。
5. statusの必要roleが`AVAILABLE`（又は明示承認済み`AVAILABLE_WITH_WARNINGS`）になるまで、P9、SIM、注文に進まない。

## 一次資料・既存証拠

- [NYSE Holidays & Trading Hours](https://www.nyse.com/trade/hours-calendars): NYSE Arcaを含むcore sessionとholiday/early-closeの公式公表。
- [Nasdaq Trading Calendar](https://www.nasdaqtrader.com/trader.aspx?id=Calendar): Nasdaqのcalendar・market-data policy参照先。
- [Saxo Chart API](https://www.developer.saxo/openapi/referencedocs/chart/v3/charts/get__chart)、[Historical Transactions API](https://www.developer.saxo/openapi/referencedocs/hist/v1/transactions/get__hist)、[InfoPrice API](https://www.developer.saxo/openapi/referencedocs/trade/v1/infoprices/get__trade)、[Instrument API](https://www.developer.saxo/openapi/referencedocs/ref/v1/instruments): broker/account contextのGET候補。
- `docs/strategy_external_data_contract_handoff_20260730.md`: EDC-00〜10のschema、fail-closed state、receipt要件。
- `specs/instrument_reference_v1.json`: 各ETFのissuer official URLと価格系列の注意事項。
- `manifests/collection_spec.json`: 11 ETFのcanonical UIC、asset type、USD、ARCA/XNAS mapping。

## 非対象

本書はprovider候補と採用判断を整理するだけである。providerの契約、資格情報、Saxo OAuth/API、外部取得、DB receipt INSERT、scheduler、口座操作、注文、precheckは行っていない。
