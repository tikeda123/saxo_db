# 別Mac用CSV bootstrap監査

更新日: 2026-08-07 JST

結論: 既存69 CSVはpublic GitHubへ追加しない。Git管理するのは、外部データを含まない人工smoke seed 3 CSVだけとする。

## 1. 判定

`tikeda123/saxo_db`は2026-08-07確認時点でGitHubの`visibility=public`である。既存bundleはSaxo OpenAPI市場データ、Yahoo Finance由来adjusted値、FRED系列、そこからの派生値を含む。秘密値scanは0件だったが、公開再配布権は確認できない。

- Saxoの[Data disclaimer](https://www.home.saxo/markets/data-disclaimer)はmarket dataの第三者配布を禁止している。
- Saxoの[OpenAPI core business concepts](https://www.developer.saxo/openapi/learn/core-business-concepts)は第三者applicationでのmarket data利用に追加license/agreementが必要な場合があると明記する。
- Yahooの[Terms of Service](https://legal.yahoo.com/us/en/yahoo/terms/otos/index.html)はYahooまたはvendorが提供するcontentの権利を留保する。bundle manifest自身もYahoo terms確認前の再配布を認めていない。
- FREDの[Terms of Use](https://fred.stlouisfed.org/legal/terms/)は系列ごとの第三者権利・copyright制限とattribution要件を残す。

このため「認証情報がない」「各fileがGitHubの100 MB上限未満」だけでは公開可能と判定しない。Git LFSも権利問題を解消せず、今回は導入しない。

## 2. 既存69 CSVの正確なinventory

machine-readableな全path、origin path、row count、size、SHA-256は[`manifests/import_file_inventory.csv`](../manifests/import_file_inventory.csv)の69 recordを正本とする。

| group | files | rows | bytes | 内容 | public Git判定 |
|---|---:|---:|---:|---|---|
| `saxo_intraday` | 27 | 525,407 | 103,538,725 | Saxo SIM 1H/4Hとcollection summary | BLOCKED_LICENSE |
| `saxo_multi_asset_daily` | 8 | 52,692 | 11,903,292 | Saxo SIM legacy日次 | BLOCKED_LICENSE |
| `saxo_ETF_daily_raw` | 14 | 58,592 | 11,727,450 | Saxo SIM ETF日次・instrument metadata | BLOCKED_LICENSE |
| `ETF11_external_sources` | 14 | 90,727 | 14,685,787 | Yahoo Finance ETF、FRED macro | BLOCKED_MIXED_RIGHTS |
| `ETF11_curated_total_return` | 1 | 54,285 | 18,521,097 | Yahoo/FRED由来の派生total-return | BLOCKED_DERIVED_RIGHTS |
| `RA0_analysis_baseline` | 5 | 105 | 27,308 | 上記市場データからの研究集計 | BLOCKED_DERIVED_RIGHTS |
| **total** | **69** | **781,808** | **160,403,659** | 約153.0 MiB | **DO_NOT_UPLOAD** |

最大fileは`data/import/daily/curated_etf_total_return/etf_daily.csv`の18,521,097 bytesである。単一file sizeはGitHub上限の直接blockではないが、現在約2.5 MBのpublic repositoryを160 MB以上増やし、履歴から容易に除去できない。容量はlicense判定と独立した追加riskである。

## 3. 機密・実データ・backup混入監査

69 CSVのheaderと内容に対し、access/refresh token、Bearer、AccountKey、ClientKey、private key、JWT形状、email形状をscanした。一致内容を出力せずfile名だけを結果化したところ、matchは0件だった。CSVはDB dump、Docker volume、Keychain、`.runtime`、log、backupではない。

ただし次を含む実市場データである。

- Saxo UIC、symbol、DataVersion、Bid/Ask、OHLC、volume、取得時刻
- Yahoo Financeのadjusted close、dividend、split由来値
- FRED seriesの観測値
- 上記から計算したtotal-return/研究統計

PII/secretがないことは再配布許可を意味しない。`data/import/**/*.csv`のGit ignoreは維持する。

## 4. Git管理する最小seed

[`bootstrap/seed`](../bootstrap/seed/README.md)にproject-authored人工CSVを追加した。

| file | rows | bytes | 役割 |
|---|---:|---:|---|
| `instruments.csv` | 11 | 643 | ETF11互換のsynthetic identity |
| `market_bars_1h.csv` | 22 | 1,128 | 各銘柄2本の人工1H OHLC |
| `total_return_daily.csv` | 22 | 552 | 各銘柄2日の人工index |
| **total** | **55** | **2,323** | interface smoke only |

値は実市場値ではない。symbol、provider、environment、source、quality、dataset metadataの全てをsyntheticとして保存し、`SYNTHETIC_BOOTSTRAP_ONLY`、`NOT_EVALUATED`、inactive datasetとする。Saxo/Yahoo/FREDへのrequestは行わない。

`manifest.json`がrow count、size、SHA-256、upstream dataなしを固定し、[`scripts/verify_bootstrap_seed.py`](../scripts/verify_bootstrap_seed.py)がofflineで次を検証する。

- 3 CSVのhash、size、row count、header
- instrument/UIC/time keyのunique性
- null/nonpositive、OHLC不整合、時刻形式
- ETF11 coverage各2行
- token/JWT/private-key形状がないこと

## 5. migrationとimportの順序

既存migration `0019_total_return_mapping.sql`は11 ETF dataset/mappingの存在を検証するため、空DBに全migrationを先に適用してはならない。`market_db.migrate --through`を追加し、clean bootstrapを次の固定順序にする。

```bash
.venv/bin/python scripts/verify_bootstrap_seed.py
.venv/bin/python -m market_db.migrate all --through 0018
.venv/bin/python -m market_db.bootstrap_seed verify
.venv/bin/python -m market_db.bootstrap_seed import
.venv/bin/python -m market_db.bootstrap_seed status
.venv/bin/python -m market_db.migrate apply
.venv/bin/python -m market_db.migrate validate
```

importerは次を満たさない限りwrite前に停止する。

- migration 0018適用済み、0019未適用
- source dataset、instrument、source file、raw/curated/total-returnが空
- seed検証PASS

seed DBはmigration/import/API配線確認専用で、scheduler、Saxo OAuth、Strategy consumer、research、paper/live operationへ使わない。正規データを後から同じDBへ混在させない。

## 6. Read API確認

full migration後にloopback serviceを起動する。

```bash
.venv/bin/python -m market_db.read_api_service start
.venv/bin/python -m market_db.read_api_preflight --format json
curl --fail http://127.0.0.1:8766/health
curl --fail 'http://127.0.0.1:8766/api/v1/ui/series?role=TOTAL_RETURN_DAILY&limit=20'
```

確認するのはhealth、reader role、read-only transaction、CSV→lineage→UI/APIの配線だけである。人工行は`NOT_EVALUATED`/staleでも正常であり、`PASS_DATA_GATE`、current data、official close、provider total-returnと読み替えない。

## 7. 正規69 CSVを使う場合

正規bundleを保持する権利と端末間transfer権限を利用者が別途確認できた場合だけ、public GitHub以外のアクセス制御済み経路で新Macのignored `data/import/`へ置く。Git add、release attachment、LFS、public URLを使わない。

```bash
.venv/bin/python -m market_db.migrate all --through 0018
.venv/bin/python -m market_db.import_legacy verify
.venv/bin/python -m market_db.import_legacy import
.venv/bin/python -m market_db.migrate apply
.venv/bin/python -m market_db.migrate validate
.venv/bin/python -m market_db.research_snapshot create
.venv/bin/python -m market_db.research_snapshot status
```

`verify`の69 files / 781,808 rows / 160,403,659 bytes / 全SHA-256一致が必須である。不一致をCSV編集、hash書換え、行削除で解消しない。

## 8. 非実施

- 既存69 CSVのGit add/push: 0
- DB migration適用・DB import/write: 0
- Saxo/Yahoo/FRED API request: 0
- OAuth/secret/Keychain操作: 0
- scheduler、注文、precheck、口座、資金操作: 0
