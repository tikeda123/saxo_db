# データ管理 Web UI 仕様（Phase DMUI0）

作成日: 2026-07-17 JST

状態: **IMPLEMENTED / DMUI4 PASS**

対象: ローカル単一ユーザー向け `saxo_market` データ管理・可視化 UI

## 1. 目的

利用者が、DBに「何のデータが」「どの期間」「どの足・価格基準・品質で」保存されているかを、SQLやCLIを使わず確認できるようにする。各時系列はTradingViewのUIコンポーネントで表示する。

このUIはデータの観測・監査用であり、取得、修正、削除、注文、戦略評価は行わない。DB1〜DB4の読み取り境界を維持し、RT0以降の研究ゲートにも影響を与えない。

## 2. 利用者と利用場面

- 主利用者: ローカル環境でDBを運用するユーザー、データ研究者
- 主な判断:
  - 利用可能な銘柄、カテゴリ、データ層、足種、期間を確認する
  - 最新データがいつまであり、更新が遅れていないか確認する
  - OHLC、出来高、欠損、重複、セッション外、改訂の有無を確認する
  - 取得実行、由来、品質判定、バックアップまで追跡する
- 表示言語: 日本語。時刻表示はJST/UTC切替、DB/APIは常にUTC
- 対象端末: デスクトップ優先。タブレットは閲覧可能、スマートフォンは要約表示

## 3. 採用方針

### 3.1 UIとDBの境界

```text
Browser
  -> http://127.0.0.1:8766/ui/
  -> same-origin /api/v1/*
  -> saxo_app_reader / read-only transaction
  -> saxo_market
```

- 既存DB4 Read APIへ静的UIを同居させ、CORSと別Webサーバーを増やさない。
- 既存の取得・Reconcile操作UI `127.0.0.1:8765` は変更しない。
- ブラウザはDBへ直接接続しない。DBパスワード、Saxo token、account identifierを受け取らない。
- UI/APIはGET/HEADのみとし、任意SQL、任意relation名、書き込みrouteを追加しない。

### 3.2 TradingViewコンポーネント

初期版は **TradingView Lightweight Charts 5.x** をローカルへ固定バージョンで同梱する。

- 採用理由: OHLCローソク足、折れ線、出来高、時刻軸を軽量に実装でき、読み取り専用のデータ管理画面に必要十分である。
- CDNは使わず、UIから外部通信を発生させない。
- Apache-2.0のライセンスとTradingViewへの帰属表示・リンクを画面と配布物に含める。
- Advanced Chartsは、描画ツール、組込み指標、レイアウト保存などが必要になり、かつ公式リポジトリの利用条件を満たした場合のPhase DMUI2候補とする。
- Trading Platform/Broker API、注文パネル、precheckは対象外とする。

TradingViewのチャートライブラリ自体は市場データを持たないため、本プロジェクトのRead APIをdata sourceとして接続する。Advanced Chartsへ移行する場合も、Datafeed APIの`getBars`契約に合わせて昇順・`[from, to)`・`countBack`を処理する。

## 4. 管理対象データの分類

UIは全データを一つの件数へ混ぜず、次の役割を常に表示する。

| 役割ラベル | DB上の主対象 | 画面での扱い |
|---|---|---|
| `CANONICAL 1H` | `curated.market_bar` | 完了かつ品質PASSの正式な1時間OHLC。ローソク足表示対象 |
| `DERIVED 4H` | `derived.market_bar_4h` | 正式1Hから決定的に集計した4時間OHLC。ローソク足表示対象 |
| `DERIVED 1D RISK` | `derived.market_bar_1d_risk` | 正式1Hから集計した日足リスク系列。ローソク足表示対象 |
| `TOTAL RETURN DAILY` | `curated.etf_total_return_daily` | 調整済終値・total return index。Saxo OHLCとは別系列。原則折れ線表示 |
| `RAW / ARCHIVE` | `raw.market_bar_revision` | 取込証跡・改訂監査。正式系列として初期選択しない |
| `REFERENCE / METADATA` | `raw.reference_observation` | 参照値・研究メタデータ。OHLC件数と混同しない |

注意事項:

