# S6V5A向け市場データ定期更新 実装計画

更新日: 2026-07-25
対象repository: `saxo_db`
状態: DPU2 remediation実装済み・3取引日SLA実証待ち、DPU3 source provider operator decision待ち

## 1. 目的と責任境界

`saxo_trading_strategy_analysis`のS6V5A readinessで必要な市場データを、`saxo_db`で定期取得し、raw、curated、coverage、freshness、quality、manifest、Read APIまで一貫して管理する。

対象内:

- SPY、IWM、EFA、EEM、VNQのSaxo SIM 1H `native_ohlc`
- EURUSDのSaxo SIM 1H `bid_ask_mid`
- 5 ETFのcurrent total-return日足。ただしprovider contract確定後に実装する
- OAuth、scheduler、retry、運用state、sanitized log

対象外:

- strategy、signal、WFO、Holdout、PnL、position、order、precheck
- LIVE環境、Saxo write endpoint
- 既存`development_cutoff_only` datasetの意味変更

## 2. 状態分類

| 分類 | 例 | data qualityとして記録するか |
|---|---|---|
| interface/auth | token失効、refresh失敗、permission | しない |
| interface/operational | timeout、503、429 | しない |
| data not ready | barがまだ形成中、期待watermark未到達 | しない |
| source revision | Saxo `DataVersion`変更 | controlled refetchを行う |
| data quality | null、非正値、OHLC不整合、bid/ask交差 | curated昇格を止める |
| source scope | `development_cutoff_only`、provider未確定 | currentへ公開しない |

## 3. 認証設計 DPU1

### 3.1 実装

- `market_db.saxo_auth`
  - SIM Authorization Code Grant with PKCE
  - authorization URL、state、verifier、S256 challenge生成
  - localhost callbackとstate検証
  - authorization codeとrefresh tokenの交換
  - refresh token rotation
  - `status/login/refresh/logout` CLI
- `MacOSKeychainStore`
  - refresh token、PKCE verifier、期限、AppKey fingerprintだけを保存
  - `security`のpassword値をprocess argumentへ入れずstdinで渡す
  - access tokenを保存しない
- operator UI
  - OAuth開始、callback、認証状態をlocalhostだけで処理
  - token値をHTML、JSON、log、browser storageへ返さない

### 3.2 人間操作

初回、credential失効、Saxo側revocation、Mac停止がrefresh期限を超えた場合だけブラウザloginが必要となる。通常の定期runごとに人間は操作しない。

## 4. 1H定期更新 DPU2

### 4.1 優先profile

`S6V5A_PRIORITY_INSTRUMENT_KEYS`を次の固定順で定義する。

```text
spy, iwm, efa, eem, vnq, eurusd
```

未知key、重複key、symbol自動代替を拒否する。既存13系列runは維持し、定期SLA用runだけを6系列へ限定する。

### 4.2 実行頻度

| job | 起動 | deadline | expected watermark |
|---|---|---|---|
| ETF regular 1H | XNYS bar終了15秒後 | bar終了3分後 | 各bar start以上 |
| 第1regular 1H | 10:30:15 ET | 10:33:00 ET | 当日09:30 ET以上 |
| EURUSD | 毎UTC時03分 | 毎UTC時10分 | 直前に完成が確定した1H以上 |

XNYS営業日、holiday、short session、DSTは`catalog.session_interval`を正本とし、曜日や固定UTC時刻で決めない。regular session内で1時間が完全に閉じるbarだけをscheduleする。

ETFのexpected completeは`bar start + 60分 <= session close`を必須とする。通常16:00 ET closeでは14:30 ET開始barが最終であり、15:30 ET開始の不完全barをfreshness根拠にしない。Read APIとschedulerはmigration `0020`～`0023`の同じ完成可能slot契約を使う。`0022`／`0023`は全履歴へのslot展開を等価な算術集計へ変更し、Read APIの30秒制限内でseries-statusを返す。

EURUSDはSaxo公表の標準FX条件をfreezeし、`America/New_York`のDSTを適用する。16:59〜17:04 New Yorkの価格停止・maintenanceとweekendをexpected slotから除外し、UTC正時から始まる1H全体がsession interval内に収まる場合だけ完成対象とする。calendar IDは`SBFX_24X5`、schedule versionは`saxo_fx_spot_trading_conditions_20260725_v1`、verificationは`VERIFIED`とする。特殊通貨pairやholiday overrideは対象外であり、必要時はSaxo `tradingschedule` endpointのredacted evidenceで再freezeする。

### 4.3 処理経路

```text
OAuth access token in memory
  -> Saxo SIM GET allow-list
  -> immutable raw JSON + SHA-256
  -> normalize / deduplicate / quality checks
  -> staging
  -> raw revision + curated + watermark + derivedを単一transaction commit
  -> expected watermark gate
  -> Read APIから即時参照
```

Read APIはDBを直接read-only参照するため、別のcopy/publication jobを設けない。curatedとwatermarkのcommitが公開境界となる。

