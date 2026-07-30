# Saxo APIデータ取得 実装ハンドオフ

作成日: 2026-07-16 JST  
対象環境: Saxo OpenAPI Simulation（SIM）  
状態: **DB3 PASS / DB4 PASS / RT0 NEXT**

## 1. この文書の目的

この文書は、前の会話や旧`../saxo_api`プロジェクトを参照できない別のAIが、`saxo_db`内だけでSaxo APIから市場データを新規取得する機能を実装するためのハンドオフである。

対象は次の2経路である。

1. **初回フル取得**: DBをゼロから再構築するとき、13銘柄の60分足を取得可能な最古時点まで後方取得する。
2. **増分取得**: DB2完了後、`ops.watermark`を起点に最新60分足を重複取得し、revisionを監査しながらDBへ反映する。

DB1・DB2・DB3はPASSし、DB3のAPI取得、DB transaction、派生足、運用CLIは実装・live検証済みである。本書は現在のDB3取得契約と障害復旧手順を定義する。DB4は次工程、研究工程はDB4 PASSまでLOCKEDである。

## 2. 変更してはいけない取得方針

- API gatewayはSIMの `https://gateway.saxobank.com/sim/openapi` に固定する。
- 正本として新規取得する時間足は**60分足のみ**とする。
- 4時間足と日次リスク足は、品質合格済みの完成60分足からDB内で決定論的に生成する。
- Saxo APIの生4時間足を新規取得・更新しない。
- 対象は固定13銘柄、AssetTypeは`Etf`または`FxSpot`だけとする。
- 銘柄はsymbolだけではなく、`environment + Uic + AssetType`で識別する。
- ETFをStock、CFD、ETF CFD、別市場上場へ自動置換しない。
- Access Token、AccountKey、ClientKey、口座識別子をファイル、DB、ログ、manifest、browser storageへ保存しない。
- 注文、注文事前確認、position、balance、account一覧は呼び出さない。
- 取得したChart raw応答はatomic保存し、SHA-256を記録してから変換する。
- 最新の形成中barは保存してよいが、研究・シグナル・派生足では使用しない。
- ETFのSaxo Chart OHLCをtotal-return系列として扱わない。
- FX Chartには過去の実現swap/rollover費用が含まれないため、overnight戦略をこれだけで解放しない。
- LIVE接続は本仕様の範囲外である。base URLの切替だけでLIVEへ接続できる設計にしない。

## 3. 公式仕様と確認済み事項

実装時には次のSaxo公式ページを正本として再確認する。