- raw 4Hは監査・比較用であり、正式4Hではない。特にraw FX 4Hのbid/ask交差品質問題を正式系列へ混入させない。
- `native_ohlc`、`bid_ask_mid`、`etf_total_return`などのprice basisを明示する。
- raw、curated、derivedを合計した「総OHLC件数」だけを主指標にしない。同じ市場時点の重複計上を避ける。
- 現在のDB例では正式1H対象は13銘柄、対応足は1H/4H/1Dである。件数・期間は更新されるため、画面ではAPI取得時刻を添えて動的表示する。

### 4.1 現在の在庫スナップショット

2026-07-17 JSTに`analytics.v_data_inventory`を照合した時点の例である。これは画面設計を検証するためのsnapshotであり、UIの固定値にはしない。

| 系列 | 銘柄 | 行数 | 主な収録期間 UTC | price basis |
|---|---|---:|---|---|
| 正式1H | ETF等11銘柄 + EURUSD/USDJPY | 480,355 | FXは2010-06-17/18〜2026-07-17、ETF等は銘柄により2010-06-21、2016-02-02、2017-08-02〜2026-07-16 | ETF等=`native_ohlc`、FX=`bid_ask_mid` |
| 派生4H | 正式1Hと同じ13銘柄 | 128,469 | 各銘柄の正式1H範囲内 | 同上 |
| 派生1D | 正式1Hと同じ13銘柄 | 47,784 | 各銘柄の正式1H範囲内 | 同上 |
| ETF total return日次 | EEM/EFA/GLD/IEF/IWM/LQD/SHY/SPY/TIP/TLT/VNQ | 54,285 | 2004-11-18〜2024-06-28 | `etf_total_return` |

正式1HのETF等はEEM、EFA、GLD、IEF、IWM、LQD、SHY、SPY、TIP、TLT、VNQである。1時間足の行数は銘柄ごとに約15,693〜28,184、FXは約102,600であり、単純な銘柄数だけでなく行数と期間を併記する必要がある。

### 4.2 OHLCの意味

- ETF等の1H `native_ohlc`: Saxo chart responseのOpen/High/Low/Closeを保持する。
- FXの1H `bid_ask_mid`: Open/High/Low/Closeごとに`(Bid + Ask) / 2`を計算したmid OHLCで、元のbid/ask OHLCも監査列として保持する。bidがaskを上回る不正sampleは品質規則で拒否または隔離する。
- 派生4H/1D: complete/PASSの正式1Hだけから、Open=先頭、High=最大、Low=最小、Close=末尾、Volume=合計として生成する。4Hはsession open起点のbucket、1Dはsession date単位である。
- Total return日次: adjusted close、dividend、splitからなる調整系列であり、Saxoの未調整ローソク足とは意味が異なる。初期chartはtotal return indexの折れ線を正とする。

## 5. 画面構成

### 5.1 データ概要 `/ui/overview`

最初の5秒でDBの現在状態を理解できる要約画面とする。

上段カード:

1. 有効データセット数
2. 正式系列の銘柄数
3. 正式1H行数
4. 派生4H行数
5. 派生1D行数
6. 更新要確認の系列数（STALE/WARN/FAIL）
7. 最新の取込実行結果と完了時刻
8. 最新backup/restore smoke結果

下段:

- カテゴリ別・足別の系列数と行数（積み上げ棒）
- 銘柄×足の期間ヒートマップ
- 現在の品質・鮮度状態一覧
- 最近の取込runタイムライン
- すべてのカードに`as of <server UTC>`を表示

### 5.2 データ在庫 `/ui/inventory`

dataset → instrument → layer/price basisの階層を一覧する。

フィルタ:

- 正式系列のみ / 全データ
- dataset、役割、category、symbol、layer、price basis
- quality、freshness、calendar verification
- 期間開始・終了、active/inactive

列:

- 役割、dataset、銘柄、カテゴリ、足、price basis
- 行数、最古時刻、最新時刻、最新complete時刻、収録期間
- quality、freshness、calendar verification
- 最新ingestion run、source manifest

初期ソートは「要対応状態を上、category、symbol、layer」とし、ページングと列固定を行う。CSV出力や任意SQLは初期版に含めない。

### 5.3 系列詳細 `/ui/series/<series_id>`

ヘッダー:

- symbol、instrument key、category、asset type、currency、exchange
- layer、price basis、役割、derivation version
- 最古/最新/最新complete、行数、品質、鮮度、calendar verification

チャート:

