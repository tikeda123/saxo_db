# C2 SIM ETF11 quote `NoAccess` 調査結果

調査日: 2026-07-31
対象: C2初回SIM観測のETF11 atomic InfoPrice
状態: **初回技術観測は `SUCCEEDED / PASS_WITH_WARNINGS`、低頻度paper評価は日次終値fallbackで継続可能**

## 結論

米国通常取引時間中に利用者が明示実行した再観測では、15件のGETが成功し、account参照とETF11のinstrument identityも一致した。一方、ETF11全件のInfoPriceは`PriceType=NoAccess`で、Bid/Askは提供されなかった。

Saxo公式の`PriceQuality`定義では、`NoAccess`は利用者が対象price feedの権限を持たない状態である。FX以外のexchange-based商品では、利用できる価格がapplication type、default feed、利用者のfeed契約に依存する。したがって、今回の結果は次のように分類する。

- OAuth/App KeyのRead権限障害ではない。許可した15 GETは完了している。
- instrument不存在・identity driftではない。ETF11のreference identityは一致した。
- 市場閉場による`NoMarket`または`OldIndicative`ではない。通常取引時間中も明示的に`NoAccess`だった。
- Bid/Ask値の破損ではない。価格値自体がfeed権限により提供されていない。
- 現在のSIM利用者・applicationの組合せで、米国ETFのOpenAPI price feedを利用できない状態である。

App Keyのtrading disabled設定はwrite権限を禁止する安全設定であり、market-data feedを付与する設定ではない。Developer PortalのApplication ManagementでApp Keyを作り直しても、このprice-feed権限不足を解消できる根拠はない。

Saxo OpenAPI Supportの一次情報は、純SIM/demoではFX以外のmarket dataを提供しないと明記する。また、SaxoTraderの`OpenAPI Access`からmarket-data免責へ同意する操作はLIVE環境向けであり、それだけではSIMへmarket dataを追加しない。非FXをdemoへ追加する公式経路は、Developer Portalの`Apps > Live Applications`からdemoをLIVE accountへlinkする方法である。したがって純SIMだけで遅延ETF価格を有効化する設定はない。

## 公式根拠

