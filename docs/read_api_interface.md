# Saxo DB Read API インターフェース仕様

更新日: 2026-07-20 JST

対象API: `v1`

契約revision: `1.2`

機械可読契約: [`specs/read_api_v1_openapi.yaml`](../specs/read_api_v1_openapi.yaml)

対象読者: `saxo_db`のデータを利用する外部の分析・戦略・可視化プロジェクト

## 1. 目的と利用原則

Read APIは、`saxo_market`の管理データを別プロジェクトへ読み取り専用で提供するlocalhostインターフェースです。外部プロジェクトはDBのtable、view、role、passwordへ依存せず、このAPIまたは検証済みParquetを利用します。

基本原則は次のとおりです。

- OHLC取得の正式な入口は`GET /api/v1/bars`とする。
- 利用前にinventory、coverage、freshness、qualityを確認する。
- datasetやsnapshotを固定した処理では`GET /api/v1/manifests`の識別情報を記録する。
- responseへ将来追加される未知のfieldは無視する。
- PostgreSQLのNUMERICはJSON文字列として受け取り、計算時はdecimal型へ変換する。
- 戦略側からDBまたはAPIへ書き込まない。

## 2. 接続モデル

### 2.1 起動

repository rootでPostgreSQLとRead APIを起動します。

```bash
docker compose -p saxo-market-data up -d postgres
.venv/bin/python -m market_db.read_api --port 8766
```

base URL:

```text
http://127.0.0.1:8766
```

health確認:

```bash
curl --fail http://127.0.0.1:8766/health
```

正常例:

```json
{
  "database": {
    "database_name": "saxo_market",
    "role_name": "saxo_app_reader",
    "statement_timeout": "30s",
    "transaction_read_only": "on"
  },
  "status": "PASS"
}
```

consumerはデータ取得前に`HTTP 200`かつ`status=PASS`を確認してください。

### 2.2 セキュリティ境界

- serverは`127.0.0.1`へ固定bindされます。
- 認証token、Saxo access token、account情報は不要です。
- DB接続は`saxo_app_reader`、read-only transaction、最大5接続、30秒statement timeoutです。
- 任意SQL、任意relation、file path、write endpointは公開しません。
- `POST`、`PUT`、`PATCH`、`DELETE`は`405 READ_ONLY_API`です。
- responseは`Cache-Control: no-store`です。

「外部プロジェクト」は、現時点では同一Mac上の別processを意味します。別host、VM、Docker container、Kubernetes、クラウドからの直接接続は現行契約の対象外です。

Docker container内の`127.0.0.1`はcontainer自身を指すため、そのままではhost上のAPIへ到達しません。APIを`0.0.0.0`へ変更して公開しないでください。container/remote連携が必要な場合は、認証、TLS、接続元制限、rate limit、監査logを備えた別gatewayを設計します。

別originのブラウザJavaScriptからは、現行serverにCORS headerがないため直接利用できません。server-side processから呼ぶか、consumer側backendでproxyしてください。

## 3. API区分

### 3.1 外部consumer向け安定契約

| Method | Path | 用途 |
|---|---|---|
| GET | `/` | service、version、read-only状態 |
| GET | `/health` | DB roleとread-only状態のhealth check |
| GET | `/api/v1/operations/{command}` | inventory、品質、運用情報 |
| GET | `/api/v1/bars` | PASS済みOHLCの期間取得 |
| GET | `/api/v1/snapshots/{snapshot_id}/bars` | 固定研究snapshotの検証済み1H OHLC |
| GET | `/api/v1/total-return` | 承認mapping済みETF total-return日次 |
| GET | `/api/v1/manifests` | dataset、research snapshot識別 |
| GET | `/api/v1/layer-counts` | 1H、4H、1Dの現在行数 |

### 3.2 Web UI支援API

`/api/v1/ui/*`はWeb UIと対話的なデータ探索のためのendpointです。系列検索、opaqueな`series_id`、chart marker、total-return折れ線を提供しますが、外部batch処理の長期安定契約にはしません。

