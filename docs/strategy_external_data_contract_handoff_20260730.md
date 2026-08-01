# Strategy Analysis向け外部データ契約 実装・引渡し

作成日: 2026-07-30
対象bundle: `c2_strategy_external_data_contract_v1`
現在状態: `PARTIALLY_RESOLVED / BLOCKED_EXTERNAL_CONTRACT`

> 2026-07-31運用反映: migration 0034/0035を適用し、Read APIを再起動した。
> receiptは10件で、common calendarだけが`AVAILABLE_WITH_WARNINGS`、他の対象roleは
> fail-closedである。現在状態と監査件数は
> [`c2_external_contract_receipt_resolution_report_20260731.md`](c2_external_contract_receipt_resolution_report_20260731.md)
> を正本とする。以下の「未適用」は初期実装時点の記録である。

Saxo SIM Read認証が後から安全に供給された場合の短命・非永続session、
provider決定入力、運用gate決定入力は
[`c2_sim_read_session_and_decision_flow_20260731.md`](c2_sim_read_session_and_decision_flow_20260731.md)
を参照する。現時点でSaxo API実行や認証値保存を行ったことを意味しない。

正規提供元候補、採用推奨、ユーザー決定事項の正本案は
[`c2_external_data_source_decision_ledger_proposal_20260731.md`](c2_external_data_source_decision_ledger_proposal_20260731.md)
を参照する。同文書はsource選定案であり、live API、migration、accepted receiptの運用状態を証明しない。

## 1. 結論

C2 P9以降が必要とする外部データを、`saxo_db`が契約仕様、immutable receipt、状態、manifestとしてGET専用Read APIから渡すためのschema、未適用migration、OpenAPI、fixture、テストを実装した。

これは「sourceが利用可能になった」という報告ではない。current total-return provider、official close、11 ETF共通calendar、口座別cash transaction、quote、fee、USD quantumの実測receiptが未確定の項目は、値を推測せず`BLOCKED_EXTERNAL_CONTRACT`または`NOT_EVALUATED`を維持する。Strategyはこれらを消費するだけであり、Saxoへ直接取得しない。

本作業ではproduction DB migration、Read API再起動、scheduler変更、Saxo取得/refetch、OAuth、口座操作、注文、precheckを実施していない。

## 2. 実装した契約面

| Endpoint | 用途 | 現在の動作 |
|---|---|---|
| `GET /api/v1/strategy-data/contracts` | EDC-00〜10のschema、blocker、decision registry、manifest identity | DB migration前でもmanifest hashを検証して返す |
| `GET /api/v1/strategy-data/status` | quality、freshness、revision、cost confidence、最新receiptを別軸で返す | migration未適用なら`migration_status=NOT_APPLIED`、全項目をfail-closed表示 |
| `GET /api/v1/strategy-data/receipts` | accepted／warning／blocked receiptのbounded履歴 | migration前は空配列。適用後はimmutable public viewだけを読む |
| `GET /api/v1/strategy-data/calendars/{calendar_id}` | accepted receiptのversion付きcalendar intervalとordered content hash | 未acceptedのcatalog seedは公開せず404。2026年common calendarはwarning付きで公開済み |
| `GET /api/v1/manifests` | 既存dataset/snapshot/total-return contractと外部契約bundle | `strategy_external_data_contract`を追加 |

すべてGETのみである。POST/PUT/PATCH/DELETEは既存の`READ_ONLY_API`で拒否する。

## 3. confirmed / blocked / decision-required

### 3.1 Confirmed

- 11 ETFの既存固定期間／full-history total-return研究契約は、source lineageとcontent hashを持つ。ただしcurrent運用用`SIGNAL_TOTAL_RETURN_DAILY`の代替にはしない。
- 既存の1H由来risk日足は、16:00 ETのprimary-exchange official closeとは同一ではない。
- `catalog.session_calendar`と`catalog.session_interval`にはversion、期間、UTC interval、source hashを保持できる。
- Saxo Chart `GET /chart/v3/charts`は`Horizon=1440`をサポートする。ただしprimary-exchange official closeとのparityは未検証である。
- Saxo `GET /hist/v1/transactions`はPersonal Readで、booking、CorporateAction、correction、currency/cost情報を取得する候補である。
- Saxo InfoPrice GETはQuote、LastUpdated、PriceSource、DelayedByMinutes、PriceType、Commissions field group等を返す候補である。InfoPrice自体はnon-tradable informational priceである。
- Saxo instrument trading schedule GETはinstrument別sessionsとtimezoneを返す候補である。

