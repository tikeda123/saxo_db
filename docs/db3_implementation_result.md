# Phase DB3 実装結果

実施日: 2026-07-17 JST  
結論: **OFFLINE PASS / LIVE SIM BLOCKED_LIVE_SIM_TOKEN**

## 1. 判定

DB3のコード、migration、calendar、watermark、4H/1D派生、coverage/freshness、failure rollback、DataVersion full-refetch guardを実装し、実DBでoffline gateをPASSした。現在のprocessに`SAXO_ACCESS_TOKEN`がないため、Saxo SIM smoke、canonical 13のdetail/schedule/chart、直後の2回目runは未実施である。このためDB3総合PASS、DB4、RT0は解放しない。

## 2. 実装した取得・安全境界

- SIM base URL固定、GET endpoint allow-list、Etf/FxSpot限定、chart horizon 60・Count最大1200
- token redaction、response/errorのsanitized code、429の1/2/4秒有限retryとrate header要約
- canonical 13のUIC、AssetType、Symbol、Currency照合。drift時の自動代替なし
- raw JSONのrepository相対path・atomic保存・SHA-256。token/account関連fieldを除去
- Etf 20本、FxSpot 72本の実完成バーoverlap、`Mode=From` paging、境界重複排除
- Decimal正規化、ETF native OHLC、FX Bid/Ask保存とmid生成、最新sampleだけを未完成扱い
- 13銘柄全体のstage、quality、raw、curated、revision、watermark、4H/1Dを単一transaction化
- 失敗時はcurated/derived/watermarkをrollbackし、raw artifact、失敗run、sanitized quality eventだけを保持
- DataVersion不一致を`STALE_DATA_VERSION`で停止。対象1銘柄だけのguard付き`Mode=UpTo` full refetchを実装
- 注文、precheck、portfolio endpoint呼出し0。account identifier保存0

## 3. 実DB結果

| 項目 | 実測 |
|---|---:|
| canonical watermark | 13、全件`ACTIVE` |
| session interval | ETF 4,696 / FX 4,696 |
| calendar割当 | ETF 11 `VERIFIED` / FX 2 `PROVISIONAL` |
| derived 4H | 107,623 |
| derived 4H analysis eligible | 78,232 |
| derived 1D | 44,292 |
| derived 1D analysis eligible | 39,336 |
| derived quality FAIL | 0 |
| staging rows | 0 |
| research DB DB3 migration | 0 |
| research DB post-cutoff rows | raw 0 / curated 0 / total-return 0 |

coverageはETF 11銘柄が`WARN`、FX 2銘柄が`NOT_EVALUATED`、`FAIL`は0である。ETFではcalendar期待slotに対するmissing 433行とout-of-session 914行を分離して表示した。旧データを削除せず、calendar外行を4H/1D派生からだけ除外した。FXはSaxo live trading scheduleとの照合前なので、missing 608行・out-of-session 2,725行を観測値として表示しつつ判定は`NOT_EVALUATED`に保った。

freshnessは、live増分をまだ実行していないためETF 11銘柄が`STALE`、FX 2銘柄がcalendar provisionalにより`NOT_EVALUATED`である。この状態をPASSへ偽装しない。

## 4. Migration

| migration | database | SHA-256 |
|---|---|---|
| `0010_db3_incremental_support.sql` | `saxo_market`のみ | `20ad78cf0dfe42f3c38c8b8dbba3c7bedf1fa6e53c65e9e0ea2fbafa6722166a` |
| `0011_db3_coverage_refinement.sql` | `saxo_market`のみ | `09d059660a4e493a33d8b372c3228f932662b2a76efd14c60d777e5440daa4e0` |
| `0012_db3_full_refetch_guard.sql` | `saxo_market`のみ | `ddf6ce3702ec7c7c9ceaf42e679f3bde77174beb916a2d2fa7a6ada8dc7c6b03` |

`0010`の初回試行は既存view列順の互換性検査でtransaction全体がrollbackした。既存列を維持して新列を末尾へ追加するよう修正後に適用した。適用済みmigrationは以後変更していない。実データでcalendar外とmissingを分ける必要が判明したため、適用済み`0010`を改変せず`0011`で補正した。

## 5. 検証

- unit/fixture/static/integration: **54 passed**
- migration checksum: PASS
- Docker/PostgreSQL health・UTC・role境界: PASS
- calendar DST、祝日、短縮日、例外休場fixture: PASS
- From/UpTo境界包含paging・cursor advance: PASS
- derived再実行冪等性: PASS
- curated/watermark/derived rollback fixture: PASS
- full-refetch guardのACTIVE watermark拒否: PASS
- DB3 offline validator: PASS
- DB3総合validator: `BLOCKED_LIVE_SIM_TOKEN`、exit 2

## 6. 次に許可する作業

session-only tokenをfile・chatへ保存せず、runbookに従ってsmoke、通常増分runを直後に2回、DB3 validatorを実行する。live gateがすべてPASSした場合だけDB3をPASSへ変更できる。DataVersion不一致時だけ、対象銘柄のguard付きfull-refetchを使う。

DB4のread API、一般backup/restore、retention、Parquet/DuckDB export、および特徴量、strategy、PnL、WFO、Holdoutは引き続きLOCKEDである。
