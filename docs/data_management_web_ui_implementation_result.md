# データ管理 Web UI 実装結果（Phase DMUI4）

実施日: 2026-07-17 JST

状態: **PASS**

研究ゲートへの影響: **NONE（RT0以降の戦略評価は未実施）**

## 1. 実装結果

DBに格納されたデータの種類、期間、足、価格基準、品質、鮮度、取込run、lineage、backupを、SQLやCLIを使わず確認できる読み取り専用Web UIを実装した。

- URL: `http://127.0.0.1:8766/ui/overview`
- 画面: overview、inventory、series detail、quality、runs、operationsの6画面
- chart: TradingView Lightweight Charts 5.2.0を自己ホスト
- 表示系列: 正式1H、派生4H、派生1D risk、ETF total return日次
- 監査系列: raw/archive、reference/metadataを正式系列と分離
- DB接続: `saxo_app_reader`、read-only transaction、最大5接続、30秒timeout
- HTTP: GET/HEADのみ。token、account、任意SQL、任意relation、注文、取得・修復操作なし

## 2. 実DB照合

2026-07-17の実DBで次を照合した。値はUIへ固定せず、各API応答時にDBから取得する。

| 項目 | 実測 |
|---|---:|
| active dataset | 7 |
| canonical instrument | 13 |
| canonical 1H | 480,355行 |
| derived 4H | 128,469行 |
| derived 1D | 47,784行 |
| ETF total return | 54,285行 / 11銘柄 |
| raw/archive | 1,208,527行 |
| current freshness | STALE 11 / NOT_EVALUATED 2 / FAIL 0 |
| latest ingestion run | 105 / PASS |

IWM canonical 1Hは28,178行で、TradingViewローソク足、出来高、OHLC表を表示した。EURUSD derived 4Hは管理確認モードで402本の`NOT_EVALUATED` barを表示し、`NON_ELIGIBLE_STORED_COMPLETE_DATA_MAY_BE_INCLUDED`を固定表示した。IWM total returnはOHLCを捏造せず、total return indexの折れ線として表示した。

## 3. 性能

loopback上で同一endpointを5回測定した。

| endpoint | 5回の実測範囲 | 目標 |
|---|---:|---:|
| `/api/v1/ui/overview` | 0.827〜0.887秒 | p95 2秒以内 |
| `/api/v1/ui/series/<id>` | 1.747〜1.809秒 | 画面初期表示用 |
| `/api/v1/ui/chart-bars` 1,000本 | 0.776〜0.894秒 | p95 1秒以内 |

overviewは重いcoverage全計算を初期表示から分離した。series detailはinstrumentをSQLで先に限定し、約10.4秒から約1.8秒へ短縮した。品質画面はmissing/out-of-sessionを全13銘柄で計算するため、overviewとは別に明示的に読み込む。

## 4. 安全性とデータ契約

- migration `0014_data_management_ui_reader.sql`は`curated.etf_total_return_daily`のSELECTだけをreaderへ追加する。
- opaqueな24文字`series_id`をサーバー側でallow-list解決し、relation名をrequestから受け取らない。
- chartは`[start, end)`、最新最大1,000本、応答は昇順とし、過去追加時の重複timestampをエラーにする。
- `eligible`はcomplete/PASSのみ。`stored_complete`はcompleteのWARN/NOT_EVALUATEDを含み、必ず固定警告を返す。
- CSP、no-store、frame deny、no-referrerを設定し、CDN、telemetry、LocalStorage、SessionStorageを使用しない。
- vendor licenseとTradingView attributionを同梱・表示する。

## 5. 検証

- Python compile: PASS
- JavaScript syntax: PASS
- unit regression: 80 PASS / integration 23 SKIP（通常実行）
- full DB integration: 103 PASS（`SAXO_DB_INTEGRATION=1`）
- DB4 validator: PASS（DB1〜DB4、migration 0014、旧DB4親manifest、DMUI4拡張manifestを含む）
- browser E2E: 6画面、在庫filter/paging、1H/4H/1D candlestick、total return line、管理確認警告、TradingView attributionを確認
- browser console error: 0件
- Saxo order/precheck/write request: 0件

## 6. 証跡の継承

`manifests/db4_implementation_manifest.json`はDB4実装時点の不変証跡として変更しない。DMUIで変更した`market_db/read_api.py`等は、`manifests/data_management_web_ui_implementation_manifest.json`に現行size/SHA-256と親DB4 manifestのSHA-256を記録する。validatorは旧証跡の変更を黙認せず、DMUI4 manifestで明示的にsupersedeされたartifactだけを現行hashで再検証する。

## 7. 起動

```bash
.venv/bin/python -m market_db.read_api --port 8766
```

<http://127.0.0.1:8766/ui/overview> を開く。Saxo tokenは不要である。取得・Reconcileは別のoperator UI `127.0.0.1:8765`で扱い、このUIへ混在させない。
