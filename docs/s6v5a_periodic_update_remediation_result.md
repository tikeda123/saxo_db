# S6V5A向け定期更新 残課題修正結果

- 実施日: 2026-07-25 JST
- 対象repository: `saxo_db`
- 判定: `REMEDIATION_IMPLEMENTED_PENDING_SLA_AND_PROVIDER`
- strategy実行・precheck・order: 実施していない

## 1. 結論

R1、R2、R3、R4のコード・DB・Read API修正を実装した。R5はproviderを推測採用せず、provider-neutralな検査contractとprovider障害分類まで実装し、`BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED`を維持した。したがって、1Hデータ経路は運用可能だが、current total-returnと3 XNYS取引日SLAは未完了である。

## 2. R1〜R5判定

| ID | 判定 | 証跡 |
|---|---|---|
| R1 | PASS | `LC_ALL=C`固定、開始時刻のcanonical化、日本語／英語fingerprint意味比較、PID・cwd・command SHA-256・module・port全一致時だけ旧state移行。manager statusは`PASS / managed=true`。 |
| R2 | PASS | 最終artifactをimplementation manifestへ機械同期し、artifact mismatch 0を検証する。 |
| R3 | PASS | XNYS session内で完全に閉じる1Hだけを期待する。通常日は6本、最終bar startは14:30 ET。ETF 5系列のfreshnessはPASS。 |
| R4 | PASS_WITH_NONBLOCKING_COVERAGE_WARN | Saxo公表条件に基づく`SBFX_24X5`をVERIFIED化。weekend、DST、16:59〜17:04 New York maintenanceを除外。EURUSD freshness PASS、coverage WARN、quality PASS。 |
| R5 | BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED | provider、利用・再配布条件、adjusted close、dividend、split、訂正、SLA、revision identityのoperator承認待ち。development snapshotは昇格していない。 |

## 3. 実装内容

### Service／OAuth

- process probeとchild processのlocaleをCへ固定し、開始時刻を`YYYY-MM-DDTHH:MM:SS`へ正規化した。
- identity不一致のPIDへsignalを送らない既存fail-closed規則を維持した。
- OAuth token endpointの成功2xx（Saxo SIMの201を含む）を受理しつつ、JSON、access token、refresh token、両expiryの必須検査を維持した。
- access tokenはprocess memoryだけ、refresh credentialはmacOS Keychainだけに保持する。
- 2026-07-24 23:33 UTCの再起動時、期限切れaccess leaseからrefreshが成功し、refresh credential expiryが更新された。token値は取得・出力していない。

### Calendar／coverage／freshness

- migration `0020`で完全slot freshnessを導入した。
- migration `0021`でFX canonical session境界を維持したままmaintenance隣接slotを除外した。
- migration `0022`／`0023`で全履歴`generate_series`とbarごとのrange joinを算術集計・session-date equality joinへ置換した。
- coverage照会は30秒timeoutから約2秒へ改善した。
- 定期更新とoperator／integration再構築が競合しないよう、派生bar再構築へtransaction advisory lockを追加した。

### Total-return fail-closed contract

- 対象tickerをSPY、IWM、EFA、EEM、VNQへ固定した。
- duplicate date、日付逆転、null／非正値、負dividend、非正split、provider revision欠落を遮断する。
- ordered content SHA-256とrevision keyを決定的に生成する。
- providerの401／403は`interface_auth`、その他のprovider transport障害は`interface_operational`とし、data-quality FAILへ誤分類しない。
- provider contract未確定中は取得、schedule、current dataset公開を行わない。

## 4. Runtime evidence

### Service／OAuth／scheduler

| 項目 | 結果 |
|---|---|
| service manager | PASS、managed=true |
| scheduler | RUNNING |
| OAuth | AUTH_READY、access token in memory、refresh credential present |
| token values exposed | false |
| Saxo write | 0 |
| precheck／order | 0 |
| 3 XNYS session SLA | PENDING_0_OF_3 |

