# C2外部データ契約 技術ブロッカー解消結果

作成日: 2026-07-31
対象: `c2_strategy_external_data_contract_v1` / EDC-00〜10
live確認時点: 2026-07-31T01:51Z
状態: **PARTIALLY_RESOLVED / FAIL-CLOSED**

## 結論

`migration 0035`を適用し、検証済みreceiptが初期`BLOCKED_EXTERNAL_CONTRACT`より優先されるRead API状態表示へ修正した。NYSE/NYSE ArcaとNasdaqの2026年公式calendarを取得・hash化し、regular sessionのintersectionを`ARCX_XNAS_COMMON_REGULAR_2026`としてappend-only登録した。このroleは`AVAILABLE_WITH_WARNINGS`、`quality=PASS_WITH_WARNINGS`、`freshness=CURRENT`である。公式ページに機械可読な`published_at`がないためwarningを残す。

Saxo口座依存のinstrument/account context、quote、cash transaction、currency/amount quantumは、GET-only clientとreceipt schemaまで実装したが、実行環境は`AUTH_CONFIG_MISSING`である。値を取得していないため`PASS`にせず、`BLOCKED_INTERFACE_OPERATIONAL_AUTH_NOT_READY`を登録した。issuer distribution revision監視、account-specific fee適用条件、上流receiptから導出するSLAも未確定なので`BLOCKED_EXTERNAL_CONTRACT`を維持する。Saxo公式の一般料金ページは到達性とcontent hashを確認したが、口座固有tierの証拠やactual feeの代替としては受け入れていない。

current total-return providerとofficial-close providerの選定・契約・取得は行っていない。

## 解消済み

| 項目 | 結果 | 証拠 |
|---|---|---|
| receipt schema/hash | PASS | strict key、UTC timestamp、required field、canonical SHA-256、秘密key再帰拒否、available時のprovider/lineage/content hash gate |
| append-only publication | PASS | 初回8 receiptとissuer probe 1 receiptをappend-only INSERT。UPDATE/DELETE trigger、writerはINSERTのみ |
| status表示 | PASS | accepted receiptを初期catalog blockerより優先。新しいblocked receipt後もlast-goodを保持 |
| common calendar | `AVAILABLE_WITH_WARNINGS` | 2026年251 sessions、10 holidays、11/27・12/24の13:00 ET短縮、ARCX/XNAS intersection |
| Read API | PASS | `/health` read-only、`/status`、`/receipts`、calendar endpointをlive確認 |
| Saxo GET-only transport | 実装PASS | accounts/me、balances/me、session capabilities、historical transactions、InfoPrice list。write methodはallowlistに追加していない |

calendar identity:

- `calendar_id=ARCX_XNAS_COMMON_REGULAR_2026`
- `calendar_version=arcx_xnas_common_2026_v1`
- `normalized_sha256=a7a6fddbb70bab6c256d8dbdc635d62dc9397e1a76dc7905d950fc6652a3a8fd`
- NYSE source SHA-256: `c466a9cb0377a8028046cd581e782f2695d01e2d06570a8f68d048681c0f2ebe`
- Nasdaq source SHA-256: `efcba8539b42986a6f6b874c00f9ad395daeac197425166d0e18fc5701c2a529`
- warning: `SOURCE_PUBLISHED_AT_NOT_EXPOSED`

公式source:

- [NYSE Holidays & Trading Hours](https://www.nyse.com/trade/hours-calendars)
- [Nasdaq Trader Holiday Calendar](https://www.nasdaqtrader.com/trader.aspx?id=Calendar)

## 未解消

| Role | current state | 理由 | 次アクション |
|---|---|---|---|
| `CURRENT_NATIVE_MARKET_BAR` | `NOT_EVALUATED` | 今回はscheduler/refetchを禁止 | 既存bar receiptを別運用で評価 |
| `SIGNAL_TOTAL_RETURN_DAILY` | `BLOCKED_EXTERNAL_CONTRACT` | current provider未選定 | EDR-01。今回は選定しない |
| `VALUATION_PRICE_DAILY` | `BLOCKED_EXTERNAL_CONTRACT` | official-close provider未選定 | EDR-02。今回は選定しない |
| `DISTRIBUTION_DECLARATION` | `BLOCKED_EXTERNAL_CONTRACT` | issuer 11銘柄の公式ページ到達性・HTTP content hashは確認済み。訂正revision、published-at、negative event意味論が未検証 | sourceごとのas-of/revision/negative eventを構造化して検証 |
| `DISTRIBUTION_CASH_TRANSACTION` | `BLOCKED_INTERFACE_OPERATIONAL` | Saxo OAuth/AppKeyが現在のprocessで未設定 | fresh OAuth後、`GET /hist/v1/transactions`をUIC/date/pagination付きで取得 |
| `INSTRUMENT_REFERENCE` | `BLOCKED_INTERFACE_OPERATIONAL` | 同上 | details + accounts/balance/capabilitiesを同一SIM contextでGET |
| `PROPOSAL_PRICE_SNAPSHOT` | `BLOCKED_INTERFACE_OPERATIONAL` | 同上 | InfoPrice listをGETし、delay/age/PriceType/11銘柄spanを検証 |
| `FEE_ESTIMATE_AND_ACTUAL` | `BLOCKED_EXTERNAL_CONTRACT` | Saxo公式の一般料金ページは到達・hash確認済み。account-specific適用条件は未検証、actualはOAuth待ち | account tier/venueを固定し、actual transactionと分離 |
| `CURRENCY_AND_AMOUNT_UNIT` | `BLOCKED_INTERFACE_OPERATIONAL` | account currency/decimals/minimumを未観測 | account/instrument GET receiptを発行 |
| `REVISION_AND_LATENCY_STATE` | `BLOCKED_EXTERNAL_CONTRACT` | upstream accepted receipt不足、numeric SLA未決定 | role別receiptから導出 |

公式Saxo仕様で確認済みなのは「GET endpointとfieldが存在すること」であり、現在のSIM口座で値が返ることではない。

- [Historical Transactions GET](https://www.developer.saxo/openapi/referencedocs/hist/v1/transactions/get__hist): Personal Read、CorporateActionId、CorrectionReason、CurrencyDecimals、BookedAmount。
- [InfoPrice](https://www.developer.saxo/openapi/referencedocs/trade/v1/infoprices): non-tradable snapshot。Bid/Ask、PriceSource、DelayedByMinutes、PriceTypeはfeed権限依存。
- [Balance GET](https://www.developer.saxo/openapi/referencedocs/port/v1/balances/get__port__me): Community/Personal Read。
- [Session capabilities GET](https://www.developer.saxo/openapi/referencedocs/root/v1/sessions/capabilities): GETだけを使用し、PUT/PATCHは使用しない。
- [Saxo Commissions, Charges and Margin Schedule](https://www.home.saxo/rates-and-conditions/commissions-charges-and-margin-schedule): 一般公開料金表のHTTP responseをhash化。account-specific applicabilityは未検証。

## DB監査

| 対象 | 前 | 後 | 差分 |
|---|---:|---:|---:|
| `ops.strategy_external_data_receipt` | 0 | 10 | +10 |
| `raw.market_bar_revision` | 2,623,747 | 2,623,747 | 0 |
| `raw.reference_observation` | 90,894 | 90,894 | 0 |
| `curated.market_bar` | 789,713 | 789,713 | 0 |
| `ops.ingestion_run` | 1,087 | 1,087 | 0 |

receipt bundle: `manifests/c2_external_data_receipts_20260731.json`
bundle SHA-256: `abd44560e2c639f7b31799d09d77bd363e091f0fc9998a57bf92fb120f261595`
issuer source probe: `manifests/c2_distribution_source_probe_receipt_20260731.json`
probe SHA-256: `201c64e1a9998a092cb5d1e30629c25a4d486eaf8c723bb4d83cc96631a07b53`
fee source probe: `manifests/c2_fee_source_probe_receipt_20260731.json`
probe SHA-256: `0f4bea37764c36740a7c3f54d4a24b9803ff02e530106de44281a876f79ba1f5`
official page SHA-256: `0ac338977db2d8b4ff9087def17090b56f57f2a2c8701e68ce1be3b7254d2ac4`

`0034`と`0035`を含む全適用migration checksumはPASS。Read APIは`role=saxo_app_reader`、`transaction_read_only=on`、`statement_timeout=30s`、`health=PASS`である。

関連回帰テスト（external contract/receipt、Saxo GET-only client、migration、Read API、operational readiness）は`112 passed`。`git diff --check`もPASSした。

## Strategy向けGET

```text
GET /api/v1/strategy-data/status
GET /api/v1/strategy-data/receipts?limit=20
GET /api/v1/strategy-data/calendars/ARCX_XNAS_COMMON_REGULAR_2026?start=2026-01-01&end=2027-01-01&limit=5000
```

Strategyはcalendar warningを明示的に受け入れる場合だけ使用できる。口座依存roleはまだ利用不可であり、interface blockerをdata-quality failureへ変換しない。

## ユーザー決定待ち

1. C2のSaxo contextをSIMに固定し、fresh OAuthを行うか。行う場合もGET-onlyで、口座変更・注文はしない。
2. quoteの最大age、最大`DelayedByMinutes`、11銘柄snapshot spanと、SIM delayed quoteをwarning許容するか。
3. issuer 3系統（iShares / State Street / Vanguard）のrevision監視規則と、GLDのnegative distribution stateの扱い。
4. account-specific official fee scheduleの版・tier。未確定feeは`UNKNOWN`であり0にしない。
5. role別numeric SLAと、late/revision時のrole + instrument + session単位の隔離規則。

## 安全証跡

注文0、precheck 0、取消0、資金移動0、口座変更0、scheduler変更0、full-refetch/backfill 0、Saxo API GET 0。token、AppKey、account/client identifierはreceipt・log・本書へ保存していない。

StrategyのWFO、PnL、allocation、注文判断は対象外である。
