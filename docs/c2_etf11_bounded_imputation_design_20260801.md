# C2 ETF11 短時間欠落の有界前方補完仕様・実装結果

## 結論

C2の低頻度紙上監視では、ETF11の60分足に限定的な欠落があってもサービス全体や他銘柄、日次close処理を停止しない。欠落はcanonicalデータを変更せず、C2専用overlayに`IMPUTED_PREVIOUS_VALID`として明示的に追加する。日次closeは必ずsession終端のSaxo accepted実値を使い、補完値をofficial close、total return、execution priceとして扱わない。

コード、migration 0036、Read API/UI契約、回帰テストを実装し、2026-08-01にユーザーの明示承認に基づいて実DBへ反映した。0036に加え、最新状態APIを低遅延化する0037と、append時の一意キーだけをingest roleへ参照許可する0038を適用した。ETF11のguarded revisionは全件APPLIED、TIP/GLDのoverlayは各2本である。

## 補完規則

対象はETF11、60分、`native_ohlc`、C2 paper monitoringだけである。

- 1 sessionあたり最大2本、最大2本連続まで。
- 補完OHLCは`O=H=L=C=直前の有効なactual close`とする。
- volumeは`null`とし、0または前値を作らない。
- recursiveな補完は禁止し、補完値を次の補完元にしない。
- 欠落runの直後に同一DataVersionのactual barが必要である。
- session始点欠落は、前のverified sessionのactual terminal barと当該session内の右actual anchorがそろう場合だけ候補にできる。
- 当日terminal barはactualかつcompletedが必須で、補完しない。したがって日次close自体が欠落する場合はfail-closedとなる。
- calendar未検証、DataVersion/lineage不一致、actual OHLC異常、左/右anchorなし、3本以上の欠落はinstrument/session単位でBLOCKEDにする。他銘柄とサービス全体は継続する。

補完rowは以下を監査可能にする。

- instrument、session_date、missing timestamp
- `source_kind=IMPUTED_PREVIOUS_VALID`
- reason
- 元actual timestamp
- 連続欠落のindex/count
- source/candidate DataVersion
- source ingestion run、artifact path、payload SHA-256
- policy IDとreview ID
- `official_close_claim=false`
- `total_return_claim=false`
- `execution_price_claim=false`

## raw・canonicalとの境界

`raw.market_bar_revision`、`curated.market_bar`、`ops.watermark`、既存`derived.market_bar_4h`、`derived.market_bar_1d_risk`は補完で上書きしない。migration 0036はappend-onlyの`derived.c2_market_bar_1h_imputation`を追加し、actualと補完を区別するread-only viewだけを公開する。後日actual barが到着した場合、overlay viewはactualを優先するが、過去の補完evidenceは削除しない。

通常incrementalはmigration適用後、取得instrumentの直近3 completed sessionsだけを検査する。適格planだけをappendし、blocked planは他instrumentのtransactionやschedulerを停止させない。migration未適用時は`NOT_APPLIED_SCHEMA`のno-opであり、既存更新経路は変更しない。

## TIP/GLD候補

既存のimmutable revision evidenceと限定read-only Saxo再確認では、TIPとGLDはいずれも2026-07-29 13:30Z・14:30Zだけが欠落し、15:30Z以後とsession終端actual barが存在する。2本は同一sessionの連続した始点欠落であるため、前session terminal actualのDataVersion/lineageが一致すれば本規則の候補となる。

候補review後にユーザーの明示承認を受け、TIP/GLDのprovider actual行をguarded revision経路でcanonicalへapplyし、同じtransaction内で各2本をoverlayへappendした。補完値はcanonical/rawへ書かれていない。

read-only候補reviewは両銘柄とも`PASS_WITH_IMPUTATION_WARNING`となった。各2本の元actualは2026-07-28 19:30Zで、missing sessionの15:30Z以後のactual、actual terminal、同一DataVersion、verified calendarにより前後を限定できた。機械可読証跡は`manifests/c2_etf11_bounded_imputation_candidate_review_20260801.json`である。価格値は証跡へ出力していない。

## Read APIとUI

- `GET /api/v1/c2/daily-close-status`
  - actual daily close、freshness、`imputed_bar_count`、`imputation_status`、`warning_ids`を返す。
  - 補完を含む正常sessionは`AVAILABLE_WITH_IMPUTATION_WARNING`で、service停止とは扱わない。
- `GET /api/v1/c2/hourly-overlay`
  - actualと補完を`source_kind`で区別し、補完の元timestamp、理由、連続欠落数、DataVersion、lineage hashを返す。
  - generic `/api/v1/bars`へ補完値を混在させない。
- Data Consoleの`C2日次終値`
  - 補完銘柄数、銘柄別補完本数、警告ID、補完状態を表示する。

StrategyはC2専用endpointだけで補完警告を消費し、`WARN`をactual provider observationと誤認しない。official close、total-return、execution-price contractにはこのoverlayを使用しない。

## 実運用反映結果

- migration 0036/0037/0038はchecksum検証済みでproduction DBへ適用済み。
- SPY/IWM/EFA/EEM/VNQ/SHY/IEF/TLT/LQD/TIP/GLDは、各revision eventとold/new DataVersionを固定したguarded applyがAPPLIED。
- 全11銘柄のlatest sessionは2026-07-31、freshnessはPASS、欠損銘柄は0。
- TIP/GLDの2026-07-29 13:30Z・14:30Zは各2本だけが`IMPUTED_PREVIOUS_VALID`。元actual timestampは2026-07-28 19:30Zで、session終端はactualのまま。
- Read APIの全体状態は`AVAILABLE_WITH_IMPUTATION_WARNING`。TIP/GLDはWARN、他9銘柄はPASS。
- schedulerは`all_except_usdjpy_with_fx_research_candidates_20260727`でRUNNING/AUTH_READY。USDJPYは引き続き除外。
- 機械可読な実行証跡は`manifests/c2_etf11_bounded_imputation_live_apply_20260801.json`。

rollbackは補完rowの手動DELETEでは行わない。actual rowが後から公開された場合はviewが自動的にactualを優先する。契約不備が判明した場合はRead APIで該当policy/review IDを非選択にする新しいappend-only migrationを作り、監査証跡を保持する。

## 安全境界

- DB migration: 0036/0037/0038適用済み
- guarded revision apply: ETF11の11件をAPPLIED
- overlay append: TIP 2本、GLD 2本
- raw/source evidence: append-only、既存行の上書き・削除0
- Saxo GET: 140（失敗時も含む全監査試行、write requestは0）
- Saxo write、注文、precheck、cancel、口座・資金操作: 0
- USDJPY取得・quarantine変更: 0
