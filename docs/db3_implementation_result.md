# Phase DB3 実装結果

実施日: 2026-07-17 JST  
結論: **DB3 PASS / DB4 NEXT**

## 1. 判定

DB3のコード、migration、calendar、watermark、4H/1D派生、coverage/freshness、failure rollback、DataVersion full-refetch guardを実装し、offline/live両gateをPASSした。Saxo SIMのcanonical 13全銘柄は`ACTIVE`となり、EURUSD/USDJPYの限定隔離付きfull-refetch、通常run 104・105の連続PASS、DB3総合validator PASSを確認した。DB4だけを次工程として解放し、RT0はDB4 PASSまでLOCKEDを維持する。

## 2. 実装した取得・安全境界

- SIM base URL固定、GET endpoint allow-list、Etf/FxSpot限定、chart horizon 60・Count最大1200
- token redaction、response/errorのsanitized code、429・一時的network例外の1/2/4秒・最大4attempt有限retryとrate header要約
- canonical 13のUIC、AssetType、Symbol、Currency照合。drift時の自動代替なし
- raw JSONのrepository相対path・atomic保存・SHA-256。token/account関連fieldを除去
- Etf 20本、FxSpot 72本の実完成バーoverlap、`Mode=From` paging、境界重複排除
- Decimal正規化、ETF native OHLC、FX Bid/Ask保存とmid生成、最新sampleだけを未完成扱い
- 13銘柄全体のstage、quality、raw、curated、revision、watermark、4H/1Dを単一transaction化
- 失敗時はcurated/derived/watermarkをrollbackし、raw artifact、失敗run、sanitized quality eventだけを保持
- DataVersion不一致を`STALE_DATA_VERSION`で停止。対象1銘柄だけのguard付き`Mode=UpTo` full refetchを実装
- 過去FxSpot High/Low交差をfull-refetch限定で最大10件・0.01%以下だけ無補正隔離し、raw原本、`rejected_rows`、解決済みWARNへ記録
- localhost限定operator UIから固定`reconcile` jobを起動し、tokenを子process環境だけへ渡すAI運用経路
- 注文、precheck、portfolio endpoint呼出し0。account identifier保存0

## 3. 実DB結果

| 項目 | 実測 |
|---|---:|
| canonical watermark | `ACTIVE=13` |
| session interval | ETF 4,696 / FX 4,696 |
| calendar割当 | ETF 11 `VERIFIED` / FX 2 `PROVISIONAL` |
| derived 4H | 128,469 |
| derived 4H analysis eligible | 78,254 |
| derived 1D | 47,784 |
| derived 1D analysis eligible | 39,347 |
| derived quality FAIL | 0 |
| staging rows | 0 |
| research DB DB3 migration | 0 |
| research DB post-cutoff rows | raw 0 / curated 0 / total-return 0 |

coverageはETF 11銘柄が`WARN`、FX 2銘柄が`NOT_EVALUATED`、`FAIL`は0である。ETFではcalendar期待slotに対するmissing 433行とout-of-session 914行を分離して表示した。旧データを削除せず、calendar外行を4H/1D派生からだけ除外した。FXはSaxo live trading scheduleとの照合前なので、missing 872行・out-of-session 4,693行を観測値として表示しつつ判定は`NOT_EVALUATED`に保った。

freshnessはSTALE 11、NOT_EVALUATED 2、FAIL 0である。ETFのSTALEは米国市場休場時間帯に対する運用表示で、watermarkの`data_status`は13銘柄すべて`ACTIVE`である。FXはcalendarがprovisionalなのでPASSへ偽装せず`NOT_EVALUATED`を維持する。

### 3.1 Live DataVersion復旧結果

`reconcile`と個別full-refetchにより、11 ETFに加えてEURUSDとUSDJPYもPASSした。EURUSD full-refetch run 100は59,952 revisionを更新し、過去High/Low交差5件を隔離した。USDJPYは最初のfull-refetch run 102が74 page取得後の一時的network例外で`FAILED_NETWORK`となりDB変更をrollbackしたため、GET限定・1/2/4秒・最大4attemptの有限retryを追加した。再実行run 103は59,952 revisionを更新し、過去High/Low交差9件を隔離してPASSした。

いずれも`manual_db3_full_refetch`・FxSpot・過去High/Lowだけ、最大10 unique rowかつ0.01%以下、最新sample以外という全条件を満たす。値をswap・補間・clamp・上書きせず、raw JSON、SHA-256、元Bid/Ask、timestamp、artifact path、`rejected_rows`、解決済みWARNを保持した。閾値外なら従来どおり全runをFAILする。

復旧後の通常run 104・105は、いずれもcanonical 13系列を成功処理し、新規0件、revision/update 2件、reject 0件で連続PASSした。形成中FX sample以外に不要な差分はなく、`reconcile`最終結果は`consecutive_normal_passes=2 / status=PASS`、注文・precheck送信0件である。

AI側運用のため、Web UIへユーザーがtokenを入力し、固定`reconcile`を子processとして起動するlocal operatorを追加した。tokenをbrowser storage、cookie、file、DB、log、command引数へ保存せず、AIはtoken値を読まずにjob開始とsanitized progressの監視を行う。一般DB API・任意command・注文機能は追加していない。

## 4. Migration

| migration | database | SHA-256 |
|---|---|---|
| `0010_db3_incremental_support.sql` | `saxo_market`のみ | `20ad78cf0dfe42f3c38c8b8dbba3c7bedf1fa6e53c65e9e0ea2fbafa6722166a` |
| `0011_db3_coverage_refinement.sql` | `saxo_market`のみ | `09d059660a4e493a33d8b372c3228f932662b2a76efd14c60d777e5440daa4e0` |
| `0012_db3_full_refetch_guard.sql` | `saxo_market`のみ | `ddf6ce3702ec7c7c9ceaf42e679f3bde77174beb916a2d2fa7a6ada8dc7c6b03` |

`0010`の初回試行は既存view列順の互換性検査でtransaction全体がrollbackした。既存列を維持して新列を末尾へ追加するよう修正後に適用した。適用済みmigrationは以後変更していない。実データでcalendar外とmissingを分ける必要が判明したため、適用済み`0010`を改変せず`0011`で補正した。

## 5. 検証

- unit/fixture/static/integration: **74 passed**（非統合51、DB1/DB2統合16、DB3統合7）
- migration checksum: PASS
- Docker/PostgreSQL health・UTC・role境界: PASS
- calendar DST、祝日、短縮日、例外休場fixture: PASS
- From/UpTo境界包含paging・cursor advance: PASS
- derived再実行冪等性: PASS
- curated/watermark/derived rollback fixture: PASS
- full-refetch guardのACTIVE watermark拒否: PASS
- DB3 offline validator: PASS
- DB3総合validator: PASS（`ACTIVE=13`、最新通常run 104・105 PASS）、exit 0

## 6. 次に許可する作業

次に許可する作業はDB4のread API、一般backup/restore、retention、Parquet/DuckDB read-only export、runbook drillだけである。

特徴量、strategy、PnL、WFO、Holdoutを含むRT0はDB4 PASSまでLOCKEDである。過去の`db3_atomic_run_gate` OPEN eventは失敗runの監査履歴として保持し、RESOLVED化する場合は`saxo_ops_operator`、operator label、非機密resolution noteを用いる。
