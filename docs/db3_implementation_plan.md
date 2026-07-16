# Phase DB3 実装計画書

作成日: 2026-07-17 JST
前提: `DB1=PASS / DB2=PASS`
状態: **OFFLINE IMPLEMENTATION COMPLETE AND PASS / LIVE SIM GATE BLOCKED BY SESSION TOKEN**

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

1. SIM限定GET client、token非表示、endpoint allow-list、429上限retry
2. canonical instrument detail照合と自動置換禁止
3. raw JSON atomic保存、SHA-256、相対path manifest
4. ETF 20本、FX 72本の実bar overlapとFrom paging
5. Decimal正規化、ETF native OHLC、FX Bid/Askとmid
6. quality gate、raw append、revision event、curated idempotent upsert
7. DataVersion変化のfull-refetch block
8. watermark初期化・成功時のみ前進・失敗時rollback
9. US ETF calendarとFX schedule登録、coverage/freshness view
10. 完成1Hだけから4H/1D生成
11. unit/fixture/DB transaction testとmanual SIM integration gate

DataVersion不一致時は通常runを`BLOCKED_FULL_REFETCH_REQUIRED`で全rollbackし、対象watermarkを`STALE_DATA_VERSION`へ移す。復旧は対象1銘柄、専用trigger、STALE watermarkの3条件をprocedureが確認した場合だけ、`Mode=UpTo`全履歴refetchとcurated再構築を許可する。既存最古時刻に届かなければ置換前に停止する。

## 4. Runtime順序

1. migration `0010`を適用する。
2. DB2 curated 1Hからcanonical 13銘柄のwatermarkを初期化する。
3. calendarを登録し、instrumentへ割り当てる。
4. 既存完成1Hから4H/1Dを生成する。
5. tokenなしのunit・fixture・DB failure injectionを実行する。
6. operatorがsession-only tokenを入力した場合だけsmoke test、13 detail、13 chart、2回目idempotencyを実行する。
7. 全live gate成功後だけDB3をPASSとする。

tokenがない場合、実装・offline runtime gateを完了しても総合判定は`BLOCKED_LIVE_SIM_TOKEN`とする。tokenをfile、DB、manifest、logへ保存しない。

2026-07-17実施結果は`docs/db3_implementation_result.md`を正本とする。offline gateは全項目PASS、live smoke・13 detail/schedule/chart・直後2回目runだけが未実施である。DB4とRT0はLOCKEDを維持する。