| Method | Path | 用途 |
|---|---|---|
| GET | `/api/v1/ui/overview` | UI概要指標 |
| GET | `/api/v1/ui/series` | 系列検索・ページング |
| GET | `/api/v1/ui/series/{series_id}` | 系列詳細 |
| GET | `/api/v1/ui/chart-bars` | OHLCまたはtotal-return表示データ |
| GET | `/api/v1/ui/chart-marks` | quality marker |
| GET | `/api/v1/ui/quality-summary` | UI品質summary |

total-returnはnative OHLCとは別の安定契約`/api/v1/total-return`で取得します。UI支援APIは対話的な探索用として引き続き利用できます。

## 4. 共通規約

### 4.1 日時

- requestの`start`と`end`はtimezone offsetを含むISO-8601形式にする。
- `Z`はUTCとして使用できる。
- 期間は`[start, end)`で、startを含みendを含まない。
- 1H/4Hは`time_utc`、1Dは`session_date`を時系列keyにする。
- serverはrequest時刻をUTCへ正規化する。

有効:

```text
2026-07-15T00:00:00Z
2026-07-15T09:00:00+09:00
```

無効:

```text
2026-07-15T00:00:00
2026-07-15
```

### 4.2 数値

価格とvolumeは精度を保つためJSON numberではなく文字列です。

```json
{
  "open": "295.250000000000",
  "volume": "3568973.00000000"
}
```

Pythonでは`float`ではなく`decimal.Decimal`を推奨します。volumeはnullの場合があります。

### 4.3 品質

`/api/v1/bars`は`is_complete=true AND quality_status=PASS`だけを返します。ただし、barが存在することはデータが最新であることを意味しません。consumerは`freshness`、`coverage`、`quality`を確認します。OPEN/ACKNOWLEDGEDのERROR/CRITICAL eventは、`applicability=CURRENT`または`UNKNOWN`ならfail-closedで停止します。scope/applicability fieldが欠ける旧応答もUNKNOWNとして停止し、HISTORICALを推測しません。

### 4.4 versioning

- breaking changeは新しいmajor path、例:`/api/v2`で提供する。
- 同じ`v1`でfieldを追加する場合がある。
- consumerは必要なfieldだけを読み、未知のfieldを拒否しない。
- responseのfield削除、意味変更、型変更は`v1`では行わない。
- operationsとbarsのresponseは`api_version`、`contract_revision`、`generated_at_utc`を返す。
- consumerは`api_version=1`を確認し、対応済み`contract_revision`未満なら停止する。

## 5. 運用情報 `/api/v1/operations/{command}`

query parameter:

| Parameter | 必須 | Default | 最大 | 意味 |
|---|---:|---:|---:|---|
| `limit` | no | 200 | 1000 | response行数 |

allow-list:

| command | 参照内容 | consumerでの主な用途 |
|---|---|---|
| `inventory` | dataset、symbol、layer、期間、件数、品質、鮮度 | 利用可能データの発見 |
| `coverage` | 期待slot、実bar、missing、calendar外 | 期間の完全性確認 |
| `freshness` | watermark、最新完成bar、次の期待slot | 更新遅延の判定 |
| `runs` | ingestion run、状態、error code | 最新取込の成否確認 |
| `quality` | OPENのquality event | 未解決問題の確認 |
| `lineage` | source file、run、raw/curated/derived件数 | 由来・再現性確認 |
| `storage` | relationごとの使用量 | 運用監視 |
| `backups` | backup、restore検証状態 | 復旧可能性確認 |

例:

```bash
curl --fail 'http://127.0.0.1:8766/api/v1/operations/inventory?limit=100'
curl --fail 'http://127.0.0.1:8766/api/v1/operations/freshness?limit=100'
curl --fail 'http://127.0.0.1:8766/api/v1/operations/quality?limit=100'
```

response envelope:

```json
{
  "api_version": 1,
  "contract_revision": "1.2",
  "generated_at_utc": "2026-07-19T12:00:00Z",
  "command": "inventory",
  "row_count": 1,
  "rows": [
    {
      "symbol": "EEM",
      "layer": "curated",
      "price_basis": "etf_total_return",
      "min_time_utc": "2004-11-18T00:00:00Z",
      "max_time_utc": "2024-06-28T00:00:00Z",
      "row_count": 4935,
      "quality_status": "WARN",
      "freshness_status": "NOT_EVALUATED"
    }
  ]
}
```