- OHLCはローソク足、total return indexは折れ線
- 出来高が存在する系列だけ下段volume histogramを表示
- 選択可能な足はDBに保存済みの`1H`、`4H`、`1D`だけ。ブラウザ内で5分足等を疑似生成しない
- 初期表示500〜1,000本、左スクロールで過去を追加読込、1応答最大10,000本
- 期間プリセット: 1M、3M、6M、1Y、3Y、全期間
- JST/UTC、fit content、OHLC tooltip、十字線、価格スケール切替
- APIの昇順データを使用し、同一timestamp重複を表示前にエラーとして検知
- 日足は00:00 UTCをtimeとして扱い、session dateをtooltipへ表示
- 品質eligibleなcomplete/PASSのみを初期表示する
- 運用者は`管理確認モード`へ切替でき、completeだがWARN/NOT_EVALUATEDの保存済みbarも確認できる。この場合はchart全体へ非eligibleの警告を固定表示し、研究利用可能とは表示しない
- incomplete/FAIL barは初期版ではローソク足へ混入させず、除外件数と時刻を品質tab/markerで確認する

チャート下部タブ:

- `データ`: 表示範囲のtime/OHLC/volume/qualityを表形式で確認
- `カバレッジ`: expected、actual、missing、out-of-session、duplicate、incomplete
- `品質`: 現在の判定とルール別イベント
- `由来`: source dataset、ingestion run、manifest、derivation version
- `定義`: price basis、時刻境界、使用可否、既知の制約

チャートマーカー:

- 欠損区間、incomplete、revision、品質event、取込境界を異なる記号で表示する。
- 現行APIが個別時刻を提供しない集計値は、チャート上へ推測配置せずタブ内の集計として表示する。

### 5.4 品質・鮮度 `/ui/quality`

- 「現在の利用可否」と「過去run由来の未解決監査event」を別セクションにする。
- historical OPEN eventが存在するだけで現在の正式系列全体をFAIL表示しない。
- 銘柄×足の状態matrix、missing率、duplicate、incomplete、out-of-sessionを表示する。
- `PROVISIONAL` calendarや`NOT_EVALUATED`はPASSへ丸めず、その理由と制約を表示する。
- FAIL/STALE/WARN/NOT_EVALUATEDを色だけでなく文字・iconでも区別する。

### 5.5 取込run・lineage `/ui/runs`

- run ID、trigger、開始/終了、status、requested/successful series、insert/update/revision/reject件数、error code
- runからsource file、相対path、SHA-256、source dataset、manifestへドリルダウン
- canonical barから最新runとderivation versionへ辿れること
- repository相対pathのみ表示し、host固有の絶対pathやsecretを出さない

### 5.6 backup・storage `/ui/operations`

- DB別の最新backup、SHA-256記録、`pg_restore --list`、restore smokeの状態
- retention方針、relation別size、row estimate
- 読み取り専用とし、backup作成・削除・restoreボタンは置かない

## 6. 指標定義

| 指標 | 定義 |
|---|---|
| 有効データセット数 | `catalog.source_dataset.active=true`の件数。dataset kind別内訳を併記 |
| 正式系列の銘柄数 | canonical 1H watermark/正式inventoryに存在するdistinct instrument |
| 系列数 | distinct `(instrument, role, layer, price_basis)`。inventory行数とは分離 |
| 収録期間 | `min_time_utc`〜`latest_complete_time_utc`。単なるmaxよりcompleteを優先 |
| 鮮度 | server UTCと`latest_complete_time_utc`の差。status判定はDB viewとcalendar規則を正とする |
| complete率 | `complete rows / actual rows`。分母0はNOT_EVALUATED |
| missing率 | `missing expected bars / expected bars`。calendar未検証はNOT_EVALUATED |
| 現在品質 | canonical coverage/freshness/current gateの状態 |
| 過去監査event | quality eventのOPEN/ACKNOWLEDGED/RESOLVED。現在品質と別集計 |
| 行数 | layer/relation単位。raw、curated、derived間で合算しない |

表示値にはdefinition tooltipとsource view/APIを付け、件数の意味を追跡可能にする。

## 7. Read API追加仕様

既存`/api/v1/operations/*`、`/api/v1/bars`、`/api/v1/manifests`、`/api/v1/layer-counts`は維持する。UI向けに次をallow-list方式で追加する。

