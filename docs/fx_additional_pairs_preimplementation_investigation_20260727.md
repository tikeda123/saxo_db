# FX追加3通貨ペア 事前調査・取得試験結果

## 結論

2026-07-27のSaxo SIM環境に対するGET-only試験では、`AUDUSD`、`USDCAD`、`USDCHF`のすべてについて、正しいinstrument identityを特定し、FxSpot 1時間足の取得と直近1,200本の基本品質検査に合格した。

現在判定は次のとおりである。

- 取得可能性: `PASS_SAMPLE`
- 全履歴品質: `NOT_EVALUATED`
- DB登録・curated化: `NOT_STARTED`
- Read API公開: `BLOCKED_NOT_REGISTERED`
- 定期取得: `NOT_ENABLED`

したがって、本結果だけで定期取得やRead API公開を有効化しない。全3ペアの全履歴取得、品質ゲート、lineage確定、Read API検証がすべてPASSした場合に限り、有効化を検討する。

## 対象と責任範囲

対象候補は、既存EURUSDに加える研究用の初期3ペアである。

| Instrument key | Symbol | 選定上の比較要因 |
|---|---|---|
| `audusd` | AUDUSD | 資源国通貨を含む比較対象 |
| `usdcad` | USDCAD | 北米・資源国通貨を含む比較対象 |
| `usdchf` | USDCHF | リスク回避局面を含む比較対象 |

これはデータ基盤の候補選定であり、売買指示、戦略判定、将来予測ではない。

`saxo_db`が扱う範囲は、Saxo OpenAPIからの取得、immutable raw保存、正規化、curated生成、coverage・freshness・quality・watermark・lineage、scheduler、Read APIである。signal、WFO、Holdout、PnL、position、precheck、order、口座操作は対象外である。

## Saxo instrument identityと1H取得可否

Saxo OpenAPIのinstrument search、instrument details、trading schedule、Chart 1Hを順に照合した。

| Instrument | UIC | AssetType | Quote currency | Tick size | Horizon | Trading schedule | 判定 |
|---|---:|---|---|---:|---:|---|---|
| AUDUSD | 4 | `FxSpot` | USD | 0.00001 | 60分 | 取得可 | PASS |
| USDCAD | 38 | `FxSpot` | CAD | 0.00001 | 60分 | 取得可 | PASS |
| USDCHF | 39 | `FxSpot` | CHF | 0.00001 | 60分 | 取得可 | PASS |

Saxo公式仕様では、instrument searchは利用者のaccess rightsを反映し、ChartはUICとAssetTypeで系列を指定する。FxSpotのChart sampleはBid/AskのOHLCを返し、`Horizon=60`を利用できる。

- [Saxo OpenAPI Instrument Reference](https://www.developer.saxo/openapi/referencedocs/ref/v1/instruments)
- [Saxo OpenAPI Chart Reference](https://www.developer.saxo/openapi/referencedocs/chart/v3/charts)
- [Saxo OpenAPI Chart Guide](https://www.developer.saxo/openapi/learn/chart)

## GET-only取得試験

試験は認証済みSaxo SIM環境に対し、レスポンス本文を保存せず、in-memoryで集計した。instrument search・details・trading schedule・Chartに12 GET、`ChartInfo.FirstSampleTime`確認に3 GETを使用した。

- HTTP method: GETのみ
- 合計request: 15
- Saxo write request: 0
- precheck: 0
- order: 0
- token・credential・口座識別子の出力: 0
- raw/DB/manifestへの書込み: 0

### 直近1,200本の標本結果

| Instrument | DataVersion | 取得範囲 UTC | 最新sample | 最新完成候補 | Rows | 重複 | 時刻順 | Bid/Ask交差 | Null | 非正値 | OHLC不整合 | 判定 |
|---|---:|---|---|---|---:|---:|---|---:|---:|---:|---:|---|
| AUDUSD | 29749260 | 2026-05-19 10:00 ～ 2026-07-27 13:00 | 13:00 | 12:00 | 1,200 | 0 | strict | 0 | 0 | 0 | 0 | PASS_SAMPLE |
| USDCAD | 29749380 | 2026-05-19 10:00 ～ 2026-07-27 13:00 | 13:00 | 12:00 | 1,200 | 0 | strict | 0 | 0 | 0 | 0 | PASS_SAMPLE |
| USDCHF | 29749380 | 2026-05-19 10:00 ～ 2026-07-27 13:00 | 13:00 | 12:00 | 1,200 | 0 | strict | 0 | 0 | 0 | 0 | PASS_SAMPLE |

`Bid/Ask交差=0`はOpen、High、Low、Closeの各項目で`Bid <= Ask`を確認した結果である。OHLC不整合はBid、Ask、算出midpointの各sideについて検査した。最新13:00Z sampleは形成中の可能性があるため、完成watermark候補から除外した。

3ペアすべてで`ChartInfo.FirstSampleTime=2002-09-25T02:40:00Z`、`DelayedByMinutes=0`が返った。ただし、この値はSaxoが示す取得可能開始時刻の案内であり、全slotの存在や品質合格を保証しない。全履歴pagingを完了していないため、履歴coverageをPASSとは判定しない。

## 品質所見

### 合格した項目

- UIC、AssetType、symbolの一致
- `Horizon=60`の取得可否
- 直近1,200本のcompleteness、uniqueness、strict UTC order
- Bid/AskのOpen・High・Low・Close整合
- Bid/Ask/midpointのOHLC内部整合
- 直近sampleの鮮度候補

### 未評価・ブロック中の項目

| 項目 | 状態 | 理由 |
|---|---|---|
| 全履歴coverage | NOT_EVALUATED | `FirstSampleTime`までの全pageを取得していない |
| 全page DataVersion一貫性 | NOT_EVALUATED | 直近pageだけの確認 |
| 歴史的gap分類 | NOT_EVALUATED | verified calendarとの全期間anti-join未実施 |
| immutable raw lineage | NOT_STARTED | 試験ではbodyを保存していない |
| DB raw→curated | NOT_STARTED | candidateをcatalog登録していない |
| current watermark | NOT_STARTED | 完成足の正式公開をしていない |
| Read API | BLOCKED_NOT_REGISTERED | `series-status`は3ペアとも404 |
| scheduler | NOT_ENABLED | active scopeへ追加していない |

現時点の404はinterface障害ではなく、未登録候補をfail-closedで公開しない正常な状態である。

## 既存運用への影響

- active scheduler scopeは変更していない。
- EURUSDとETF 11系列の定期取得は継続している。
- USDJPYは`BLOCKED_PROVIDER_CONTENT_QUALITY`の隔離を維持した。
- USDJPYの再取得、DataVersion変更、品質gate緩和は行っていない。
- 値補間、Bid/Ask交換、clamp、forward fill、別provider fallbackは行っていない。

## 判定

3ペアとも「実装候補として調査を継続できる」状態である。ただし、研究consumerが利用可能とはまだ判定しない。次工程は、定期運転とは分離したonboarding jobで各ペアを1つずつ全履歴取得し、品質・lineage・Read APIを検証することである。