各viewは運用上必要な列を追加する可能性があります。column名の厳密な一覧を固定する処理ではなく、必要列を名前で選んでください。

`quality` rowの主要field:

| Field | 説明 |
|---|---|
| `quality_event_id` | append-only event識別子 |
| `instrument_id` / `instrument_key` | 対象identity。global/run eventはnullの場合がある |
| `scope_kind` | `INSTRUMENT`, `SERIES`, `DATASET`, `RUN`, `LAYER`, `GLOBAL`, `UNKNOWN` |
| `applicability` | `CURRENT`, `HISTORICAL`, `UNKNOWN` |
| `current_blocker` | OPEN/ACK、ERROR/CRITICAL、CURRENT/UNKNOWNの導出値 |
| `applicability_reason` / `reviewed_at_utc` | 最新の運用review証跡 |

`current_blocker`はevent単体の状態であり、特定系列への適用可否までは表しません。consumerは`instrument_key`に加えて`affected_layer`、`horizon_minutes`、`price_basis`を要求系列と照合します。scopeが一致するCURRENT/UNKNOWNだけをblockし、scope不明は安全側でblockします。たとえばraw 1440分のCURRENT eventはraw日足をblockしますが、curated 1Hをblockしません。

`applicability=HISTORICAL`は最新reviewの`superseded_by_ingestion_run_id`と理由も確認してください。scopeが一致するCURRENTとUNKNOWNは利用不可です。2026-07-20のDMI1B review後は、legacy raw archive 5件がCURRENT、復旧済みatomic run 17件がHISTORICAL、UNKNOWNは0件です。

### 5.1 Atomic series status

外部projectの正式preflight:

```text
GET /api/v1/series-status?instrument_key=spy&layer=1h&price_basis=native_ohlc
```

初期契約はcanonical 1Hだけを対象とし、`instrument_key`、`layer=1h`、正しい`price_basis`を必須とします。存在しない組合せは`SERIES_NOT_FOUND`、4H/1Dは`INVALID_REQUEST`です。

identity、coverage、freshness、scope適合quality event、watermark、latest ingestion run、quality high-watermarkを、1つの`REPEATABLE READ / READ ONLY` transactionで取得します。`consistency.snapshot_marker`は同一snapshotの監査情報であり、異なるresponse間でdata versionが同じだと推定する用途には使いません。

主なresponse:

```json
{
  "api_version": 1,
  "contract_revision": "1.2",
  "generated_at_utc": "2026-07-20T00:00:00Z",
  "series": {
    "instrument_id": 9,
    "instrument_key": "spy",
    "layer": "1h",
    "horizon_minutes": 60,
    "price_basis": "native_ohlc"
  },
  "consistency": {
    "read_at_utc": "2026-07-20T00:00:00Z",
    "snapshot_marker": "...",
    "watermark_data_version": 0,
    "latest_ingestion_run_id": 105,
    "quality_event_high_watermark": 395032
  },
  "state": {
    "coverage_status": "WARN",
    "freshness_status": "STALE",
    "quality_status": "PASS",
    "eligibility_status": "BLOCKED",
    "eligibility_reasons": ["FRESHNESS_STALE"],
    "eligibility_warnings": ["COVERAGE_WARN"],
    "current_blockers": [],
    "unknown_blocker_count": 0,
    "historical_unresolved_event_count": 3
  },
  "components": {
    "coverage": {},
    "freshness": {},
    "latest_ingestion_run": {}
  }
}
```

`eligibility_status`:

- `ELIGIBLE`: componentが揃い、coverage/freshness/quality/latest runがPASS。
- `ELIGIBLE_WITH_WARNINGS`: blocking条件はないがcoverage WARNなどがある。
- `BLOCKED`: component欠損、STALE/FAIL/NOT_EVALUATED、非ACTIVE watermark、非PASS latest run、またはscope適合CURRENT/UNKNOWN blockerがある。

UNKNOWN ERROR/CRITICALは必ず`current_blockers`へ入り、`BLOCKED`になります。raw日足などscope不一致eventはcanonical 1H blockerへ混入しません。

## 6. OHLC `/api/v1/bars`

### 6.1 Request

```text
GET /api/v1/bars
```

