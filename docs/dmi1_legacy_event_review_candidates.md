# DMI1 旧quality eventレビュー結果

更新日: 2026-07-20 JST

状態: **PASS — CURRENT 5 / HISTORICAL 17 / UNKNOWN 0**

## 目的

Migration 0015適用前に存在したOPEN eventを、eventごとにsource、失敗run、復旧run、current watermarkと照合した結果です。reviewは`quality.event`を変更せず、追記専用tableへoperator label `codex-dmi1b-20260720`で記録しました。

raw archiveの異常5件は現在も事実なのでCURRENTを維持します。ただしscopeをraw 240分/1440分に限定し、canonical 1Hの利用可否には混入させません。atomic run 17件は下表のPASS runで復旧済みです。

## レビュー対象

| Event ID | instrument_key | rule | failed run | review | superseding PASS run |
|---:|---|---|---|---:|---|---|
| 13 | spy | source_series_quality_gate | 21 | SERIES/raw/CURRENT | — |
| 14 | efa | source_series_quality_gate | 21 | SERIES/raw/CURRENT | — |
| 15 | tip | source_series_quality_gate | 21 | SERIES/raw/CURRENT | — |
| 16 | eurusd | source_series_quality_gate | 43 | SERIES/raw/CURRENT | — |
| 17 | usdjpy | source_series_quality_gate | 43 | SERIES/raw/CURRENT | — |
| 32 | — | db3_atomic_run_gate | 72 | RUN/curated/HISTORICAL | 104 |
| 33 | spy | db3_atomic_run_gate | 73 | RUN/curated/HISTORICAL | 74 |
| 28213 | iwm | db3_atomic_run_gate | 76 | RUN/curated/HISTORICAL | 77 |
| 56385 | efa | db3_atomic_run_gate | 78 | RUN/curated/HISTORICAL | 79 |
| 84557 | eem | db3_atomic_run_gate | 80 | RUN/curated/HISTORICAL | 82 |
| 84558 | — | db3_atomic_run_gate | 81 | RUN/curated/HISTORICAL | 104 |
| 112730 | vnq | db3_atomic_run_gate | 83 | RUN/curated/HISTORICAL | 84 |
| 140908 | shy | db3_atomic_run_gate | 85 | RUN/curated/HISTORICAL | 86 |
| 156595 | ief | db3_atomic_run_gate | 87 | RUN/curated/HISTORICAL | 88 |
| 172282 | tlt | db3_atomic_run_gate | 89 | RUN/curated/HISTORICAL | 90 |
| 190609 | tip | db3_atomic_run_gate | 91 | RUN/curated/HISTORICAL | 92 |
| 218786 | lqd | db3_atomic_run_gate | 93 | RUN/curated/HISTORICAL | 94 |
| 246964 | gld | db3_atomic_run_gate | 95 | RUN/curated/HISTORICAL | 96 |
| 275135 | eurusd | db3_atomic_run_gate | 97 | RUN/curated/HISTORICAL | 100 |
| 275136 | eurusd | db3_atomic_run_gate | 98 | RUN/curated/HISTORICAL | 100 |
| 335098 | usdjpy | db3_atomic_run_gate | 101 | RUN/curated/HISTORICAL | 103 |
| 335099 | usdjpy | db3_atomic_run_gate | 102 | RUN/curated/HISTORICAL | 103 |

## 再検証

固定planはwriteなしで再検証できる。

実行例（note/reasonはpromptへ入力）:

```bash
.venv/bin/python -m market_db.operate reconcile-dmi1-legacy \
  --operator operator-label
```

`status=PLAN_VALID`、`database_writes=0`を確認する。再適用が必要な場合だけ明示的に`--apply`を付ける。既存reviewとの競合、復旧run非PASS、13系列watermark不一致ではfail-closedで停止する。