| endpoint | 用途 | 主な入力 |
|---|---|---|
| `GET /api/v1/ui/overview` | カード、状態分布、最終run/backup | なし |
| `GET /api/v1/ui/series` | 在庫の検索・ページング | role/category/symbol/layer/status/cursor/limit |
| `GET /api/v1/ui/series/<series_id>` | 系列metadata・coverage・lineage | なし |
| `GET /api/v1/ui/chart-bars` | Lightweight Charts用OHLC/line | series_id/start/end/limit/eligibility |
| `GET /api/v1/ui/chart-marks` | 品質・run境界marker | series_id/start/end |
| `GET /api/v1/ui/quality-summary` | current/historicalを分離した品質要約 | category/layer/status |

共通契約:

- responseは`api_version`、`generated_at_utc`、`data`、`paging`、`warnings`を持つ。
- `series_id`はdataset/instrument/role/layer/price basisをサーバー側で解決する不透明な識別子とし、relation名やSQL断片を含めない。これによりinstrument IDを持たないtotal return系列も同じ契約で扱う。
- `eligibility`は`eligible`（既定、complete/PASS）または`stored_complete`（completeかつWARN/NOT_EVALUATEDを含む）のallow-listとする。後者のresponseは必ず`warnings`とrow単位の`quality_status`を返す。FAIL/incomplete/raw archiveはこのendpointの対象外とする。
- response metadataの`series_kind`は`ohlc`または`line`とする。OHLC rowは`time/open/high/low/close/volume/price_basis`、total return rowは`time/value/price_basis`を返し、存在しないOHLCを捏造しない。
- 時刻範囲は`start <= time < end`、結果は昇順。同一時刻・price basisの重複を許さない。
- 価格精度を落とさないようAPI内は文字列で保持可能とし、クライアント変換時に非有限値を拒否する。
- limit上限、固定ORDER BY、parameterized query、30秒statement timeoutを維持する。
- filter値はallow-listまたは最大長を検証し、relation/order SQL断片として連結しない。
- errorはsecretやSQL本文を含めず、安定した`error_code`を返す。

## 8. 状態表示ルール

優先順位は`FAIL > STALE > WARN > NOT_EVALUATED > PASS`とする。ただし異なる意味を単一色へ統合せず、内訳を必ず残す。

- `PASS`: 現在の規則で利用可能
- `WARN`: 利用は可能だがcoverage等に注意が必要
- `STALE`: 最新completeが許容更新間隔を超過
- `FAIL`: 品質または整合性規則違反
- `NOT_EVALUATED`: calendar/threshold不足等により判定不能
- `PROVISIONAL`: calendar未確定。NOT_EVALUATEDの理由として表示
- `RAW / ARCHIVE / NOT FOR STRATEGY`: 正式系列でないことを常時badge表示

## 9. セキュリティ・プライバシー

- `127.0.0.1`だけへbindし、外部networkへ公開しない。
- `saxo_app_reader`、read-only transaction、pool最大5、statement timeout 30秒を維持する。
- static assetを自己ホストし、CSPは`default-src 'self'`を基礎にscript/style/font/img/connectを最小許可する。
- token入力、token/account保存、cookie認証、LocalStorage/SessionStorageへの機密保存を行わない。
- ブラウザcacheはno-store、表示用短期cacheはmemoryのみとしreloadで破棄する。
- 外部CDN、telemetry、analytics、注文APIを使用しない。
- UI生成物やlogへ絶対path、credential、HTTP Authorization headerを出さない。

## 10. 性能・操作性

- overviewは集約endpoint 1回を中心にし、全inventoryを先に取得しない。
- tableはserver-side paging、50件/頁を既定、最大200件/頁とする。
- chart初期取得は1,000本以内、追加取得をcursor/rangeで行う。
- filter入力は250ms debounce、古いrequestはAbortControllerで中断する。
- 同一rangeはpage内memory cacheを許可するが、`generated_at_utc`を保持する。
- 初回overview p95 2秒以内、chart 1,000本 p95 1秒以内をローカル受入目標とする。
- statusを色だけで伝えない。keyboard操作、focus表示、表見出し、aria-labelを用意する。

## 11. 空・異常・境界状態