| Parameter | 必須 | 値 | 意味 |
|---|---:|---|---|
| `instrument_key` | yes | 小文字化されるmarket key | 対象銘柄 |
| `layer` | yes | `1h`, `4h`, `1d` | データ層 |
| `start` | yes | timezone付きISO-8601 | inclusive lower bound |
| `end` | yes | timezone付きISO-8601 | exclusive upper bound |
| `limit` | no | default 200、最大10,000 | 最大返却行数 |
| `cursor` | no | 前responseの`next_cursor` | 同じqueryの次page。opaque値を保存・分解しない |

canonical 13の`instrument_key`:

```text
spy, iwm, efa, eem, vnq,
shy, ief, tlt, tip, lqd,
gld, eurusd, usdjpy
```

例:

```bash
curl --fail --get 'http://127.0.0.1:8766/api/v1/bars' \
  --data-urlencode 'instrument_key=iwm' \
  --data-urlencode 'layer=1h' \
  --data-urlencode 'start=2026-07-15T00:00:00Z' \
  --data-urlencode 'end=2026-07-16T00:00:00Z' \
  --data-urlencode 'limit=1000'
```

### 6.2 Response

```json
{
  "api_version": 1,
  "contract_revision": "1.2",
  "generated_at_utc": "2026-07-19T12:00:00Z",
  "instrument_key": "iwm",
  "layer": "1h",
  "start": "2026-07-15T00:00:00Z",
  "end": "2026-07-16T00:00:00Z",
  "row_count": 2,
  "truncated": false,
  "rows": [
    {
      "instrument_key": "iwm",
      "instrument_id": 6,
      "symbol": "IWM:arcx",
      "category": "equity_reit",
      "layer": "1h",
      "time_utc": "2026-07-15T13:30:00Z",
      "session_date": null,
      "price_basis": "native_ohlc",
      "open": "295.250000000000",
      "high": "296.440000000000",
      "low": "294.540000000000",
      "close": "296.280000000000",
      "volume": "3568973.00000000",
      "is_complete": true,
      "quality_status": "PASS"
    }
  ]
}
```

bar row:

| Field | Type | 説明 |
|---|---|---|
| `instrument_key` | string | DB内の安定market key |
| `instrument_id` | integer | DB内の安定instrument ID |
| `symbol` | string | provider symbol |
| `category` | string | データ管理category |
| `layer` | string | `1h`, `4h`, `1d` |
| `time_utc` | string or null | 1H/4Hのbar時刻 |
| `session_date` | string or null | 1Dのsession日付 |
| `price_basis` | string | 現行OHLCは`native_ohlc` |
| `open/high/low/close` | string | decimal価格 |
| `volume` | string or null | decimal volume |
| `is_complete` | boolean | 本endpointでは常にtrue |
| `quality_status` | string | 本endpointでは常にPASS |

`1d`では`time_utc=null`、`session_date=YYYY-MM-DD`です。日足をUTC午前0時の瞬間として解釈せず、取引sessionの日付として扱ってください。

### 6.3 `truncated`と期間分割

serverは`limit + 1`行の存在で`truncated`を判定し、responseには最大`limit`行を昇順で返します。

推奨方法:

1. consumer側で月単位などの重ならない期間`[start, end)`を先に作る。
2. 各期間を個別に取得する。
3. `truncated=false`をassertする。
4. `instrument_key + layer + time_utc/session_date + price_basis`の重複がないことを検証する。
5. `truncated=true`ならその期間をさらに分割する。

単に`limit`を増やし続けたり、切れたresponseを完全データとして保存したりしないでください。

## 7. 固定研究snapshot OHLC

```text
GET /api/v1/snapshots/{snapshot_id}/bars
```

current `/api/v1/bars`とは別契約です。serverはcurrent `saxo_market`をcutoffで切り出さず、default read-onlyの`saxo_research_v13`を`v13_research_reader`専用poolから直接読みます。Saxo tokenは不要です。

### 7.1 Request

| Parameter | 必須 | 値 | 意味 |
|---|---:|---|---|
| `snapshot_id` | yes | 正の整数path parameter | 固定snapshot ID |
| `instrument_key` | yes | market key | 対象銘柄 |
| `layer` | yes | 現在は`1h`のみ | snapshot内のデータ層 |
| `price_basis` | yes | `native_ohlc`または`bid_ask_mid` | 価格系列を一意化 |
| `start` | yes | timezone付きISO-8601 | inclusive lower bound |
| `end` | yes | timezone付きISO-8601 | exclusive upper bound |
| `limit` | no | default 200、最大10,000 | 最大返却行数 |