### 3.2 Blocked

| Role | 状態 | blocker |
|---|---|---|
| `SIGNAL_TOTAL_RETURN_DAILY` | blocked | `BLOCKED_EXTERNAL_CONTRACT_SIGNAL_CURRENT` |
| `VALUATION_PRICE_DAILY` | blocked | `BLOCKED_EXTERNAL_CONTRACT_VALUATION_CLOSE` |
| `COMMON_REGULAR_SESSION_CALENDAR` | blocked | `BLOCKED_EXTERNAL_CONTRACT_CALENDAR_PUBLICATION` |
| `DISTRIBUTION_DECLARATION` | schema ready / availability blocked | `BLOCKED_EXTERNAL_CONTRACT_DISTRIBUTION_DECLARATION_SOURCE` |
| `DISTRIBUTION_CASH_TRANSACTION` | blocked | `BLOCKED_EXTERNAL_CONTRACT_DISTRIBUTION_TRANSACTION` |
| `INSTRUMENT_REFERENCE` | schema ready / account context blocked | `BLOCKED_EXTERNAL_CONTRACT_INSTRUMENT_ACCOUNT_CONTEXT` |
| `PROPOSAL_PRICE_SNAPSHOT` | blocked | `BLOCKED_EXTERNAL_CONTRACT_PROPOSAL_QUOTE` |
| `FEE_ESTIMATE_AND_ACTUAL` | blocked | `BLOCKED_EXTERNAL_CONTRACT_FEE_ESTIMATE` |
| `CURRENCY_AND_AMOUNT_UNIT` | spec closed / receipt blocked | `BLOCKED_EXTERNAL_CONTRACT_USD_ACCOUNT_QUANTUM` |
| `REVISION_AND_LATENCY_STATE` | spec closed / source SLA blocked | `BLOCKED_EXTERNAL_CONTRACT_SOURCE_SLA` |

API/network/permission failureは`BLOCKED_INTERFACE_OPERATIONAL`、値の欠損・重複・非正値・identity不整合は`FAIL_DATA_QUALITY`として分離する。未観測は`NOT_EVALUATED`でありPASSではない。

### 3.3 Decision required

- `EDR-01`: current 11 ETF adjusted total-return provider、定義、revision、publication SLA。
- `EDR-02`: official-close sourceと11 ETF parity条件。
- `EDR-03`: XNYS/XNAS regular-session intersectionとSaxo schedule不一致時のhalt規則。
- `EDR-04`: quoteのaccepted PriceType、最大age、11銘柄atomic span。値を見る前に固定する。
- `EDR-05`: issuer distribution revision sourceと口座Historical Transactions receipt。
- `EDR-06`: USD account contextでのETF11 reference receipt。
- `EDR-07`: account tier／venueに対応するfee schedule。`UNKNOWN`を0にしない。
- `EDR-09`: USD currency decimals、整数quantity、minimum size/value parity。
- `EDR-10`: provider確定後のsource別numeric SLA。

## 4. Receiptと監査

`ops.strategy_external_data_receipt`はappend-onlyである。UPDATE/DELETEはtriggerで拒否し、`saxo_ingest`にはINSERTだけを許可する。`saxo_app_reader`と`saxo_analyst_reader`はsecurity-barrier viewだけを読む。

accepted receiptは少なくともprovider、lineage、ordered content hash、accepted timestamp、`quality=PASS|PASS_WITH_WARNINGS`、`freshness=CURRENT`を必要とする。warning受入にはwarning IDが必要である。`values_modified=false`、`interpolation_performed=false`をDB制約で固定した。token、Authorization、AccountKey、ClientKeyに相当するkeyをreceipt payloadへ保存できない。

訂正は旧receiptを更新せず、`supersedes_receipt_id`を持つ新receiptを追加する。quality、freshness、revision、cost confidenceは一つの緑色statusへ集約しない。

## 5. Strategy consumer手順

1. `GET /health`でread-only roleを確認する。
2. `GET /api/v1/strategy-data/contracts`の`bundle_id`と`manifest_sha256`をrun manifestへ保存する。
3. `GET /api/v1/strategy-data/status`で必要roleの`availability_state`、quality、freshness、revision、blockerを確認する。
4. `AVAILABLE`または事前承認済み`AVAILABLE_WITH_WARNINGS`のreceiptだけを`GET /api/v1/strategy-data/receipts?dataset_role=...`から取得する。
5. data roleの実データは、receiptが指定した既存stable endpointとcontract/dataset identityで取得する。
6. hash、row count、session coverage、instrument setをStrategy側でも照合する。