1. [Saxo OpenAPI Pricing](https://www.developer.saxo/openapi/learn/pricing)は、price値がprice-feed rightsによりno data・delayed・real timeになると説明し、`NoAccess`をprice feedまたはinstrumentへsetupされておらず価格が提供されない状態と定義する。
2. [PriceQuality schema](https://www.developer.saxo/openapi/referencedocs/trade/v1/infoprices/post__trade__subscriptions/schema-pricequality)は、`NoAccess`をprice feed permission不足とし、application type、partner default feed、利用者のfeed subscriptionに依存すると明記する。
3. [Core business concepts — Prices and Market Data](https://www.developer.saxo/openapi/learn/core-business-concepts)は、exchange-based商品のfeedはvenueごとの契約に依存し、第三者applicationからAPIで価格を受ける場合はplatform表示とは別のlicense・agreement・追加費用が必要になり得ると説明する。feedがない場合、Pricesだけでなくpositionsのcurrent priceやChartにも影響が波及する。
4. [Environments](https://www.developer.saxo/openapi/learn/environments)は、SIMでは一部のmarket dataが利用できないと明記する。
5. [Saxo Help — live price subscription](https://www.help.saxo/hc/en-us/articles/360001286826-How-do-I-subscribe-to-live-prices)は、platform内のreal-time market dataをdesktop版SaxoTrader/SaxoInvestorからexchange単位で契約する手順を示す。ただし、これはOpenAPI第三者applicationのfeed権限を単独で保証しない。
6. [How do I enable market data?](https://openapi.help.saxo/hc/en-us/articles/4418427366289-How-do-I-enable-market-data)は、LIVEではSaxoTraderの`OpenAPI Access`から有効化・免責同意できる一方、この操作ではSIMのmarket dataを有効化できないと明記する。
7. [Why do I get NoAccess?](https://openapi.help.saxo/hc/en-us/articles/4405160773661-Why-do-I-get-NoAccess-instead-of-prices)は、demo/SIMでは非FX market dataが制限され、LIVE accountとのlinkが回避経路だと説明する。
8. [How can I get non-FX on my demo account?](https://openapi.help.saxo/hc/en-us/articles/4417064381457-How-can-I-get-Stocks-ETFs-CFD-and-other-non-FX-on-my-demo-account)は、Developer Portalの`Apps > Live Applications`からLIVE credentialでlinkする具体的手順を示す。

## 代替PriceType・endpointの評価

| 選択肢 | 意味 | 今回の代替になるか |
|---|---|---|
| `Indicative` | 有効な非取引価格。`DelayedByMinutes`で遅延を判定する | C2では正常な低頻度reference priceとして採用。リアルタイムである必要はない |
| `OldIndicative` | 市場に現在価格がなく、過去の有効価格 | 初回接続確認ではwarning。paper評価のcurrent quoteには使わない |
| `NoMarket` / `Pending` | 現在価格がない、または近日/直後に提供見込み | 再試行可能なavailability状態。entitlementの代替ではない |
| `NoAccess` | price-feed permissionなし | InfoPriceは利用せず、日次終値fallbackへ切り替える。低頻度paper評価全体は止めない |
| streaming `Prices` | 継続価格、Subscribe権限が必要 | 同じfeed rightsに依存するため回避策ではない |
| `Chart` | OHLC sample | proposal Bid/Askではなく、feedなしの影響も受けるため回避策ではない |
| SaxoTrader画面の価格 | platform表示 | OpenAPI利用・再配布権限の証拠にはならない |

InfoPriceはnon-tradable informational priceであり、C2のproposal quote観測候補である。official close、total-return、execution priceへ読み替えない。

## C2段階別の扱い

| C2段階 | 判定 | 理由 |
|---|---|---|
| `SIM_OBSERVATION_START` | **完了** | GET接続、account形式、ETF11 identity、quote response集合を確認できた。価格未提供はwarningとして分離した |
| `SIM_ALLOCATION/PAPER_EVALUATION` | **日次終値fallbackで継続可能** | 四半期リバランスでは1時間遅延価格または日次終値で足り、リアルタイム・二方向Bid/Askは不要 |
| `LIVE_ORDER_ELIGIBILITY` | **PROHIBITED** | 本調査は注文可否を変更しない |

total-return、official close、calendar、distribution、fee等の別roleは、この`NoAccess`だけで品質FAILにしない。各contractの既存gateで独立に判定する。

## 最小の対処と利用者操作

初回SIM技術観測を完了させるための操作は不要であり、同じ条件で再試行しない。低頻度paper評価はsaxo_db Read APIの正規日次終値fallbackで進めるため、**現在必要な利用者設定はない**。

Saxoの遅延InfoPriceを任意で併用する場合だけ、次の一つの設定フローを利用者が行う。

> Developer Portalへdemo accountでloginし、`Apps > Live Applications`からLIVE accountへloginしてSIMをlinkする。そのLIVE accountのSaxoTraderで`Account/Settings > Other > OpenAPI Access`を開き、market-data免責を確認して同意する。

これは外部account設定であり本タスクでは実行しない。LIVE accountがない場合やlinkしない場合も、日次終値fallbackを使うためC2低頻度paper評価は停止しない。real-time subscriptionは不要である。

## 低頻度価格契約

- 正常監視cadence: 1時間ごとの遅延価格、または日次終値。
- 正常PriceType: `Indicative`。`DelayedByMinutes > 0`をwarningではなく許容済み観測値として扱う。
- 数値要件: `Mid`、`Bid`、`Ask`の少なくとも一つが正値。両sideがある場合だけ`Ask >= Bid`を検査する。
- 不要: tick、real-time、二方向Bid/Ask、spread、execution price。
- `NoAccess`: data-quality FAILではない。InfoPrice roleを未利用として日次終値fallbackを選ぶ。
- 実約定確認: 将来の別段階・別contract。現在のC2 SIM paper評価へ持ち込まない。

機械可読正本は[`../specs/c2_low_frequency_price_policy_v1.json`](../specs/c2_low_frequency_price_policy_v1.json)である。

## 日次終値fallbackの読み取り確認

2026-07-31にlocalhost Read APIをread-onlyで確認した。

- `GET /health`: `PASS`、role=`saxo_app_reader`、transaction read-only=`on`。
- `GET /api/v1/bars`: SPY/IWM/EFA/EEM/VNQ/SHY/IEF/TLT/TIP/LQD/GLDの全11銘柄で`layer=1d`を取得可能。
- 2026-07-01以上2026-08-01未満のqueryでは全銘柄18行、最終`session_date=2026-07-27`、`price_basis=native_ohlc`、`is_complete=true`を確認した。
- これは経路と形式の確認であり、current freshnessのPASSを意味しない。更新・freshnessは既存DB3の系列状態で別管理する。
- C2は各日の`close`だけを低頻度referenceに使う。未調整OHLCをtotal returnやprimary-exchange official closeと偽装しない。

標準query:

```text
GET /api/v1/bars?instrument_key=<etf>&layer=1d&start=<UTC>&end=<UTC>&limit=<N>
```

## 安全・監査結果

- 追加Saxo GET: 0
- OAuth再接続: 0
- DB/receipt/raw書込み: 0
- Saxo Portal変更・market-data契約: 0
- 注文・precheck・取消・資金/口座操作: 0
- 既存DB3 scheduler変更: 0

本書とOperator UIはsanitizedな集計だけを扱い、token、App Key、account/client identifier、価格値を記録しない。
