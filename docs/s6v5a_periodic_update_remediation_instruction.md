# S6V5A向け定期更新 残課題修正指示書

- 作成日: 2026-07-25 JST
- 対象repository: `saxo_db`
- 依頼元consumer: `saxo_trading_strategy_analysis`
- 現在判定: `DPU2_PARTIAL_RUNTIME_PASS_WITH_REMEDIATION_REQUIRED`
- 戦略側判定: `S6V5A PENDING_0_OF_3`

## 1. 目的

S6V5Aで必要な市場データを、定期取得、raw保存、curated更新、coverage／freshness／quality評価、manifest、Read API公開まで一貫して運用可能にする。

この指示書はデータ管理機能の修正だけを対象とする。戦略、signal、WFO、Holdout、PnL、position、precheck、orderは実装しない。

## 2. 確認済みの正常部分

2026-07-24 UTCの実run 161は次を満たした。

- status: `PASS`
- 対象: SPY、IWM、EFA、EEM、VNQ、EURUSDの6系列
- acquisition request: 19 GET
- successful series: 6
- watermark gate: `PASS`
- database commit: `watermark_and_derived_committed`
- latest complete: ETF 5系列 `2026-07-24T18:30:00Z`、EURUSD `2026-07-24T19:00:00Z`
- order、precheck、Saxo write request: 0
- raw artifact、SHA-256、run manifestあり

したがって、以前の「1Hが2026-07-16で停止」は解消している。この正常部分を壊さず、以下の残課題だけを修正すること。

## 3. 必須修正

### R1. Service process identityをlocale非依存にする

現在、processはPID 44856で動作し、PID、cwd、command hashは一致しているが、service managerは`BLOCKED_STALE_PID`を返す。

原因:

- `service.json`: `start_fingerprint="土 7/25 07:34:34 2026"`
- 現在のprocess probe: `start_fingerprint="Sat Jul 25 07:34:34 2026"`

修正要件:

1. `market_db.periodic_update_service`と共通process probeで、開始時刻fingerprintをlocale非依存にする。
2. 推奨は`ps`実行環境を`LC_ALL=C`へ固定し、保存時と照合時に同一形式を使用すること。
3. 可能なら表示文字列ではなく、kernel由来の安定した開始時刻値へ正規化すること。
4. 既存stateを移行する場合、PID、cwd、command、command SHA-256、開始時刻の意味的一致を全て確認すること。
5. PIDが存在するだけでstateを自動採用しないこと。
6. identity不明なprocessをkill、stop、state削除しないこと。

受入条件:

- 稼働中の正規processに対して`periodic_update_service status`が`PASS`かつ`managed=true`
- 日本語localeで開始し英語localeで照会してもPASS
- 英語localeで開始し日本語localeで照会してもPASS
- PID再利用、cwd違い、command違い、開始時刻違いは`BLOCKED_STALE_PID`
- `start`、`status`、`stop`のidentity判定が同一規則

### R2. Implementation manifestを現在コードへ同期する

現在、関連testは25件中24件PASS、1件FAILである。FAILは実装manifestと現在artifactの不一致である。

不一致:

- `market_db/saxo_auth.py`: size／SHA-256不一致
- `tests/test_saxo_auth.py`: size／SHA-256不一致

修正要件:

1. R1、R3、R4のコード修正が完了するまでmanifestを更新しない。
2. 最終コードfreeze後、全artifactのsizeとSHA-256を機械生成する。
3. test件数、runtime acceptance、total-return状態を実測値へ更新する。
4. 未実証項目を`PASS`へ変更しない。
5. manifestを手作業で部分修正せず、既存生成・検証contractを使用する。

受入条件:

- `test_periodic_implementation_manifest_attests_current_artifacts` PASS
- `manifest_artifact_state(...).mismatches == []`
- Git管理対象、追加artifact、spec、testが全てmanifestと一致

### R3. ETF 1H freshnessを完成可能bar規則と一致させる

現在、schedulerは市場終了までに完全に閉じるregular 1Hだけを対象としており、2026-07-24の最終完成可能barは`18:30Z`である。一方、Read APIのfreshnessは`19:30Z`を次の期待時刻として`STALE`を返している。

修正要件:

1. `catalog.session_interval`を正本として、market closeまでに完全に閉じる1H slotだけをexpected completeへ含める。
2. 16:00 ET close日に15:30 ET開始の不完全1Hを必須完成barとして扱わない。
3. early close、holiday、DSTも同じcalendar contractで処理する。
4. S6V5Aの第1barは09:30–10:30 ETであり、10:33 ETまでにfreshness判定へ反映する。
5. coverageの既知WARNとfreshness FAILを混同しない。

受入条件:

- 通常取引日の引け後、最終完成可能1Hまで存在すればETF 5系列の`freshness_status=PASS`
- 10:30 ET第1bar確定後、10:33 ETまでに対象5系列がreadiness利用可能
- 不完全bar、未来bar、calendar外barをPASS根拠にしない

### R4. EURUSD coverage／freshnessを根拠付きで評価する

現在、EURUSDは値が`2026-07-24T19:00:00Z`まで更新されqualityはPASSだが、coverageとfreshnessが`NOT_EVALUATED`である。

修正要件:

1. Saxo SIM FXの取引session、週末close／open、DST、maintenance windowを仕様としてfreezeする。
2. expected 1H slot、完成判定、freshness graceをcatalogへ登録する。
3. 根拠なしに`NOT_EVALUATED`をPASSへ書き換えない。
4. schedulerの毎UTC時03分取得、10分deadlineと同じexpected watermark contractを使う。
5. maintenance、market close、未確定barは`DATA_NOT_READY`またはoperational状態としてdata qualityから分離する。

受入条件:

- 通常FX sessionでcoverageが`PASS`または説明可能な非blocking `WARN`
- freshnessが`PASS`
- null、非正値、Bid/Ask／OHLC違反が0
- Read API `series-status`とscheduler watermarkの判定が一致

### R5. Current total-return定期取得 DPU3を完成させる

現在のhard blockerは次である。

```text
BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED
```

既存dataset `20260712T135236Z`は`development_cutoff_only`であり、current運用へ昇格してはならない。

修正要件:

1. provider、利用条件、adjusted close、cash dividend、split、corporate action、訂正方針、availability SLAを明文化してfreezeする。
2. current用raw artifactをimmutable保存し、取得時刻、query scope、response hash、provider revisionを記録する。
3. 既存snapshotとの重複期間でprice／return／corporate action parityを検証する。
4. current用の新しい安定dataset IDを発行する。
5. SPY、IWM、EFA、EEM、VNQを前営業日まで更新する。
6. duplicate date、null、非正値、日付逆転、corporate-action不整合を遮断する。
7. coverage、freshness、quality、lineage、research eligibilityをmanifestへ公開する。
8. `GET /api/v1/total-return`から新dataset IDを指定して取得可能にする。
9. T+0 EOD取得とT+1朝retryをscheduleする。
10. provider未確定ならコードだけで推測選定せず、必要なoperator decisionを具体的に報告してblockを維持する。

受入条件:

- `development_cutoff_only`を変更せず保持
- current dataset IDが別に存在
- 5系列すべて前営業日までcoverageあり
- source parity PASS
- `research_eligibility`がcurrent readiness利用可能であることをmanifestが明示
- ordered content SHA-256、lineage、state revisionがRead APIで取得可能

## 4. Runtime acceptance

R1からR5の修正後、次を実施する。

1. manager経由でservice statusを確認する。
2. identity確認後にのみ安全なstop／startを実施する。
3. OAuth access refreshとrefresh-token rotationを期限跨ぎで実証する。
4. 3 XNYS取引日連続で第1regular 1Hを10:33 ETまでに公開する。
5. EURUSD hourly slotをdeadline内に更新する。
6. total-returnを前営業日まで更新する。
7. 各runのraw、manifest、DB run、watermark、Read APIを照合する。

catch-up runの`PASS`とSLA `MISS`を、期限内運転PASSとして数えないこと。

## 5. Test要求

最低限、次を実行する。

```bash
.venv/bin/python -m pytest -q \
  tests/test_periodic_update.py \
  tests/test_periodic_update_service.py \
  tests/test_saxo_auth.py \
  tests/test_operator_ui.py

.venv/bin/python -m pytest

SAXO_DB_INTEGRATION=1 .venv/bin/python -m pytest

.venv/bin/python -m market_db.validate --phase db4
```

追加必須test:

- process start fingerprintの日本語／英語locale差
- PID再利用、cwd違い、command違い
- 通常日／短縮日／DSTの最終完成可能ETF 1H
- EURUSD weekend／maintenance／DST
- total-return duplicate／revision／corporate action／provider error
- OAuth expiry前refresh、401後1回だけrefresh、refresh token rotation
- scheduler restart／catch-up／deadline MISS

## 6. Strategy側の最終確認

データ管理側の修正・runtime acceptance完了後、strategy側で次を実行する。

```bash
cd /Users/tikeda/workspace/trade/saxo_trading_strategy_analysis

.venv/bin/python scripts/validate_equity_reit_s6_sim_validation.py \
  --phase S6V5A \
  --integration
```

期待値:

- `current_data_status=PASS_DATA_GATE`
- Read API interface PASS
- valid readiness sessionは開始前なら`0/3`のままでよい
- order submit 0

その後、別取引日で`SHADOW_ONLY 1 session + PRECHECK_ONLY 2 sessions`を実施する。S6V5Bの注文smokeはユーザーの別途明示承認なしに開始しない。

## 7. 禁止事項

- 稼働中processのidentity不明状態でkill、state削除、強制restartしない
- `docker compose down -v`、DB drop、volume削除をしない
- raw、run manifest、quality event、watermark履歴を削除しない
- OAuth token、refresh token、AppKey、口座識別子をlog、DB、manifest、Gitへ保存しない
- 24時間tokenをscheduler用に永続化しない
- 既存`development_cutoff_only` datasetをcurrentへ改称しない
- `NOT_EVALUATED`、`STALE`、interface障害を根拠なくPASSへ変更しない
- strategy、signal、WFO、Holdout、PnL、position、precheck、orderを`saxo_db`へ実装しない
- unrelatedな既存uncommitted changeを削除・上書きしない

## 8. 完了報告に含める内容

- 変更ファイル一覧
- R1からR5の各判定
- current dataset IDとprovider contract
- scheduler／OAuth／serviceのruntime evidence
- 3取引日のslot、due、finish、deadline、SLA
- 6系列の最新complete時刻
- coverage／freshness／quality／current blocker
- manifest mismatch 0の証跡
- unit／integration／validator結果
- Saxo GET／write／precheck／order件数
- rollback手順
- strategy側S6V5A再判定結果