```bash
curl --fail --get 'http://127.0.0.1:8766/api/v1/snapshots/1/bars' \
  --data-urlencode 'instrument_key=spy' \
  --data-urlencode 'layer=1h' \
  --data-urlencode 'price_basis=native_ohlc' \
  --data-urlencode 'start=2024-06-28T13:00:00Z' \
  --data-urlencode 'end=2024-06-29T00:00:00Z' \
  --data-urlencode 'limit=100'
```

### 7.2 検証とresponse

metadata、全体件数・最大時刻、系列identity、返却barは1つの`REPEATABLE READ / READ ONLY` transactionから取得します。serverは次をすべて検証した場合だけ200を返します。

- database=`saxo_research_v13`、role=`v13_research_reader`、transaction read-only。
- `ops.research_snapshot.status=FROZEN`。
- snapshot rowのcontent manifest相対pathがallow-list内にある。
- manifest実ファイルのSHA-256が`snapshot_sha256`と一致する。
- plan、research line、source database、source inventory SHA、cutoff、row countsがDB登録値と一致する。
- `curated.market_bar`の全体件数・最大時刻がmanifestと一致し、cutoff後の行が0件。

主要response:

```json
{
  "api_version": 1,
  "contract_revision": "1.2",
  "snapshot": {
    "requested_snapshot_id": 1,
    "resolved_snapshot_id": 1,
    "snapshot_sha256": "c275d078...b63d6b",
    "snapshot_manifest_relative_path": "manifests/db2_research_snapshot_content.json",
    "cutoff_utc": "2024-06-28T23:59:59Z",
    "source_database": "saxo_market",
    "snapshot_database": "saxo_research_v13",
    "snapshot_marker": "..."
  },
  "query": {
    "instrument_key": "spy",
    "layer": "1h",
    "price_basis": "native_ohlc",
    "start": "2024-06-28T13:00:00Z",
    "end": "2024-06-29T00:00:00Z",
    "limit": 100
  },
  "integrity": {
    "status": "PASS",
    "curated_market_bar_rows": 329745,
    "curated_max_time_utc": "2024-06-28T20:00:00Z",
    "post_cutoff_rows": 0
  },
  "row_count": 7,
  "truncated": false,
  "next_cursor": null,
  "ordered_content_sha256": "0d5b1c9b...fb2594b",
  "rows": []
}
```

`ordered_content_sha256`は返却順のrow配列をcanonical JSON化したSHA-256です。同じsnapshot IDとquery parameterによる外部runでは、`snapshot_sha256`、`row_count`、`ordered_content_sha256`を一緒に保存してください。`integrity.curated_market_bar_rows`はquery結果ではなくsnapshot内のcurated 1H全体件数です。

### 7.3 Fail-closed

| 条件 | HTTP | `error_code` |
|---|---:|---|
| snapshot IDが存在しない | 404 | `SNAPSHOT_NOT_FOUND` |
| 系列またはprice basisが存在しない | 404 | `SNAPSHOT_SERIES_NOT_FOUND` |
| 4H/1Dを要求 | 409 | `SNAPSHOT_LAYER_NOT_AVAILABLE` |
| manifestが未検証・欠損 | 503 | `SNAPSHOT_NOT_VERIFIED` |
| DB metadata・件数・cutoff・SHA不一致 | 503 | `SNAPSHOT_INTEGRITY_FAILED` |
| write method | 405 | `READ_ONLY_API` |

これらの失敗時にcurrent `/api/v1/bars`へfallbackしないでください。固定4H/1Dが必要な場合はsnapshot 1を変更せず、別snapshot IDとmanifestを作る計画が必要です。

### 7.4 Cursor pagination（DMI4）

