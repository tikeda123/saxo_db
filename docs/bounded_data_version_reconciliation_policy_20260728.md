# Saxo Chart DataVersion限定reconcile運用仕様

> 監査注記（2026-07-28）: 本文はSPY、SHY、GLDへ適用済みのlegacy
> `bounded_data_version_reconciliation_v1`を説明する履歴文書である。今後の検知へ
> 自動適用しない。future defaultは
> [`data_version_warning_review_policy_20260728.md`](data_version_warning_review_policy_20260728.md)
> であり、検知だけではreconcile、置換、full-refetch、オンライン停止を行わない。

## 1. 結論と適用範囲

DataVersion変更を無視せず、最初から全履歴を再取得する既定経路を、次の段階照合へ置き換える。

1. 対象instrumentの最新96本をSaxo Chart `GET`で再取得する。
2. content差分の前に16本連続の完成済み一致barがあれば、訂正境界を限定できたと判定する。
3. 境界が見つからない場合だけ384本、1200本へ拡大する。
4. 限定できた範囲だけをimmutable rawへ保存し、guard付きraw→curated経路でsupersedeする。
5. 境界不明、上限超過、広域変更疑い、欠損上限超過、正規化・OHLC・Bid/Ask違反では、対象instrumentだけを`BLOCKED_FULL_REFETCH`にする。

対象はcanonical 60分足の1系列単位である。追加候補FX 3ペアのonboarding契約、USDJPY quarantine、戦略・PnL・注文処理は変更しない。

## 2. Saxo公式資料との関係