`BLOCKED_EXTERNAL_CONTRACT`、`NOT_EVALUATED`、`DATA_NOT_READY`、`BLOCKED_INTERFACE_OPERATIONAL`、`FAIL_DATA_QUALITY`を相互変換しない。

## 6. 未適用rollout手順

以下は今回実施していない。

1. maintenance windowを確保する。
2. migration checksumをreviewする。
3. `.venv/bin/python -m market_db.migrate apply`でmigration `0034`をproduction DBへ適用する。
4. `.venv/bin/python -m market_db.migrate validate`でchecksumを確認する。
5. Read APIを新コードで再起動する。
6. `/health`、contracts、status、receipts、calendar、manifestsをGETで確認する。
7. source ownerが確定したroleだけ、正規取得・品質gate・immutable receipt発行を別作業で行う。

migration適用前も`contracts`はfixtureから読める。`status`と`receipts`は`migration_status=NOT_APPLIED`を返すため、未適用をavailability PASSと誤認しない。

## 7. Rollback

本変更は未適用なのでproduction rollbackは不要である。将来0034適用後にAPI codeだけ戻す場合も、新tableとreceiptは削除しない。新endpointを旧codeから参照しなくなるだけで、immutable evidenceは残す。schema削除が必要な場合は別migrationと明示承認を必要とし、手動DELETE/DROPを通常rollbackにしない。

## 8. StrategyがP9を開始できる条件

必要roleのstatusが`AVAILABLE`または事前承認済み`AVAILABLE_WITH_WARNINGS`となり、対応receiptがprovider、lineage、manifest/content hash、coverage、quality、freshness、revisionを確定していること。現在は外部事実が未確定なので、この実装だけではP9開始条件を満たさない。

StrategyのWFO、PnL、会計、allocation、注文判断は`saxo_db`の対象外である。実注文、SIM注文、precheck、口座mutationは一切行わない。

## 9. 一次資料

- [Saxo Chart GET](https://www.developer.saxo/openapi/referencedocs/chart/v3/charts/get__chart): OHLC、`Horizon=1440`、UIC/AssetType、最大件数の仕様。
- [Saxo Historical Transactions GET](https://www.developer.saxo/openapi/referencedocs/hist/v1/transactions/get__hist): Personal Read、期間／UIC／CorporateActionId等のfilterとtransaction response。
- [Saxo InfoPrice GET](https://www.developer.saxo/openapi/referencedocs/trade/v1/infoprices/get__trade): Quote、Commissions field group、LastUpdated、PriceSource等。
- [Saxo Instruments](https://www.developer.saxo/openapi/referencedocs/ref/v1/instruments): instrument detailとtrading schedule GET。
- [NYSE trading hours/calendar](https://www.nyse.com/trade/hours-calendars)、[Nasdaq holiday calendar](https://www.nasdaqtrader.com/trader.aspx?id=Calendar): regular session、holiday、early close照合候補。

上記はendpoint／schema／候補sourceの存在を確認する一次資料である。対象口座での権限、11 ETF current response、利用条件、SLA、parityを証明するaccepted receiptではない。

## 10. C2 ETF11短時間欠落overlay（2026-08-01追記）

C2低頻度紙上監視だけは、ETF11 1H `native_ohlc`の短い欠落を最大2本まで`IMPUTED_PREVIOUS_VALID`として別overlayで利用できる。Strategyは`GET /api/v1/c2/daily-close-status`の`imputed_bar_count`、`imputation_status`、`warning_ids`を必ず保存し、必要な場合だけ`GET /api/v1/c2/hourly-overlay`でrow-level lineageを取得する。

このoverlayはraw/canonicalを変更せず、日次close自体はactual terminal provider rowを必須とする。`official_close_claim`、`total_return_claim`、`execution_price_claim`はfalseであり、EDC-01/02やexecution価格の代替ではない。3本以上、terminal欠落、calendar/DataVersion/lineage不一致は当該instrumentだけをfail-closedにし、他銘柄とサービス全体は継続する。

migration 0036とTIP/GLD backfillは未適用であるため、現時点のStrategy consumerは新contractをlive利用可能と判定してはならない。適用後のcontract IDは`c2_etf11_bounded_hourly_overlay_v1`、詳細は[`C2 ETF11有界補完仕様`](c2_etf11_bounded_imputation_design_20260801.md)を参照する。