catch-up run 169はEURUSD watermark gate PASS、Saxo GET 4、write 0、order 0である。ただしdeadline後のcatch-upなのでSLAはMISSであり、3取引日実績へ数えない。修正指示書記載のrun 161は6系列、GET 19、watermark gate PASSである。

### 6系列のRead API状態

確認時点: 2026-07-24 23:32 UTC

| series | latest complete UTC | latest expected UTC | coverage | freshness | quality | blocker |
|---|---|---|---|---|---|---|
| SPY 1H | 2026-07-24T18:30:00Z | 2026-07-24T18:30:00Z | WARN | PASS | PASS | none |
| IWM 1H | 2026-07-24T18:30:00Z | 2026-07-24T18:30:00Z | WARN | PASS | PASS | none |
| EFA 1H | 2026-07-24T18:30:00Z | 2026-07-24T18:30:00Z | WARN | PASS | PASS | none |
| EEM 1H | 2026-07-24T18:30:00Z | 2026-07-24T18:30:00Z | WARN | PASS | PASS | none |
| VNQ 1H | 2026-07-24T18:30:00Z | 2026-07-24T18:30:00Z | WARN | PASS | PASS | none |
| EURUSD 1H | 2026-07-24T19:00:00Z | 2026-07-24T19:00:00Z | WARN | PASS | PASS | none |

全6系列で`current_blockers=[]`、`unknown_blocker_count=0`、calendarはVERIFIEDである。coverage WARNは履歴上の欠損・時間外行を表示する既知の非blocking状態で、freshnessまたはqualityのFAILではない。EURUSDはexpected 92,422、calendar-aligned 92,017、missing 405、out-of-session 10,722である。

`GET /health`はHTTP 200／PASS、database `saxo_market`、role `saxo_app_reader`、`transaction_read_only=on`である。`GET /api/v1/bars`ではSPYの最新完成bar `18:30Z`とEURUSDの最新完成bar `19:00Z`を取得した。

## 5. Current total-return

- current dataset ID: 未発行（`null`）
- provider contract: 未確定
- current schedule: 無効
- 既存dataset ID: `20260712T135236Z`
- 既存eligibility: `development_cutoff_only`
- development dataset promoted: false

operator decisionには、provider名、ライセンス／再配布条件、adjusted-close・cash-dividend・split定義、corporate-action訂正方針、availability SLA、provider revision identityが必要である。承認後にだけcurrent raw取得、overlap parity、新dataset ID、T+0／T+1 scheduleを実装する。

## 6. Test／validator

- targeted unit: 34 PASS
- locale、PID再利用、cwd／command／start mismatch: PASS
- normal／short session／DST、FX weekend／maintenance: PASS
- OAuth 201、expiry前refresh、401後1回refresh、rotation: PASS
- total-return duplicate／revision／corporate action／provider error: PASS
- scheduler restart／latest catch-up／deadline MISS: PASS
- DB calendar status test（read-only role、6系列）: PASS
- 派生bar再構築競合修正後のidempotence test: PASS
- full pytest: 150 PASS、39 skip
- full DB integration: 189 PASS（0:09:26）
- DB4 validator: PASS

## 7. Rollback

1. `.venv/bin/python -m market_db.periodic_update_service stop`でidentity確認済みserviceだけを停止する。
2. 必要なら`.venv/bin/python -m market_db.saxo_auth logout --callback-port 8765`でこのアプリのKeychain credentialだけを削除する。
3. 適用済みmigration `0020`〜`0023`は編集・削除しない。旧viewへ戻す場合は新しいforward migrationを作る。
4. raw、run manifest、quality event、watermark、curated、research snapshot、DB volumeを削除しない。
5. コードrollback後、migration checksum、calendar、coverage、freshness、Read APIを再検証する。

## 8. 未完了gate

1. provider contractをoperatorが決定し、current total-return datasetを発行する。
2. 3 XNYS取引日で第1regular 1Hを10:33 ETまでに公開し、`PENDING_0_OF_3`を完了する。
3. 上記完了後、strategy repositoryでS6V5A integration validatorを実行する。

このrepositoryではstrategy、signal、WFO、Holdout、PnL、position、precheck、orderを実行しない。
