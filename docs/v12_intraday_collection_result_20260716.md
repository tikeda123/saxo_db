# V12短期取引データ取得結果

取得日時：2026-07-16  
環境：Saxo OpenAPI Simulation  
データセット：`data/v12/intraday/20260716T115307Z/`

## 結論

対象13銘柄について、1時間足・4時間足の全26系列を取得した。API取得成功は26/26、総行数は525,381、raw Chart応答は471ページ、保存容量は約219 MBである。

1時間足は13/13系列で品質PASSかつ8年以上の履歴を満たした。4時間足はETF 11系列がPASSしたが、EURUSDとUSDJPYに合計9件のBid/Ask集計値不整合があり、FXカテゴリーの全時間足Gateは`BLOCKED_DATA`を維持した。

## カテゴリー別結果

| カテゴリー | 系列数 | 行数 | 最短履歴 | 品質PASS | 判定 |
|---|---:|---:|---:|---:|---|
| 株式・REIT | 10 | 181,099 | 16.07年 | 10/10 | PASS |
| 債券・Credit | 10 | 136,336 | 8.95年 | 10/10 | PASS |
| Gold | 2 | 36,216 | 16.07年 | 2/2 | PASS |
| FX | 4 | 171,730 | 9.39年 | 2/4 | BLOCKED_DATA |

## FX結果

- EURUSD 1時間足：59,952本、約9.42年、品質PASS
- USDJPY 1時間足：59,952本、約9.39年、品質PASS
- EURUSD 4時間足：25,910本、約16.08年、3件のBid/Ask不整合でFAIL_QUALITY
- USDJPY 4時間足：25,916本、約16.08年、6件のBid/Ask不整合でFAIL_QUALITY

異常値はraw応答と正規化CSVを変更せず保存した。自動補正、Ask/Bidの入替え、将来値による補間はしていない。

### EURUSD 4時間足

| UTC | 異常 |
|---|---|
| 2013-06-09T17:00:00Z | LowBid 1.31838 > LowAsk 1.31666 |
| 2015-01-04T22:00:00Z | LowBid 1.18837 > LowAsk 1.18726 |
| 2016-02-05T10:00:00Z | HighBid 1.12424 > HighAsk 1.12360 |

### USDJPY 4時間足

| UTC | 異常 |
|---|---|
| 2015-09-04T09:00:00Z | LowBid 118.674 > LowAsk 118.606 |
| 2015-09-06T17:00:00Z | LowBid 118.830 > LowAsk 118.799 |
| 2015-09-20T17:00:00Z | LowBid 119.846 > LowAsk 119.749 |
| 2015-10-28T17:00:00Z | LowBid 120.298 > LowAsk 120.043 |
| 2015-10-30T01:00:00Z | LowBid 120.470 > LowAsk 120.316 |
| 2016-04-08T17:00:00Z | LowBid 108.052 > LowAsk 108.041 |

## 利用境界

- Primary売買足として1時間足を使うための価格履歴Gateは4カテゴリーとも通過可能。
- 4時間足をFX Primaryまたは必須補助入力にする場合は、上記9件をraw証跡付きで除外するか、1時間足から決定論的に再集計して再監査する。
- ETFはSaxo raw OHLCであり、分配金込みTotal Returnではない。保有中の分配金とSplitを別台帳で処理する。
- FXの過去時点実現Swapは含まれない。日跨ぎ戦略に使用する場合はSwapデータGateが別途必要。
- トークン、AccountKey、口座識別子は保存していない。注文・Pre-checkは0件。

## 成果物

- `collection_spec.json`：取得仕様
- `collection_summary.csv`：26系列の件数・期間・品質Gate
- `dataset_manifest.json`：全保存ファイルのSHA-256とセキュリティ境界
- `normalized/*.csv`：銘柄・時間足別CSV
- `raw/<銘柄>/<時間足>/page_*.json`：Saxo Chart raw応答

## 次の作業

Phase ST0.1でPrimary売買足を1時間足に固定し、日足はRegime・Risk補助に限定する。その後、1時間足データを対象にSession、DST、取引時間、分配金、FX rollover回避、執行費用を監査してからST2へ進む。