`truncated=true`の場合だけ`next_cursor`が返ります。次pageは元の`instrument_key`、`layer`、`price_basis`、`start`、`end`、`limit`を同一にして、`cursor`だけを追加してください。cursorはHMAC-SHA256署名済みのopaque値で、snapshot SHA、query条件、`time_utc + instrument_id + price_basis`の複合順序を含みます。`cursor`をURL decode後に改変したりqueryを変更したりした場合は、それぞれ`CURSOR_INVALID`（400）、`CURSOR_QUERY_MISMATCH`（409）になります。snapshotのSHAが変わった場合は`CURSOR_EXPIRED`（409）として途中結果を継続しません。

```bash
page1=$(curl --fail --get 'http://127.0.0.1:8766/api/v1/snapshots/1/bars' \
  --data-urlencode 'instrument_key=spy' --data-urlencode 'layer=1h' \
  --data-urlencode 'price_basis=native_ohlc' --data-urlencode 'start=2024-06-28T13:00:00Z' \
  --data-urlencode 'end=2024-06-29T00:00:00Z' --data-urlencode 'limit=100')
```

page連結後に`time_utc + instrument_id + price_basis`の重複0、昇順をconsumer側でも確認します。current `/api/v1/bars`は従来どおりbounded time-window取得であり、cursorを必須にしません。

## 8. dataset・snapshot・件数

### 8.1 `/api/v1/manifests`

parameterはありません。

```bash
curl --fail http://127.0.0.1:8766/api/v1/manifests
```

response:

```json
{
  "datasets": [
    {
      "source_dataset_id": "...",
      "dataset_name": "...",
      "provider": "Saxo OpenAPI",
      "environment": "SIM",
      "dataset_kind": "market_bar",
      "price_basis": "native_ohlc",
      "research_eligibility": "..."
    }
  ],
  "snapshots": [
    {
      "snapshot_id": 1,
      "plan_id": "...",
      "research_line_id": "...",
      "cutoff_utc": "2024-06-28T23:59:59Z",
      "source_database": "saxo_market",
      "snapshot_sha256": "...",
      "status": "PASS",
      "snapshot_manifest_relative_path": "...",
      "dump_relative_path": "...",
      "dump_sha256": "...",
      "dump_size_bytes": 0,
      "dump_pg_restore_list_pass": true
    }
  ]
}
```

再現可能な外部runでは、取得時刻、query parameter、source dataset ID、必要ならsnapshot SHA-256、consumer自身のcode versionを一緒に記録してください。

### 8.2 `/api/v1/layer-counts`

```bash
curl --fail http://127.0.0.1:8766/api/v1/layer-counts
```

1H、現行derivation versionの4H/1Dについて、現在の総行数を返します。件数は増分更新やfull refetchで変化するため、固定テスト値として埋め込まないでください。

## 9. ETF total-returnの取得

ETF total-returnは`curated.etf_total_return_daily`にnative OHLCとは別系列で保存されています。外部batch処理はstable endpoint、UI探索はUI支援APIを使用します。

### 9.0 Stable total-return endpoint

```bash
curl --fail --get 'http://127.0.0.1:8766/api/v1/total-return' \
  --data-urlencode 'instrument_key=IWM' \
  --data-urlencode 'start=2024-01-01T00:00:00Z' \
  --data-urlencode 'end=2024-07-01T00:00:00Z' \
  --data-urlencode 'limit=1000' \
  --data-urlencode 'eligibility=eligible'
```

このendpointは`catalog.series_instrument_mapping`の承認済みmappingだけを使用します。symbol文字列の暗黙joinは行いません。複数datasetが候補になる場合、`source_dataset_id`を明示しないrequestは`SOURCE_DATASET_REQUIRED`で拒否します。

responseのseriesは`price_basis=etf_total_return`、各rowの`value`はtotal-return indexです。native OHLCのopen/high/low/closeとして扱ってはいけません。`ordered_content_sha256`、`row_count`、`truncated`、`source.parity_status`をconsumer runへ保存してください。

`truncated=true`のときは`next_cursor`を返します。次pageでは同じ`instrument_key`、`start`、`end`、`limit`、`eligibility`を維持してください。cursorは承認mappingの`source_dataset_id`、source manifest SHA-256（state revision）、`session_date`をbindします。source datasetまたはstate revisionが変わった場合は`CURSOR_EXPIRED`（409）、query変更は`CURSOR_QUERY_MISMATCH`（409）、改変値は`CURSOR_INVALID`（400）です。source_dataset_idは初pageで明示した場合も、cursor付き次pageでは省略できます。

