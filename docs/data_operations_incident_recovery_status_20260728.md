# データ運用障害・復旧状況 2026-07-28

## 1. 文書の目的と責務境界

本書は`saxo_db`の取得、immutable raw、curated、watermark、派生足、品質、
Read API、schedulerの運用状況を記録する。トレード戦略の採否、signal、
PnL、position、precheck、order、account操作とは無関係であり、本対応では
Saxo write request、precheck、order、account操作を実行していない。

状態分類は次を混同しない。

- provider raw値の不整合: data/content-quality
- provider履歴境界へ到達しない: coverage
- DB権限不足、OAuth設定不足、API停止: interface/operational
- DataVersion変更のみ: revision warning / review pending

## 2. 項目別の現在状態

| 項目 | 現在状態 | 影響範囲 | 現在の利用可否 | 次アクション | 承認・ユーザー操作 |
|---|---|---|---|---|---|
| (a) DataVersion警告分離 | 完了 | 今後検知する各instrumentだけ | last acceptedを警告付き利用可能 | warningをreviewし、必要時だけapply承認 | curated変更時だけ別承認 |
| (a) SPY/SHY/GLD限定reconcile | 完了済み | 各対象instrumentだけ | `AVAILABLE` | 履歴は保持し通常監視 | なし |
| (b) 新仕様の本稼働 | 完了 | scheduler、Read API、Operator UI、migration 0029 | 稼働中 | 通常slotと将来warningを監視 | なし |
| (c) IEF/TLT権限障害 | 復旧済み・通常更新PASS確認済み | IEF、TLTだけ | `AVAILABLE`、quality/freshness PASS | 次回以降のXNYS slotを通常監視 | なし |
| (d) USDJPY provider品質 | quarantine継続 | USDJPYだけ | current利用不可 | 新DVの全履歴品質をguard付き検証するか判断 | full-refetchは別承認が必要 |
| (e) AUDUSD | 全履歴onboard・2回通常更新・定期運用開始 | AUDUSDだけ | `PUBLISHED / AVAILABLE_WITH_WARNINGS` | 独立slotを通常監視 | 14件の限定例外は承認済み |
| (e) USDCAD | 全履歴onboard・2回通常更新・定期運用開始 | USDCADだけ | `PUBLISHED / AVAILABLE_WITH_WARNINGS` | 独立slotを通常監視 | 2010年effective start承認済み |
| (e) USDCHF | 全履歴onboard・2回通常更新・定期運用開始 | USDCHFだけ | `PUBLISHED / AVAILABLE_WITH_WARNINGS` | 独立slotを通常監視 | 2010年effective start承認済み |

## 3. (a) 完了済みの警告分離と限定reconcile

今後のDataVersion変更は`data_version_revision_warning_v2`として、old/new
version、検知時刻、限定sample差分を`REVIEW_PENDING`で記録する。検知だけで
curated、watermark、4H/1Dを変更せず、自動bounded reconcile、自動
full-refetch、instrument/category/service停止を行わない。Read APIは
`AVAILABLE_WITH_REVISION_WARNING`、last accepted、provider evidence、review
pendingを分けて返す。

SPY、SHY、GLDの既存reconcileは旧policyで既に`APPLIED`であり、履歴を変更・
削除していない。今後のwarning policyへ遡及変換しない。

## 4. (b) 新仕様の本稼働適用

- migration: `0029_data_version_warning_review_policy.sql`
- checksum: `319e10d23a901e629972de428f0090c9b677be735a401cc97da199edcdbab2ae`
- checksum validation: PASS
- 適用前後の署名照合:
  - `curated.market_bar`: unchanged
  - `raw.market_bar_revision`: unchanged
  - `derived.market_bar_4h`: unchanged
  - `derived.market_bar_1d_risk`: unchanged
  - `ops.watermark`: unchanged
- Read API: health PASS、`saxo_app_reader`、transaction read-only、30秒timeout
- scheduler scope: 追加FX有効化前は`all_except_usdjpy_provider_quarantine_20260727`、
  2026-07-28T04:08:03Z以後は
  `all_except_usdjpy_with_fx_research_candidates_20260727`
- scheduler auth: `AUTH_READY`
- Operator UI: 新コードで再起動済み。旧reconcile endpointはHTTP 409
  `REVISION_REVIEW_REQUIRED_USE_EXPLICIT_APPLY`
- warning dry-run: `AVAILABLE_WITH_REVISION_WARNING`、DB mutation 0
- 再起動後のEURUSD通常slot: run 884、deadline内PASS、watermark
  `2026-07-27T23:00:00Z`

## 5. (c) IEF/TLT interface/operational権限エラー

### 原因

旧bounded retry実装は既存`ops.data_version_revision_step`をDELETEしてから
再作成しようとしたが、監査証跡を保護する`saxo_ingest`にはDELETE権限を
付与していなかった。PostgreSQL logはrun 860/862について
`permission denied for table data_version_revision_step`を記録した。

権限を広げず、現行コードをstep追記方式とし、DELETEを行わない回帰テストを
追加した。対象event 10/11には永続stepが0件であることを確認してから、失敗時に
保持したraw artifactのsize/SHA-256を再検証し、1銘柄ずつretryした。

### 結果