### 4.4 retryと復旧

- access token期限接近: run前に自動refresh
- data jobがない時間も15秒heartbeatで期限を監視し、accessまたはrefresh期限の5分前にrotation
- API 401: 強制refresh後1回再試行
- 429、network、503: 既存Saxo GET clientの有限retryを使用
- DataVersion変更: failed instrumentだけguard付きfull-refetchし、優先runを再実行
- expected watermark未到達: `DATA_NOT_READY`として30秒後に再試行
- scheduler多重起動: `flock`で拒否
- scheduler停止から6時間以内: job種別ごとの最新slotをcatch-upし、deadline超過は`sla_status=MISS`

## 5. 運用state

Git管理外の`.runtime/periodic_update/`へ0600で保存する。

| file | 内容 |
|---|---|
| `state.json` | completed slot、last job、next slot、SLA、total-return blocker |
| `service.json` | repo-owned PID、command hash、start fingerprint |
| `periodic_update.log` | sanitized daemon output |
| `service.lock` | 多重起動防止 |

token、authorization code、refresh token、AppKeyそのものをstate/logへ保存しない。AppKey fingerprintだけをservice stateへ保存できる。

process identityの開始時刻は`ps`を`LC_ALL=C`で取得して`YYYY-MM-DDTHH:MM:SS`へ正規化する。旧日本語／英語fingerprintはPID、cwd、command SHA-256、期待module・portが全て一致するときだけ意味比較して移行する。PIDだけでは採用しない。

## 6. 操作

```bash
export SAXO_OAUTH_APP_KEY='<SIM application key>'

# Web UI方式
.venv/bin/python -m market_db.operator_ui
# http://127.0.0.1:8765/ でOAuth接続、定期更新開始

# CLI方式
.venv/bin/python -m market_db.saxo_auth login --callback-port 8765
.venv/bin/python -m market_db.saxo_auth status --callback-port 8765
.venv/bin/python -m market_db.periodic_update schedule
.venv/bin/python -m market_db.periodic_update_service start --callback-port 8765
.venv/bin/python -m market_db.periodic_update_service status
.venv/bin/python -m market_db.periodic_update_service stop
```

LaunchAgentは自動installしない。repo-owned serviceの実credential受入と取引日SLA実証後にoperatorが明示判断する。

## 7. Total-return DPU3

状態は`BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED`である。既存`20260712T135236Z`は`development_cutoff_only`のまま保持する。

provider、利用条件、adjusted-close/corporate-action定義、訂正方針、availability SLAをfreezeした後に、次を実装する。

1. current用raw取得
2. 既存snapshotとの重複期間parity
3. 新しい安定dataset IDとrun revision
4. 前営業日までのcoverage/freshness/quality
5. `/api/v1/total-return-status`
6. T+0 EOD、T+1朝のretry schedule

現時点ではYahoo Finance由来の既存snapshotを運用provider採用の根拠にしない。既存manifest自身がresearch snapshot限定、利用条件確認要、production feed非主張としているためである。operatorは少なくともprovider、ライセンス／再配布条件、adjusted close・cash dividend・split定義、corporate-action訂正方針、availability SLA、provider revision identityを承認する必要がある。承認まではprovider-neutralなduplicate、日付逆転、null／非正値、split／dividend、revision、ordered hash gateだけを実装し、取得・schedule・current dataset ID発行をblockする。

## 8. 受入gate

### DPU1

- Web UIで初回OAuthが成功する
- refresh前後でAPI GETが成功する
- Keychain内にaccess tokenがない
- refresh rotation後に旧refresh tokenを再利用しない
- Mac停止がrefresh期限を超えた場合は`AUTH_LOGIN_REQUIRED`

### DPU2

- 3 XNYS営業日連続で第1barを10:33 ETまでに公開
- 6系列のexpected watermark gateがPASS
- duplicate/null/non-positive/OHLC不整合が0
- write request、order、precheckが0
- API障害がdata quality FAILとして扱われない

### 最終統合

DPU3完了後にstrategy側がcurrent total-return dataset IDとspec hashを更新し、strategy repositoryで次を実行する。

```bash
.venv/bin/python scripts/validate_equity_reit_s6_sim_validation.py \
  --phase S6V5A \
  --integration
```

`saxo_db`は戦略計算または注文を実行しない。

## 9. Rollback

```bash
.venv/bin/python -m market_db.periodic_update_service stop
.venv/bin/python -m market_db.saxo_auth logout --callback-port 8765
```

その後、コードを直前commitへ戻す。raw artifact、ingestion run、quality証跡、既存curatedを手動削除しない。新しい定期runは既存の原子的transactionを利用するため、失敗時はcurated/watermarkを前進させない。

適用済みmigration `0020`～`0023`は書き換えたり削除しない。slot contractを戻す必要がある場合はschedulerを安全停止し、旧view定義を復元する新しいforward migrationを作成して、calendar、coverage、freshness、Read APIを再検証する。