`eligibility=eligible`は`quality_status=PASS`だけを返します。`eligibility=stored_complete`はWARN/NOT_EVALUATEDを含む可能性があり、`NON_ELIGIBLE_STORED_COMPLETE_DATA_MAY_BE_INCLUDED`を返します。

mappingがない場合は`TOTAL_RETURN_MAPPING_NOT_FOUND`、未指定datasetが曖昧な場合は409 `SOURCE_DATASET_REQUIRED`、mapping・source dataの整合性が壊れている場合は503 `TOTAL_RETURN_INTEGRITY_FAILED`です。

### 9.1 系列を検索

```bash
curl --fail --get 'http://127.0.0.1:8766/api/v1/ui/series' \
  --data-urlencode 'role=TOTAL_RETURN_DAILY' \
  --data-urlencode 'symbol=IWM' \
  --data-urlencode 'limit=20' \
  --data-urlencode 'offset=0'
```

`data[].series_id`と`price_basis=etf_total_return`を確認します。`series_id`はopaque IDであり、分解・生成・永続的な業務keyとして使用しません。

### 9.2 chart dataを取得

```bash
SERIES_ID="<前のresponseに含まれるseries_id>"
curl --fail --get 'http://127.0.0.1:8766/api/v1/ui/chart-bars' \
  --data-urlencode "series_id=${SERIES_ID}" \
  --data-urlencode 'start=2024-01-01T00:00:00Z' \
  --data-urlencode 'end=2024-07-01T00:00:00Z' \
  --data-urlencode 'limit=10000' \
  --data-urlencode 'eligibility=eligible'
```

responseの`series_kind`は`line`で、各rowはOHLCではなく`session_date`と`value`を持ちます。

`eligibility`:

| 値 | 用途 |
|---|---|
| `eligible` | 利用可能と判定されたデータだけ。default |
| `stored_complete` | WARN/NOT_EVALUATEDを含む保存済み完成データの監査表示 |

`stored_complete`は研究・戦略利用への昇格ではありません。responseに`NON_ELIGIBLE_STORED_COMPLETE_DATA_MAY_BE_INCLUDED`が含まれます。

## 10. Python consumer例

この例ではconsumer側の依存として`requests`を使用します。

```python
from decimal import Decimal
import os

import requests


BASE_URL = os.getenv("SAXO_DB_READ_API_URL", "http://127.0.0.1:8766")


def get_json(path: str, *, params: dict | None = None) -> dict:
    response = requests.get(
        f"{BASE_URL}{path}",
        params=params,
        timeout=(2, 30),
        headers={"Accept": "application/json"},
    )
    response.raise_for_status()
    return response.json()


health = get_json("/health")
if health.get("status") != "PASS":
    raise RuntimeError(f"saxo_db is not healthy: {health}")

freshness = get_json("/api/v1/operations/freshness", params={"limit": 1000})
unsafe = [
    row for row in freshness["rows"]
    if row.get("freshness_status") in {"FAIL", "STALE", "NOT_EVALUATED"}
]
if unsafe:
    raise RuntimeError(f"freshness gate did not pass: {unsafe}")

payload = get_json(
    "/api/v1/bars",
    params={
        "instrument_key": "iwm",
        "layer": "1h",
        "start": "2026-07-15T00:00:00Z",
        "end": "2026-07-16T00:00:00Z",
        "limit": 10000,
    },
)
if payload["truncated"]:
    raise RuntimeError("requested interval must be split into smaller windows")

bars = [
    {
        **row,
        "open": Decimal(row["open"]),
        "high": Decimal(row["high"]),
        "low": Decimal(row["low"]),
        "close": Decimal(row["close"]),
        "volume": Decimal(row["volume"]) if row["volume"] is not None else None,
    }
    for row in payload["rows"]
]
```

この例のfreshness policyは厳格です。実際のconsumerは対象銘柄・layerに絞り込み、自身の用途に合う明示的なpolicyを実装してください。`NOT_EVALUATED`を暗黙にPASSへ変換してはいけません。

## 11. HTTP statusと再試行