| Instrument | Retry run | DataVersion | 限定範囲 UTC | Insert/Update/Remove | Read API |
|---|---:|---:|---|---:|---|
| IEF | 885 | 29753231 | 2026-07-24 19:30–2026-07-27 19:30 | 7 / 1 / 0 | AVAILABLE、quality PASS、freshness PASS |
| TLT | 886 | 29752511 | 2026-07-24 19:30–2026-07-27 19:30 | 7 / 1 / 0 | AVAILABLE、quality PASS、freshness PASS |

各runで、対象外instrumentのcurated、raw、4H、1D、watermarkへの書込みは
すべて0だった。scheduler再起動後、IEF/TLTのterminal blockerは0となり、
serviceは`RUNNING`へ戻った。再起動直後の通常更新もIEF run 887、TLT run 888で
ともにPASSし、最新完成足`2026-07-27T18:30:00Z`、quality/freshness PASSを確認した。
保守停止中に期限を過ぎたslotのcatch-upであるため、この2 runのSLA時刻判定は
MISSのまま保持し、品質PASSと混同しない。以後は通常のXNYS scheduleで監視する。

## 6. (d) USDJPY provider raw-quality quarantine

既知異常DataVersion `29738069`には、High/Lowの`Bid > Ask`が245 unique row
存在する。補間、Bid/Ask入替、clamp、旧新version混在、gate緩和は行わず、
USDJPYだけをactive schedulerから除外している。

2026-07-28T00:08:46Zに専用`Count=1` watchを1回実行し、新しいprovider
DataVersion `29749254`を検出した。単一responseは次へ隔離保存した。

- artifact: `data/acquisition/runs/20260728T000846Z-e815c9e4/instruments/usdjpy/data_version_probe.json`
- SHA-256: `2b459a1ac427779770978f7ae98e91b8f1cadad2b0d4542aebf17d764a39b9b4`
- provider GET: 1
- DB mutation / Saxo write / precheck / order: `0 / 0 / 0 / 0`

新DataVersion検出は、過去245件が訂正された証明ではない。USDJPYの
single-instrument guarded full-refetchは「実行可否を別途判断できる」段階で、
未承認・未実行である。外部Saxo supportへの送信も行っていない。

## 7. (e) AUDUSD/USDCAD/USDCHF onboarding

初回fail-closed結果を踏まえ、ユーザー承認済みの研究用警告契約をmigration
`0030`で追加した。認証値を表示・file保存せず、指定順で全履歴onboardingを
再実行した。

| Instrument | Run | DataVersion | Effective start | 最新完成1H | 判定 |
|---|---:|---:|---|---|---|
| AUDUSD | 893（freshness 895） | 29749260 | 2003-05-12 00:00 UTC | 2026-07-28 03:00 UTC | `PUBLISHED / AVAILABLE_WITH_WARNINGS` |
| USDCAD | 896 | 29749380 | 2010-06-18 00:00 UTC | 2026-07-28 03:00 UTC | `PUBLISHED / AVAILABLE_WITH_WARNINGS` |
| USDCHF | 897 | 29749380 | 2010-06-18 00:00 UTC | 2026-07-28 03:00 UTC | `PUBLISHED / AVAILABLE_WITH_WARNINGS` |

AUDUSDの14件は件数、期間、High/Low field、content fingerprintが承認baselineと
完全一致した場合だけ例外とする。rawを保持し、curatedから無補間で除外した。
USDCAD/USDCHFはprovider表示開始2002年とeffective start 2010年を分け、空白を
補間・推定しない。3ペアともquality WARN、coverage WARN、freshness PASS、
current/unknown blocker 0である。異なる完成1H（02:00 UTC、03:00 UTC）の通常更新を
2回確認した。normal pass runはAUDUSD `904 / 913`、USDCAD `905 / 914`、USDCHF
`906 / 915`である。候補専用schedulerを起動し、既存EURUSD+ETF11のschedule条件と
USDJPY quarantineは変更していない。

## 8. (f) 次アクションと承認点

1. IEF/TLT: 復旧後最初の通常更新はPASS済み。以後のXNYS slotを通常監視する。
   現時点の利用は可能。
2. USDJPY: new DataVersion `29749254`について、USDJPYだけのguarded full-refetchを
   実行するかoperatorが別途承認する。未承認のまま自動実行しない。
3. AUDUSD/USDCAD/USDCHF: 異なる完成1Hで通常更新2回を確認済み。候補専用slotを
   instrument単位で通常監視する。
4. 追加FX candidate scheduler: `PUBLISHED / 2`、承認policy一致、freshness PASS、
   blocker 0を確認し、2026-07-28T04:08:03Zに起動済み。最初の独立slotは
   AUDUSD run 916、USDCAD run 917、USDCHF run 918ですべてSLA PASS。
5. 将来のprovider anomaly増加、範囲拡大、Open/Close異常、新規品質規則違反は
   一般PASSへ緩めず、該当instrumentだけを再reviewする。

将来、独立CLIからOAuth操作する場合は、AppKey値をchatへ貼らず、ユーザー自身の
terminal sessionへ`SAXO_OAUTH_APP_KEY`を設定する。現在の無人schedulerとOperator
UIは`AUTH_READY`であり、今回の運用に追加loginは不要である。
