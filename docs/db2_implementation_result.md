# Phase DB2 実装結果

実施日: 2026-07-17 JST
対象仕様ID: `v13_database_prerequisite_20260716_v2`
対象研究線: `v13categoryintraday`
総合判定: **PASS**

## 1. 結論

検証済み69 CSVをimmutableな入力としてPostgreSQLへ移行し、全781,808 source rowをraw market bar、reference、curated 1H、ETF total-returnへ分類保存した。69 source fileをそれぞれ独立したingestion runへ登録し、file単位の件数・SHA-256・lineageを照合した。既知品質FAILは修正・削除せず、raw原本とOPEN quality eventとして保持した。

`2024-06-28T23:59:59Z`以前だけを`saxo_research_v13`へ物理copyし、default read-onlyに固定した。snapshot content manifestとcustom-format dumpを作成し、SHA-256と`pg_restore --list`を検証した。全統合testは39件PASS、DB2 validatorもPASSであり、DB2の全必須gateを満たした。

DB2はデータ移行・snapshotの品質gateであり、Saxo API増分更新、4H/1D派生、session calendar、read API、一般backup/restore、戦略の優位性を証明するものではない。

## 2. Source inventoryと分類結果

| 項目 | 実測値 |
|---|---:|
| CSV file | 69 |
| source row | 781,808 |
| source bytes | 160,403,659 |
| inventory SHA-256 | `72abbdcedd75b290b46d4ca8396125ebe99863e16e8c570c0f06fdf8440282db` |
| missing file | 0 |
| size mismatch | 0 |
| SHA-256 mismatch | 0 |
| source file mutation | 0 |

| 保存先 | file | row | 役割 |
|---|---:|---:|---|
| `raw.market_bar_revision` | 44 | 636,629 | 1H、raw 4H archive、legacy daily reference |
| `raw.reference_observation` | 24 | 90,894 | summary、master、外部原本、macro、RA0 metadata |
| `curated.etf_total_return_daily` | 1 | 54,285 | Saxo rawと分離した調整済みtotal-return |
| 合計 | 69 | 781,808 | source row全件 |

1H 394,992行はrawに加え、curated latestへも登録した。同一source rowのraw/curated二層保持は意図したlineageであり、source row合計へ二重加算しない。

## 3. Market DB実測値

| object | row |
|---|---:|
| `catalog.source_dataset` | 6 |
| `catalog.instrument` | 18 |
| `ops.ingestion_run` | 69 |
| `ops.source_file` | 69 |
| `raw.market_bar_revision` | 636,629 |
| `raw.reference_observation` | 90,894 |
| `curated.market_bar` | 394,992 |
| `curated.etf_total_return_daily` | 54,285 |
| `quality.event` | 5 |
| `ops.research_snapshot` | 1 |

raw market barの時間足別内訳:

| horizon | row | complete | incomplete |
|---:|---:|---:|---:|
| 60分 | 394,992 | 394,979 | 13 |
| 240分 | 130,389 | 130,376 | 13 |
| 1440分 | 111,248 | 111,230 | 18 |

curated 1Hは主キー重複0件、complete 394,979件、incomplete 13件である。ETF total-returnは`PASS` 54,283件、`WARN` 2件、`FAIL` 0件だった。

source file台帳のrow count合計は781,808で、全69 fileのlineage mismatchは0件だった。全ingestion runはPASSである。再実行では`imported_files=0`、`skipped_files=69`、`imported_source_rows=0`となり、既存rowを変更しなかった。

## 4. 品質状態

source summaryがFAILとしていた5系列を、`source_series_quality_gate`の`ERROR / OPEN` eventとして登録した。

- intraday raw 240分: EURUSD、USDJPY
- legacy Saxo ETF daily: 3系列

actionは`RAW_ARCHIVE_ONLY_DB2`である。元CSV、raw bar、observed valueは変更していない。session calendarはDB3で登録するため、coverageは全件`NOT_EVALUATED`とし、欠損0やPASSへ偽装していない。

## 5. Research snapshot

