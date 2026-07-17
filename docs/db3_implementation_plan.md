# Phase DB3 実装計画書

作成日: 2026-07-17 JST
前提: `DB1=PASS / DB2=PASS`
状態: **IMPLEMENTED / LIVE GATE PASS / COMPLETE**

## 1. 目的

Saxo SIMの許可済みread endpointだけを使い、canonical 13銘柄の60分足をwatermarkから重複取得する。raw JSON、source file、raw revision、curated latest、historical revision、watermarkをrun IDで追跡し、受理済み完成1Hだけから4Hと1D risk barを決定論的に生成する。

DB3ではsession calendar、holiday、短縮取引、DSTを登録し、coverageとfreshnessを根拠付きで評価する。DB4のread API・一般backup/restore・retention、戦略、PnL、WFO、Holdoutは実施しない。

## 2. 現行公式契約の確認

2026-07-17にSaxo公式文書を再確認し、次を実装値として維持する。

- SIM REST base URL: `https://gateway.saxobank.com/sim/openapi`
- Chart: `GET /chart/v3/charts`
- 1 request最大1200 sample
- `Mode=From/UpTo`と`Time`は対で指定し、境界sampleを含む
- 最新sampleは形成中で、`DataVersion`変更時は全履歴を無効化して再取得
- default rate limitはsession/service groupごと120 request/minute
- Developer Portal tokenはSIM専用・24時間有効

## 3. 実装範囲

1. SIM限定GET client、token非表示、endpoint allow-list、429・一時的network例外のGET限定上限retry
2. canonical instrument detail照合と自動置換禁止
3. raw JSON atomic保存、SHA-256、相対path manifest
4. ETF 20本、FX 72本の実bar overlapとFrom paging
5. Decimal正規化、ETF native OHLC、FX Bid/Askとmid
6. quality gate、raw append、revision event、curated idempotent upsert
7. DataVersion変化のfull-refetch blockと自動`reconcile`
8. full-refetch限定の過去FX High/Low交差row隔離・監査・上限制御
9. watermark初期化・成功時のみ前進・失敗時rollback
10. US ETF calendarとFX schedule登録、coverage/freshness view
11. 完成1Hだけから4H/1D生成
12. unit/fixture/DB transaction testとmanual SIM integration gate
13. localhost限定operator UIによるsession-only token受渡し、single reconcile job、AI側進捗監視

DataVersion不一致時は通常runを`BLOCKED_FULL_REFETCH_REQUIRED`で全rollbackし、対象watermarkを`STALE_DATA_VERSION`へ移す。復旧は対象1銘柄、専用trigger、STALE watermarkの3条件をprocedureが確認した場合だけ、`Mode=UpTo`全履歴refetchとcurated再構築を許可する。既存最古時刻に届かなければ置換前に停止する。

full-refetchで過去FxSpotのHigh/Low集計極値だけに`Bid > Ask`が含まれる場合は、最大10 unique rowかつ全unique観測の0.01%以下、最新形成中sample以外という全条件を満たしたときだけ隔離する。source値は修正せずraw JSONへ残し、該当rowをDB barから除外して`rejected_rows`と解決済みWARNへ記録する。Open/Close交差、通常run、閾値超過、重複矛盾は全runをFAILする。

## 4. Runtime順序

1. migration `0010`を適用する。
2. DB2 curated 1Hからcanonical 13銘柄のwatermarkを初期化する。
3. calendarを登録し、instrumentへ割り当てる。
4. 既存完成1Hから4H/1Dを生成する。
5. tokenなしのunit・fixture・DB failure injectionを実行する。
6. operatorがsession-only tokenを入力した場合だけsmoke test、13 detail、13 chart、2回目idempotencyを実行する。
7. 全live gate成功後だけDB3をPASSとする。

tokenがない場合、実装・offline runtime gateを完了しても総合判定は`BLOCKED_LIVE_SIM_TOKEN`とする。tokenをfile、DB、manifest、logへ保存しない。

長期full-refetchの途中でtimeout・接続切断等の一時的network例外が起きた場合は、同一GETだけを1/2/4秒待機で最大4attemptまで再試行する。4回失敗時は`FAILED_NETWORK`でrunを停止し、無限retry、write API retry、別instrumentへの自動代替はしない。

operator UIはDB4の一般read APIやDB管理UIではない。DB3 live gateの固定`reconcile`だけを起動する一時credential bridgeであり、loopback bind、same-origin/CSRF、no-store、single job、`shell=False`、出力redactionを必須とする。ユーザーがpassword欄へ入力し、AIはtoken値を取得せず開始・監視だけを行う。

2026-07-17実施結果は`docs/db3_implementation_result.md`を正本とする。offline gateとlive gateは全項目PASSした。canonical 13 watermarkはすべて`ACTIVE`、EURUSD/USDJPYの限定隔離付きfull-refetch、通常run 104・105の連続PASS、DB3総合validator PASSを確認した。DB4だけを次工程として解放し、RT0はDB4 PASSまでLOCKEDを維持する。