| HTTP | error code | 意味 | consumer動作 |
|---:|---|---|---|
| 200 | なし | 正常 | bodyを検証 |
| 400 | `INVALID_REQUEST` | parameter、日時、limit、allow-list違反 | requestを修正。retryしない |
| 404 | `NOT_FOUND` / `SERIES_NOT_FOUND` | pathまたはUI系列なし | ID・pathを再確認。retryしない |
| 404 | `SNAPSHOT_NOT_FOUND` / `SNAPSHOT_SERIES_NOT_FOUND` | 固定snapshotまたは系列なし | ID・price basisを再確認。fallbackしない |
| 409 | `SNAPSHOT_LAYER_NOT_AVAILABLE` | 固定snapshotに要求layerなし | 新snapshot計画が必要。retryしない |
| 405 | `READ_ONLY_API` | write methodを使用 | GETへ修正。retryしない |
| 503 | `SNAPSHOT_NOT_VERIFIED` / `SNAPSHOT_INTEGRITY_FAILED` | manifest・DB内容の検証失敗 | 利用を停止し運用者が調査 |
| 503 | `DATABASE_UNAVAILABLE` | DB接続・server内部問題 | healthを確認し有限retry |

推奨retry policy:

- connect timeout 2秒、read timeout 30秒を目安に設定する。
- retry対象は接続断、timeout、`DATABASE_UNAVAILABLE`だけに限定する。
- 1秒、2秒、4秒など有限backoffにし、無限retryしない。
- GETはidempotentだが、400/404/405は再送しない。
- retry後も失敗した場合、古いcacheへ黙ってfallbackせずrunを停止する。

error response例:

```json
{
  "status": "FAILED",
  "error_code": "INVALID_REQUEST"
}
```

## 12. 大量データはParquetを使う

反復研究、長期間・複数銘柄の一括取得、consumer側で固定snapshotが必要な場合は、APIへ多数の小queryを連打せずParquet exportを使用します。

```bash
.venv/bin/python -m market_db.export_parquet \
  --instrument-key iwm \
  --layer 1h \
  --start 2025-01-01T00:00:00Z \
  --end 2026-01-01T00:00:00Z \
  --output iwm_1h_2025.parquet
```

制約:

- 出力は`exports/parquet/`配下のみ。
- layerは`1h`、`4h`、`1d`。
- 最大100,000行。
- 既存fileを上書きしない。
- ZSTD圧縮、SHA-256、DuckDB read-back件数をmanifestで検証する。
- HTTPで自動配信しない。consumerへ明示的に受け渡す。
- DBへ逆importしない。

consumerはParquet本体と対応manifestを一緒に保管し、利用前にSHA-256とrow countを検証してください。

## 13. 推奨consumer workflow

1. `/health`がPASSであることを確認する。
2. `inventory`でsymbol、layer、price basis、期間、件数を確認する。
3. `coverage`、`freshness`、`quality`を用途別policyで判定し、ERROR/CRITICALのCURRENT/UNKNOWNを遮断する。
4. `/api/v1/manifests`からdataset/snapshot識別情報を記録する。
5. currentデータは`/api/v1/bars`、固定研究入力は`/api/v1/snapshots/{snapshot_id}/bars`を使い分ける。
6. snapshot利用時はsnapshot ID、snapshot SHA、row count、ordered content SHAを保存する。
7. `truncated=false`、時系列昇順、重複なし、OHLC制約をconsumer側でも検証する。
8. 大規模・固定入力は検証済みParquetを使用する。
9. query、取得時刻、API major version、consumer code versionをrun artifactへ保存する。
10. 戦略結果とデータ品質結果を別artifactとして報告する。

## 14. 現在の制約と将来拡張

- APIの可用性SLAはない。ローカルprocessとして運用する。
- 認証、TLS、CORS、remote接続は提供しない。
- total-returnの安定endpointは`GET /api/v1/total-return`を提供する。snapshot-bound total-returnはDMI4以降の拡張対象。
- corporate action、cash distribution、point-in-time swapはOHLC APIに含まれない。
- FX calendarはSaxo live scheduleによる完全検証前は`NOT_EVALUATED`になり得る。
- streaming/WebSocketは提供しない。取得はbounded GETのみ。

将来拡張では、既存`v1`を破壊せず、利用目的、品質gate、security boundary、migration、test、manifestを先に定義します。