物理snapshotはdatabase linkを使わず、market DBとresearch DBの独立接続間でcopyした。FDWとdblinkのobjectは0件である。

| object | research row |
|---|---:|
| `raw.market_bar_revision` | 544,397 |
| `raw.reference_observation` | 83,978 |
| `curated.market_bar` | 329,745 |
| `curated.etf_total_return_daily` | 54,285 |
| `quality.event` | 5 |

| 境界・証跡 | 実測値 |
|---|---|
| cutoff | `2024-06-28T23:59:59Z` |
| raw最大時刻 | `2024-06-28T20:00:00Z` |
| curated最大時刻 | `2024-06-28T20:00:00Z` |
| reference最大時刻 | `2024-06-28T00:00:00Z` |
| total-return最大日 | `2024-06-28` |
| cutoff超過row | 0 |
| market raw除外row | 92,232 |
| market curated除外row | 65,247 |
| database default read-only | `on` |
| content manifest SHA-256 | `c275d078dcdff418ff2d34eb8e2e38a8d790510881556341f6bafbf0c8b63d6b` |
| dump SHA-256 | `3211fc37bdda970171d1ffe9c1767bbea4d49fe4cc2538a09318950cd3228160` |
| dump size | 53,397,996 bytes |
| dump形式 | `pg_dump` custom format |
| `pg_restore --list` | PASS |
| restore smoke test | LOCKED UNTIL DB4 |

snapshot作成の再実行は`skipped_existing`となり、content hashとdump hashが変化しないことを確認した。dump本体はGit管理外で、content/dump manifestだけを追跡する。

## 6. Migrationと実装

DB2で`0009_db2_import_support.sql`をmarket/research DBへ適用した。SHA-256は`439875350a7aa22e74ca040e06f01de9e496fa9e2b0a0aa62ab5e3ed88b12691`である。

主な追加内容:

- `raw.reference_observation`とsource-row payload hash
- `curated.etf_total_return_daily.source_file_id`
- research snapshotのmanifest/dump metadata
- referenceを含むinventoryとfile単位lineage view
- immutable inventory検証・冪等import CLI
- cutoff copy・read-only固定・snapshot dump CLI
- DB2 validatorとunit/integration test

適用済みmigrationのchecksum検証は16 target-rowすべてPASSし、同内容再実行はskipされた。

## 7. 検証結果

```bash
SAXO_DB_INTEGRATION=1 .venv/bin/python -m pytest -q
```

結果は`39 passed`、failure 0、skip 0である。

```bash
.venv/bin/python -m market_db.validate --phase db2
```

結果は`PASS`。source inventory、DB object、migration checksum、Docker health、market件数、時間足内訳、curated品質、lineage、research cutoff/read-only、content/dump hash、`pg_restore --list`を実接続で確認した。

通常restart後にもserviceはhealthyへ復帰し、market全件数、research snapshot件数・cutoff・default read-only、全migration checksumが保持された。Git管理対象および追加予定のtext 65 fileを実secret 8値との完全一致で検査し、検出は0件だった。host固有絶対pathのactive code/config検出も0件である。

## 8. DB2で実施していないこと

- Saxo OpenAPI接続、24時間token入力、market data request
- order/precheck request
- watermark増分更新
- session calendar、holiday、短縮取引、DST登録
- 1Hからの4H・1D派生生成
- RA0 baselineの再計算、特徴量、signal、position、cost、PnL
- WFO、Holdout、portfolio allocation
- Flask read API
- 別名DBへのrestore smoke test、一般backup/retention運用
- volume削除、database drop、source CSV変更

Saxo API call、order/precheck、戦略計算、破壊操作はいずれも0件である。

## 9. Gate

```text
DB0 v2  RE-FROZEN
DB1     PASS
DB2     PASS
DB3     NEXT
DB4     LOCKED
RT0     LOCKED UNTIL DB4 PASS
```

DB2 PASSにより解放できるのはDB3だけである。DB3は`docs/saxo_api_data_acquisition_handoff.md`を正本として、canonical 1H増分更新、revision、4H/1D派生、session calendar、freshness監視を実装する。DB4とRT0へは進まない。