- データ0件: 正常なempty stateとし、適用filterと利用可能期間を案内する。
- API停止/DB停止: 最終値を正常値として残さず「接続不可」とretryを表示する。
- 10,000本超過: truncationを隠さず、追加読込または期間短縮を案内する。
- DST、短縮取引、holiday: UTC保存を正とし、JST表示変更によってbar境界を再集計しない。
- volume null: 0へ置換せずvolume paneを非表示にする。
- NaN/Infinity/OHLC invariant違反/非昇順: chartへ渡さず、UI data errorとして表示する。
- dataset未分類: `UNKNOWN ROLE`で隔離し、canonicalへ自動昇格しない。

## 12. 受入基準

### 機能

- overview、inventory、series detail、quality、runs、operationsの6画面が実装される。
- 1H/4H/1Dのローソク足とtotal returnの折れ線を表示できる。
- WARN/NOT_EVALUATED系列を管理確認モードで表示でき、非eligible警告を解除・非表示にできない。
- 銘柄、足、期間、price basisを変更して正しい範囲を再取得できる。
- raw/archiveとcanonical/derivedが視覚・定義・件数で明確に区別される。
- current qualityとhistorical audit eventが混同されない。

### データ整合性

- UIカード・表・chartの件数、最古/最新、最初/最後のOHLCがRead API/CLIと一致する。
- chart dataは昇順、重複0、complete/PASSのみ、OHLC invariant成立である。
- 1DはUTC 00:00、JST/UTC切替で元barの識別が変わらない。
- paginationした範囲の境界で欠落・重複がない。

### 安全性

- POST/PUT/PATCH/DELETEは405、任意SQLと任意relation指定は不可能である。
- Saxo token/account identifierを入力・保存・送信しない。
- ブラウザnetworkにloopback以外のrequestがない。
- DB role、read-only、timeout、pool上限がDB4 validatorで確認される。
- 注文、precheck、data mutationは0件である。

### QA

- API unit/integration、UI component、browser E2E、accessibility、security headerを自動試験する。
- ETF、FX、長期系列、短期系列、空期間、10,000本境界、calendar provisionalをfixtureに含める。
- TradingView帰属表示とライセンス同梱をrelease checkへ追加する。
- 既存DB1〜DB4 validatorと全pytestを回帰PASSさせる。

## 13. 実装フェーズ案

| Phase | 内容 | Gate | 状態 |
|---|---|---|---|
| DMUI0 | 本仕様の合意・凍結 | 画面、定義、境界、採用libraryが確定 | PASS |
| DMUI1 | UI用read model/APIと契約test | CLI/API照合、read-only/security PASS | PASS |
| DMUI2 | overview・inventory・quality・runs | 指標定義と表示の照合PASS | PASS |
| DMUI3 | Lightweight Chartsによる系列詳細 | OHLC/期間/paging/timezone照合PASS | PASS |
| DMUI4 | E2E、accessibility、運用手順、release | 全受入基準・DB4回帰PASS | PASS |

DMUIはデータ管理の並行workstreamであり、戦略研究のRT0を代替せず、PnL・signal・WFO・Holdoutを解放しない。

実装結果と再現手順は`docs/data_management_web_ui_implementation_result.md`、現行artifactのSHA-256継承は`manifests/data_management_web_ui_implementation_manifest.json`を正とする。

## 14. 今回の仕様判断

- 初期版は読み取り専用とする。
- 既存DB4 Read APIの同一origin `/ui/`へ配置する。
- TradingView Lightweight Chartsを採用し、Advanced Chartsは将来選択肢とする。
- 正式1H、派生4H、派生1D、total return、raw/archiveを別データ種として扱う。
- チャートは保存済み足だけを表示し、クライアント再集計を行わない。
- データ修復、再取得、Reconcile、backup/restore操作はこの画面へ混在させない。

## 15. 参照

- TradingView Lightweight Charts: <https://github.com/tradingview/lightweight-charts>
- Lightweight Charts time scale: <https://tradingview.github.io/lightweight-charts/docs/5.1/time-scale>
- Lightweight Charts series types: <https://tradingview.github.io/lightweight-charts/docs/series-types>
- TradingView Datafeed API: <https://www.tradingview.com/charting-library-docs/latest/connecting_data/Datafeed-API/>
- DB4実装結果: `docs/db4_implementation_result.md`
- DB運用runbook: `docs/database_operations_runbook.md`
- DB0仕様: `specs/v13_phase_db0_database_spec.json`