Saxoの現行公式[Chart学習資料](https://www.developer.saxo/openapi/learn/chart)は、historical chartが訂正やcorporate actionで変わる可能性を説明し、DataVersion更新時には当該instrument/time horizonの全sampleを無効化して再取得するよう記載している。[Chart v3 endpoint reference](https://www.developer.saxo/openapi/referencedocs/chart/v3/charts/get__chart)は、`DataVersion`をデータのversion番号として返し、`Count`上限1200、`Mode=From/UpTo`と`Time`で範囲を指定できる。Chart subscription資料も、訂正またはcorporate action時にresetが発生すると説明する。

したがって本仕様は「公式資料が限定再取得を要求している」という解釈ではない。これはSIM研究運用で、全履歴取得コストを抑えつつ訂正を無視しないためのローカル運用ポリシーである。公式契約との差はmanifestとRead APIに残す。全履歴が変わっていないことを限定windowだけで数学的に証明することはできないため、境界を証明できないケースはfull-refetchへfail-closedする。

## 3. 凍結した判定値

| 項目 | 値 | 判定 |
|---|---:|---|
| 初期比較window | 96 bars | 最新から比較 |
| 拡大window | 384, 1200 bars | 前段で境界未確定時だけ |
| 安定anchor | 16 completed bars | 差分範囲の直前で連続content一致 |
| 限定置換上限 | 240 rows | 超過時はfull-refetch |
| provider欠落上限 | 64 rows | 超過時はfull-refetch |
| 広域変更疑い | completed一致対象の80%以上が変更 | corporate action等を疑いfull-refetch |

比較対象contentはOHLC、Bid/Ask OHLC、Volume、MarketTradingState、完成状態である。DataVersionだけが変わりcontentが同じ行は`version_only_rows`へ分ける。補間、Bid/Ask交換、clamp、forward fill、手動watermark更新は禁止する。

## 4. 監査モデル

migration `0027`は次を追加する。

- `ops.data_version_revision_event`: old/new DataVersion、比較期間、差分件数、限定範囲、最終状態、適用run、置換結果
- `ops.data_version_revision_step`: 96→384→1200の各比較stepとraw SHA-256
- `ops.v_data_version_revision_state`: 最新のinstrument別reconcile状態と利用可否
- `curated.prepare_bounded_revision`: ingest role、専用trigger、STALE watermark、READY event、instrument、置換範囲が完全一致するときだけDELETEを許可

通常incrementalでold/new DataVersionの不一致を検出した時点で、immutableな
`revision_detection.json`と`DETECTED` eventを同一失敗runへ記録する。続く段階照合は
同じevent IDを再利用してstepを追加し、API・正規化・品質検査の途中で止まっても
DataVersion変更そのものを監査から失わない。

migration `0028`は、Read API readerが基表のwrite権限を得ずにservice部分劣化を読める`ops.v_series_revision_availability`を追加する。

raw responseは既存の`ops.source_file`と`raw.market_bar_revision`へ追加し、curatedの対象範囲だけを置換する。4H/1Dは対象instrumentだけを再構築し、他instrumentのderived rowsをDELETE/INSERTしない。

## 5. schedulerとRead API

scheduler slotはcategory一括からinstrument laneへ分離する。SPYのrevision blockerはSPY laneだけを停止し、IWM/EFA/EEM/VNQ、EURUSD、債券、金のlaneを継続する。blockerが1件以上でもサービス状態は`RUNNING_DEGRADED`とし、`degraded_instruments`と複数の`operator_actions_required`を返す。全停止を意味する`BLOCKED_OPERATOR_ACTION_REQUIRED`へ昇格しない。

Read APIは次を追加する。

- `GET /api/v1/series-status`: `components.revision`にinstrument別status、old/new DataVersion、比較範囲、差分、利用可否を返す。
- `GET /api/v1/service-status`: `PASS / PARTIALLY_DEGRADED / BLOCKED`、利用可能数、degraded系列を返す。

migration前に検出済みのSTALE watermarkは`DETECTED_LEGACY`として合成し、revision eventがないことを成功扱いしない。

## 6. SPY・SHY・GLDのread-only dry-run

保持済みblocked runのraw 22本を現行curatedへ正規化比較した。DB、watermark、scheduler、Saxo側へのrequestは変更していない。証跡は[`sp_y_shy_gld_20260728.json`](../manifests/data_version_revision_dry_run/sp_y_shy_gld_20260728.json)に固定した。

| instrument | old → observed new DataVersion | content差分 | versionのみ | 新規 | 安定anchor | 判定 |
|---|---|---:|---:|---:|---:|---|
| SPY | 29738070 → 29751202 | 1 | 20 | 1 | 20 | READY_TO_APPLY |
| SHY | 29740653 → 29752511 | 1 | 20 | 1 | 20 | READY_TO_APPLY |
| GLD | 29738073 → 29752711 | 1 | 20 | 1 | 20 | READY_TO_APPLY |

3系列ともcontent差分は旧runの最後の形成中bar、新規は次取引日の先頭barであった。ただし22本の保持証跡だけによる判定なので、実適用前にはlive 96→必要時384→1200照合を必須とする。

## 7. 実データ適用gate

本実装時点ではSPY、SHY、GLDのlive reconcileを実行しない。実行を承認した場合のみ、scheduler停止中に対象を1つずつ次で実行する。

```bash
.venv/bin/python -m market_db.data_version_reconcile run \
  --instrument-key spy \
  --auth-mode keychain
```

`shy`、`gld`も同じ固定形を使う。実行結果が`PASS`でも、他instrumentのrow count、watermark、latest ingestion run IDが変わっていないことを検証してからschedulerを新コードで再開する。`BLOCKED_REVISION_SCOPE_UNRESOLVED_FULL_REFETCH_REQUIRED`なら、対象instrumentだけを既存guard付きfull-refetchへ送る。

## 8. rollback

- migrationは追加schemaのみで、既存OHLCを変更しない。問題時はschedulerを停止し、bounded reconcileを呼ばず旧guard付きfull-refetchを対象instrumentだけに使用する。
- bounded applyは1 transactionでraw登録、curated限定置換、watermark、derived対象instrument、revision eventを更新する。途中失敗は全rollbackする。
- apply前の旧raw revisionは削除しない。誤適用が疑われる場合はrevision eventとrun manifestを根拠に、対象instrumentのguard付きfull-refetchで再構築する。手動DELETEやwatermark直接UPDATEはrollback手段にしない。