- [Environments](https://www.developer.saxo/openapi/learn/environments): SIMとLIVEのURL、Developer Portalのone-day tokenはSIM専用であること
- [Security](https://www.developer.saxo/openapi/learn/security): 全API呼出しにAccess Tokenが必要で、Portal tokenは24時間有効であること
- [OpenAPI Request/Response](https://www.developer.saxo/openapi/learn/openapi-request-response): `Authorization: Bearer` headerとHTTP errorの扱い
- [Chart endpoint](https://www.developer.saxo/openapi/referencedocs/chart/v3/charts/get__chart): `Uic`、`AssetType`、`Horizon`、`Count`、`Mode`、`Time`、`FieldGroups`
- [Chart usage](https://www.developer.saxo/openapi/learn/chart): 最新barは更新中であること、`DataVersion`変更時の全履歴再取得
- [Reference Data](https://www.developer.saxo/openapi/learn/reference-data): instrument searchとUIC解決
- [Instrument endpoints](https://www.developer.saxo/openapi/referencedocs/ref/v1/instruments): instrument summary/detail API
- [Rate Limiting](https://www.developer.saxo/openapi/learn/rate-limiting): 既定limit、rate-limit header、HTTP 429

2026-07-16確認時点のChart APIは、1回あたり最大1200 samplesである。Chartは単一の`Uic + AssetType`に対するOHLCを返し、FXではBid/Ask、その他instrumentでは主にlast-traded値を返す。仕様は将来変更され得るため、実装開始時と各release upgrade時に上記公式ページを再確認する。

## 4. 認証とoperator手順

### 4.1 OAuth PKCEによる定期運用

2026-07-24以降の定期運用は`market_db.saxo_auth`のOAuth Authorization Code Grant with PKCEを使用する。Developer PortalのSIM Application Managementでアプリを作成し、PKCE redirect URIを`http://localhost/saxo/oauth/callback`として登録する。

```bash
export SAXO_OAUTH_APP_KEY='<SIM application key>'
.venv/bin/python -m market_db.operator_ui
```

`http://127.0.0.1:8765/`で「Saxo OAuth接続」を実行する。初回login後、refresh credentialとPKCE verifierだけをmacOS Keychainへ保存する。access tokenはprocess memoryだけに保持し、refresh時に返された新refresh tokenで旧値を置換する。token値はGit、`.env`、`.secrets/`、PostgreSQL、manifest、raw、log、browser storageへ保存しない。

定期service:

```bash
.venv/bin/python -m market_db.saxo_auth status --callback-port 8765
.venv/bin/python -m market_db.periodic_update schedule
.venv/bin/python -m market_db.periodic_update_service start --callback-port 8765
.venv/bin/python -m market_db.periodic_update_service status
```

初回、refresh credential失効、Saxo側revocation、Mac停止がrefresh期限を超えた場合だけ人間が再loginする。各定期runで24時間tokenを取得しない。

### 4.2 24時間tokenを使用するfallback

OAuth未設定または手動診断時だけ、operatorがSaxo Developer Portalの`Get 24 Hour Token`からSIM tokenを取得する。

1. Developer PortalでSimulation/Demo accountへloginする。
2. `Get 24 Hour Token`を開く。
3. 認証を完了し、表示されたAccess Tokenをコピーする。
4. tokenをDB画面や設定ファイルへ貼り付けず、実行processへ一時入力する。
5. 取得終了後にprocess environmentからtokenを消す。

CLI実装では、tokenをコマンドライン引数に渡さない。次のように対話入力し、そのshell sessionだけへexportする運用を想定する。

```bash
read -s SAXO_ACCESS_TOKEN
export SAXO_ACCESS_TOKEN
python3 -m market_db.saxo_smoke_test
python3 -m market_db.incremental_update run
unset SAXO_ACCESS_TOKEN
```

`read -s`のpromptは実装側で表示してよいが、token値は表示しない。shell history、process argument、exception、HTTP debug logにtokenを出さない。`.env`、`.secrets/`、Docker secret、PostgreSQLへSaxo tokenを保存しない。DB passwordとSaxo tokenは別物である。

24時間tokenは無人定期実行に使用しない。Portal loginやtoken copyをbrowser automationしない。従来のtoken入力reconcileは手動fallbackとして保持する。

### 4.3 疎通確認

最初のread-only smoke testは次のendpointだけを使用する。

```http
GET https://gateway.saxobank.com/sim/openapi/port/v1/users/me
Accept: application/json
Authorization: Bearer <session-only-token>
```

成功条件はHTTP 200とJSON objectである。response bodyにはuser/account関連情報が含まれ得るため、bodyをファイル、DB、test artifact、通常logへ保存しない。記録してよいのは、時刻、environment、endpoint ID、HTTP status、latency、sanitized error codeだけである。

## 5. 使用を許可するAPI

| 目的 | Method / path | 備考 |
|---|---|---|
| token疎通 | `GET /port/v1/users/me` | bodyは保存しない |
| instrument検索 | `GET /ref/v1/instruments` | discovery専用、結果から自動置換しない |
| instrument確認 | `GET /ref/v1/instruments/details/{Uic}/{AssetType}` | canonical masterとの一致確認 |
| trading schedule確認 | `GET /ref/v1/instruments/tradingschedule/{Uic}/{AssetType}` | session/DST監査で必要な場合 |
| OHLC取得 | `GET /chart/v3/charts` | 60分足だけをcanonical取得 |

許可リスト外のAPIはdefault denyとする。特に`/trade/*`、order、precheck、portfolio account一覧、balance、positionを増分取得processから呼ばない。

## 6. Canonical instrument master

DB3増分取得の機械可読な正本は `specs/source_collection/v13_db3_incremental_collection.json` である。旧`v12_intraday_collection.json`は初回収集時点の不変契約として保持する。

| category | key | symbol | UIC | AssetType | currency |
|---|---|---|---:|---|---|
| equity_reit | spy | `SPY:arcx` | 36590 | Etf | USD |
| equity_reit | iwm | `IWM:arcx` | 31933 | Etf | USD |
| equity_reit | efa | `EFA:arcx` | 31874 | Etf | USD |
| equity_reit | eem | `EEM:arcx` | 31871 | Etf | USD |
| equity_reit | vnq | `VNQ:arcx` | 34910 | Etf | USD |
| bond_credit | shy | `SHY:xnas` | 7522053 | Etf | USD |
| bond_credit | ief | `IEF:xnas` | 7522010 | Etf | USD |
| bond_credit | tlt | `TLT:xnas` | 3441903 | Etf | USD |
| bond_credit | tip | `TIP:arcx` | 31996 | Etf | USD |
| bond_credit | lqd | `LQD:arcx` | 31923 | Etf | USD |
| gold | gld | `GLD:arcx` | 32664 | Etf | USD |
| fx | eurusd | `EURUSD` | 21 | FxSpot | USD |
| fx | usdjpy | `USDJPY` | 42 | FxSpot | JPY |

毎回の取得前にdetail endpointで少なくともUIC、AssetType、Symbol、CurrencyCode/PriceCurrencyを照合する。exchange、trading status、description、format、sessionも取得runのreference snapshotとして保存できるが、`TradableOn`、AccountKey、ClientKey等はredactする。

次の場合は`BLOCKED_INSTRUMENT_DRIFT`として人間の確認を求める。

- UICまたはAssetTypeがdetail endpointで無効になった。
- Symbol、currency、primary listing、exchangeがcanonical masterと矛盾した。
- search結果が複数listingまたは別instrumentを示した。
- ETFではなくStock/CFDだけが返った。

search endpointは候補調査に使えるが、最高scoreのinstrumentを無条件に採用してmasterを書き換えてはならない。master変更は仕様差分、Saxo responseのredacted証跡、新旧UIC、影響データ範囲を提示して再凍結する。

## 7. Chart request contract

canonical requestは次で固定する。

```text
GET /chart/v3/charts
Uic=<canonical UIC>
AssetType=<Etf or FxSpot>
Horizon=60
Count=1200
FieldGroups=Data,DisplayAndFormat,ChartInfo
Mode=<omit | From | UpTo>
Time=<omit | exact sample timestamp in UTC>
```

`Mode`と`Time`は必ず同時に指定する。`Time`はUTC ISO-8601で、DBに存在する実sampleのtimestampを使用する。任意の時計時刻を生成してsample境界だと仮定しない。

保存対象response metadata:

- `ChartInfo.FirstSampleTime`
- `ChartInfo.Horizon`
- `ChartInfo.DelayedByMinutes`
- `ChartInfo.ExchangeId`
- `DataVersion`
- `DisplayAndFormat.Symbol`
- `DisplayAndFormat.Currency`
- retrieval時刻UTC
- HTTP status、safe rate-limit header、payload SHA-256

Authorization header、cookie、request object全体、account fieldは保存しない。

## 8. 初回フル取得アルゴリズム

既存CSVを使わずAPIから全履歴を再構築する必要がある場合だけ実行する。通常のDB2は移管済みCSVをimportするため、DB3の日常運用で毎回フル取得しない。

銘柄ごとの処理:

1. canonical instrument detailを照合する。
2. `Mode`と`Time`を省略し、最新側から`Count=1200`で取得する。
3. raw responseを一時ファイルへ書き、fsync後にatomic renameする。
4. SHA-256、size、row count、request metadataをmanifestへ追加する。
5. `Data[].Time`の最古timestampを`oldest`とする。
6. 次ページは`Mode=UpTo, Time=oldest`で取得する。
7. `UpTo`は境界sampleを再度含むため、`Uic + AssetType + Horizon + Time + price_basis`で重複排除する。同一DataVersionでも次の古いpage末尾は部分形成値を返す場合があるため、request順で最初に取得した完成側sampleを保持し、後続境界sampleで上書きしない。両方のraw responseとSHA-256は保持する。
8. 次のいずれかで停止する。
   - `Data`が空
   - 最古timestampが前ページから進まない
   - 最古timestampが`FirstSampleTime`以下
   - 設定済み最大page数へ到達
9. timestamp昇順へ並べ、正規化と品質検査を行う。
10. 最も新しいbarは形成中とみなし、初回runでは保守的に`is_complete=false`とする。

最大page数は50、1 pageは最大1200 samplesとする。page capに達して`FirstSampleTime`へ到達していない場合、成功扱いにせず`BLOCKED_PAGE_CAP`とする。

full runのraw保存先は次の形式とする。

```text
data/acquisition/runs/<UTC-run-id>/
  run_manifest.json
  collection_summary.csv
  raw/<market_key>/60m/page_001.json
  raw/<market_key>/60m/page_002.json
  reference/<market_key>.json
```

このdirectoryは`data/import/`と分離する。`data/import/`は移管済みの不変証跡であり、新規API responseを書き込まない。

## 9. 増分取得アルゴリズム

### 9.1 起点

銘柄・60分・price basisごとに`ops.watermark.latest_complete_time_utc`を読む。

- Etf: watermarkから実barを20本戻す。
- FxSpot: watermarkから実barを72本戻す。

「20時間前」「72時間前」と時計時間を減算せず、DBにある完成barをtimestamp降順で数えてoverlap開始sampleを決める。週末、祝日、exchange休場、DSTを時計時間の仮定で埋めない。

watermarkがない場合はinitial full acquisitionへrouteする。DB2直後はimport済み最新完成barからwatermarkを作成してから増分取得する。

### 9.2 forward paging

1. overlap開始sampleのtimestampを`cursor`とする。
2. `Mode=From, Time=cursor, Count=1200`で取得する。
3. raw pageをatomic保存しSHA-256を登録する。
4. boundary duplicateを主キーで除去する。
5. responseの最新timestampが前回cursorと同じなら停止し、`BLOCKED_CURSOR_NOT_ADVANCING`を記録する。
6. 1200 samplesが返り、現在まで到達していない場合、responseの最新sample timestampを次のcursorにする。
7. `Data`が空、1200未満、または最新側へ到達したら停止する。

増分取得は、Saxo公式Chart usageの「保存済み最新sampleを`Mode=From`の`Time`へ指定する」方式に従う。overlapを加えることで、形成中barの確定と短期historical correctionを再取得する。

### 9.3 transaction

1. PostgreSQL advisory lockを取得する。
2. `ops.ingestion_run`を`RUNNING`でcommitする。
3. API取得とraw file保存を行う。
4. `ops.source_file`へrelative path、SHA-256、size、row countを登録する。
5. normalized rowsをstagingへCOPYする。
6. instrument、time、OHLC、Bid/Ask、duplicate、completion、DataVersionを検査する。
7. 合格responseを`raw.market_bar_revision`へappendする。
8. 既存curatedと比較し、変更があれば`quality.event`へrevisionを記録する。
9. 品質合格rowだけを`curated.market_bar`へidempotent upsertする。
10. 影響期間のderived 4H/1Dを再生成する。
11. `ops.watermark`を更新してcommitする。
12. `ops.ingestion_run`を`SUCCEEDED`へ更新する。

途中失敗時はstaging、curated、derived、watermarkのtransactionをrollbackする。raw JSON fileは削除せず、失敗証跡としてSHA-256とともに残す。別transactionで`ops.ingestion_run=FAILED`、sanitized error code、対象instrument、最終成功stepを記録する。

## 10. `DataVersion`とhistorical correction

Saxo公式Chart資料は、`DataVersion`が変化した場合、そのinstrument/time horizonの全samplesを無効化して再取得するよう記載している。本プロジェクトではprovider訂正の可能性を無視せず、future defaultを[`data_version_warning_review_policy_20260728.md`](data_version_warning_review_policy_20260728.md)とする。検知・証跡・警告と、review・実データ置換を分離し、検知だけではオンライン停止や自動再取得を行わない。

処理規則:

1. responseの`DataVersion`を`ops.watermark.data_version`と比較する。
2. 同じ場合は通常のoverlap更新を続ける。
3. 変化した場合は取得済みChart JSONをrevision evidenceとして隔離保存し、accepted curatedとの限定sample差分を記録する。
4. eventを`REVIEW_PENDING / PENDING_REVIEW`、系列を`AVAILABLE_WITH_REVISION_WARNING`として公開する。watermarkは`ACTIVE`のaccepted versionを維持する。
5. schedulerはslotをwarning付きPASSとして終了し、他instrumentを含め通常継続する。自動bounded reconcileとfull-refetchを行わない。
6. operatorがevidenceをreviewし、current維持またはapply承認をreviewer・note・時刻とともに記録する。
7. `APPROVE_APPLY`済みeventへ明示`apply`した場合だけ、再比較・guard後に対象instrumentのcurated、derived、watermarkをatomic更新する。

限定sampleは警告の重要度判断用であり、置換範囲の自動決定ではない。ETFのsplit等を示す広域変更でも自動applyせず、reviewで必要な追加調査と取引停止判断を行う。

### 10.1 full-refetch時の限定FX極値隔離

通常増分runでは、FxSpotのいずれかのfieldで`Bid > Ask`を検出した時点で従来どおり全runをFAILする。例外は`DataVersion`復旧用の`manual_db3_full_refetch`だけであり、次をすべて満たす過去rowに限定する。

- AssetTypeが`FxSpot`で、交差fieldが`High`または`Low`だけである。
- 交差するunique rowが最大10件で、かつ取得したunique観測row全体の0.01%以下である。
- 形成中の最新sampleではない。
- 同一timestampに受理rowとreject rowが併存せず、重複page間の交差値が一致する。

合格したrowも値を修正しない。swap、interpolate、clamp、上書きは禁止し、raw JSON、page SHA-256、相対path、timestamp、元のBid/Askを保持する。該当rowは`raw.market_bar_revision`、curated、4H/1Dから除外し、`ops.ingestion_run.rejected_rows`へ件数、`quality.event`へ`db3_fx_crossed_extrema_quarantine`の解決済みWARNを1 rowずつ記録する。Open/Close交差、Bid/Ask欠損、OHLC違反、最新sample、件数または比率超過は例外にせず、DB barを変更しないまま全runをFAILする。

## 11. 正規化ルール

### 11.1 共通

- `Time`をUTC-aware timestampとして扱う。
- JSONの小数をbinary floatのままDBへ丸め込まず、文字列表現から`Decimal`へ変換して`NUMERIC(24,12)`へ格納する。
- `Volume`はnullable `NUMERIC(30,8)`。
- `MarketTradingState`はsource値を保持する。
- `retrieved_at_utc`はAPI response取得時刻。
- `payload_sha256`はbarを含むraw pageのSHA-256。
- 同一bar keyが複数pageにある場合、同一payload内では最新取得pageの値を採用し、duplicate countを記録する。

### 11.2 ETF

- sourceの`Open/High/Low/Close`を使用する。
- `price_basis=native_ohlc`。
- Bid/Ask列はsourceにない場合NULLのままにする。
- Saxo Chart OHLCは分配金再投資込みtotal returnではない。
- `curated.etf_total_return_daily`とは別table・別datasetとして保持する。

### 11.3 FX

- `OpenBid/Ask`、`HighBid/Ask`、`LowBid/Ask`、`CloseBid/Ask`をすべて保持する。
- BidとAskの両方がある場合だけ、各fieldのmidを`(Bid + Ask) / 2`で計算する。
- `price_basis=bid_ask_mid`。
- Bid/Ask欠損またはBid > Askを自動修正しない。限定full-refetch隔離でもsource値は変更しない。
- FXのVolumeはsource仕様上利用不能または意味が異なる場合があるため、NULLを失敗理由にしない。

## 12. 完成bar判定

Chartの最新sampleはbar開始時に作成され、その後更新される。したがって取得時点で最新rowを完成barとみなさない。

固定規則:

- 各responseの最新timestamp rowは`is_complete=false`として保存する。
- 後続runで同じtimestampより新しい60分barが存在したら、前barを完成候補へ昇格する。
- `time_utc + 60分 <= retrieval_time`だけで完成と断定せず、次barの存在またはsession終了規則と組み合わせる。
- `ChartInfo.DelayedByMinutes`を記録し、delayを考慮する。
- 分析viewは`is_complete=true AND quality_status='PASS'`だけを返す。

休場直前barなど、次barが翌営業日まで現れないケースはtrading scheduleに基づいて完成判定する。このsession calendar実装はfixtureとDST testを伴うこと。

## 13. 品質gate

銘柄単位で次をすべて検査する。

- UIC、AssetType、environmentがcanonical masterと一致
- `ChartInfo.Horizon=60`
- timestampが非NULL、UTC、主キー重複なし、昇順
- OHLCがすべて存在し正数
- `High >= max(Open, Low, Close)`
- `Low <= min(Open, High, Close)`
- completed barが未来を指さない
- FxSpotは各OHLCのBid/Askが存在
- FxSpotは各fieldで`Bid <= Ask`
- watermarkが後退しない
- raw payload fileのSHA-256が登録値と一致
- Access Token、AccountKey、ClientKeyをpayload/DB/logへ保存していない
- HTTP requestが許可endpointだけ
- order/precheck countが0

重大品質違反は、値を補正して続行せず、runをFAILさせる。raw payloadとquality eventに事実を残す。DataVersion correctionは通常品質FAILとは別にfull refetchへrouteする。唯一の限定例外は10.1の過去FX High/Low極値隔離であり、通常増分runには適用しない。

## 14. rate limit、retry、error処理

Saxo公式の既定値ではsession/service groupあたり120 requests/minuteである。実装は上限ぎりぎりを狙わず、Chart full acquisitionでは最大90 requests/minuteをclient-side limitとする。

- 正常responseの`X-RateLimit-*-Remaining`と`Reset`をtokenなしで記録する。
- Remainingが安全閾値を下回ったらResetまで待つ。
- HTTP 429は合計4 attemptsまで。待機は1秒、2秒、4秒を基本とし、rate-limit resetが長ければheaderを優先する。
- 4回目も429ならrunを`BLOCKED_RATE_LIMIT`とする。
- 400: retryせずrequest parameterを監査する。
- 401: `BLOCKED_TOKEN_EXPIRED`。新しいtokenをoperatorへ求め、tokenをlogへ出さない。
- 403: `BLOCKED_PERMISSION_OR_NETWORK_REPUTATION`。HTML/bodyは秘密情報をredactし、Reference番号だけを記録できる。
- 404: `BLOCKED_INSTRUMENT_DRIFT`。自動で別instrumentへ切り替えない。
- 503はretryせず`FAILED_SERVICE_UNAVAILABLE`でrunをFAILさせる。
- timeout・接続切断等のnetwork例外は同一GETだけを1/2/4秒待機・最大4attemptで有限retryし、継続時は`FAILED_NETWORK`でrunをFAILさせる。

retryはGETだけに限定する。本processには書込系Saxo APIを実装しない。

## 15. raw artifactとmanifest

各取得runの`run_manifest.json`には次を含める。

```text
schema_version
run_id
environment
base_url_id                 # URL値はSIM allow-listと照合
started_at_utc
finished_at_utc
trigger                     # manual_full / manual_incremental / scheduled_future
requested_instruments
successful_instruments
horizon_minutes             # 60 only
page_count
source_row_count
normalized_row_count
quality_pass_count
rejected_row_count
data_versions_by_instrument
watermark_before_after
rate_limit_summary
orders_or_prechecks_sent    # 必ず0
access_token_saved          # 必ずfalse
account_identifier_saved    # 必ずfalse
files                       # relative path, sha256, size_bytes, row_count
gate                        # PASS / FAIL / BLOCKED_*
sanitized_errors
```

manifestへ絶対path、hostname固有path、token、Authorization header、user-info responseを保存しない。relative pathだけを記録する。

## 16. DB3で実装する予定ファイル

DB1とDB2がPASSした後、少なくとも次を実装する。

```text
market_db/saxo_client.py
market_db/saxo_smoke_test.py
market_db/instrument_registry.py
market_db/acquire_full.py
market_db/incremental_update.py
market_db/normalize_bars.py
market_db/quality.py
market_db/raw_artifacts.py
market_db/derive_bars.py
tests/fixtures/saxo/
tests/test_saxo_client.py
tests/test_acquire_full.py
tests/test_incremental_update.py
tests/test_normalize_bars.py
tests/test_data_version_revision.py
tests/test_acquisition_security.py
docs/db3_incremental_update_result.md
manifests/db3_implementation_manifest.json
```

HTTP clientはSIM base URLをconstructor任せにせずallow-list検証する。testはfake session/recorded sanitized fixtureを使い、通常のunit testでSaxo APIを呼ばない。実API testは明示flagを付けたmanual integration testだけとする。

## 17. 必須test

### Unit test

- tokenなしで起動拒否
- tokenがrepr、exception、log、manifestへ出ない
- SIM以外のbase URLを拒否
- UIC + AssetType request生成
- `Count`を1200以下へ制限
- `Mode`と`Time`の片方だけを拒否
- `UpTo`/`From`境界duplicateを決定論的に除去
- cursor非前進を検出
- 429で1/2/4秒retryし4回目で停止
- network例外で1/2/4秒retryし4回目で`FAILED_NETWORK`
- 401/403/404のsanitized gate変換
- ETF native OHLC正規化
- FX Bid/Ask保存とmid計算
- FX Bid > Askを修正せずFAIL
- full-refetch限定のFX High/Low隔離が最大10件・0.01%・最新sample禁止をすべて検査
- Open/Close交差、閾値超過、受理/reject競合で全runをFAIL
- 隔離rowの原値・raw path・SHA-256を解決済みWARNと`rejected_rows`へ記録
- 最新barをincompleteにする
- overlapがEtf 20本、FxSpot 72本
- DataVersion変化をfull refetchへroute
- 同じpayloadを2回処理してcurated件数が増えない
- quality failure時にcurated/watermarkが変わらない
- instrument driftで自動置換しない
- 注文・precheck endpointがコードのallow-listに存在しない

### SIM integration test

- `/port/v1/users/me`が200、body保存ゼロ
- 13 instrument details照合
- 13銘柄の最新60分足を各1 request取得
- 13/13でraw SHA-256、normalized rows、DataVersionを記録
- 2回目の取得でboundary duplicateが増えない
- token/account識別子scanが0
- Saxo側のPOST/PUT/PATCH/DELETEが0

### DB transaction test

- advisory lockで同時writerが1つだけ
- run statusがRUNNINGからSUCCEEDED/FAILEDへ遷移
- raw revisionはappend-only
- curated upsertは`retrieved_at_utc`が新しい場合だけ
- watermarkは成功時だけ前進
- failure injection後も直前のcurated/watermarkが維持
- derived 4H/1Dが完成60分足だけから再生成

## 18. DB3合格条件

次をすべて満たした場合だけAPI増分取得をPASSとする。

- SIMの許可endpointだけで13銘柄60分足を取得できる。
- 初回、通常増分、長期停止後のforward pagingが再現可能。
- raw JSON、DB raw revision、curated、watermarkをrun IDで追跡できる。
- 同一runの再実行がidempotent。
- historical correctionとDataVersion変更を検出できる。
- 最新形成中barが研究viewへ出ない。
- 4H/1DがSaxo rawではなく受理済み60分足から生成される。
- quality failureでcurated/watermarkが前進しない。
- 429を無限retryせず、公式headerを考慮できる。
- network例外をGET限定・最大4attemptでretryし、継続時は停止できる。
- token、account情報、Authorization headerが永続化されていない。
- order/precheck/API writeが0。
- 実行結果、test、manifest、未解決blockerが`docs/db3_incremental_update_result.md`に記録されている。

## 19. 既知の制約

- SIMはLIVEの完全な複製ではなく、一部market data/functionが利用できない場合がある。
- OAuth refresh chainが継続している間は無人更新できるが、Mac停止・sleepがrefresh期限を超えた場合は人間の再loginが必要になる。
- repo-owned periodic serviceは実装済みだが、LaunchAgentは未installである。
- Chartの最新closeはwatchlist用の最適なreal-time quoteではない。
- Chart historyはinstrument/asset classにより開始時点が異なる。
- ETF Chart OHLCだけではdividend込みreturnを再現できない。
- FX Chartだけではpoint-in-timeの実現swapを再現できない。
- 13銘柄の固定UICはSIMで確認済みのsnapshotであり、永続不変とは仮定しない。
- Saxoのinterface version、asset type、rate limitが変わる可能性があるため、公式documentationとrelease noteを定期確認する。

## 20. 別AIへの再開指示

DB1・DB2完了後、別AIへ次のように依頼できる。

> `README.md`、`docs/saxo_api_data_acquisition_handoff.md`、`specs/v13_phase_db0_database_spec.json`、`specs/source_collection/v13_db3_incremental_collection.json`を全文読んでください。Phase DB3 live reconciliationだけを継続し、SIM限定・13銘柄・canonical 60分足の増分取得を完了してください。tokenはprocess sessionだけで扱い、Saxo APIのwrite endpointを実装しないでください。DataVersion full-refetchでは本書10.1の限定隔離を厳守し、raw JSON、SHA-256、quality event、`rejected_rows`を検証してください。最後に通常runを連続2回PASSさせ、DB3 validatorを実行してください。DB4、戦略、PnL、WFO、Holdoutへは進まないでください。

---

現在の取得ゲート: `DB3 PASS / DB4 PASS / RT0 NEXT`
