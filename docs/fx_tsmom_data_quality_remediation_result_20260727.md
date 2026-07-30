# FX TSMOMデータ品質改善結果

## 現在判定

`BLOCKED_PROVIDER_CONTENT_QUALITY_USDJPY`

R1、R2、R4、R5の実装とoffline検証は完了した。R3はEURUSDだけ完了し、USDJPYはSaxoの新しいDataVersionが返した履歴OHLCのcontent-quality異常でfail-closed停止した。R6はEURUSDが合格、USDJPYが未合格である。

本作業は`saxo_db`の取得、raw/curated、coverage、freshness、quality、watermark、lineage、scheduler、Read APIだけを対象とする。FX signal、PnL、position、precheck、orderは実装・実行していない。

## USDJPY隔離中の定期更新scope

USDJPYだけを`BLOCKED_PROVIDER_CONTENT_QUALITY`として停止し、正常系列の更新は継続する。一時scope `all_except_usdjpy_provider_quarantine_20260727`はEURUSDとETF 11系列を含み、USDJPYを明示的に除外する。ETFは既存XNYS完成1H contractのまま株式・REIT、債券・Credit、Goldのcategory別slotで処理し、EURUSDは既存SBFX 24x5毎時slotで処理する。正本は`specs/source_collection/periodic_scheduler_scope_v1.json`、runtime証跡は`.runtime/periodic_update/state.json`の`scheduler_scope`である。

USDJPYを再開できるのは、provider訂正版DataVersion確認、値補正やgate緩和を伴わないguard付きfull-refetch PASS、通常run連続2回PASSのすべてが成立した場合だけである。

2026-07-27T09:10Zに同scopeでschedulerを開始した。最初のEURUSD slotは新DataVersionを検出して`BLOCKED_FULL_REFETCH_REQUIRED`となったため、schedulerを停止し、Keychain OAuthによるEURUSD単独full-refetch run 829を実行した。run 829はDataVersion `29749255`、status PASS、bounded quarantine 9件、orders/prechecks/write 0で完了した。scopeを変えずに再開した通常slot run 830はwatermark `2026-07-27T08:00:00Z`まで到達してPASSし、EURUSD terminal blockerは0へ戻った。USDJPYおよびETFをこの復旧full-refetchへ含めていない。

ETF 11系列は開始前Read APIでquality/freshness PASS、current blocker 0を確認した。本日最初のXNYS category slotは`2026-07-27T14:30:15Z`であり、それまでは`PENDING_SCHEDULED_SLOT`とする。

## R1-R6結果

| 要件 | 判定 | 結果 |
|---|---|---|
| R1 terminal retry storm抑止 | PASS_CODE | terminal keyをerror、対象系列、watermark revisionで固定し、状態変化まで再runを生成しない |
| R2 quality event scope | PASS_RUNTIME | immutable run scopeとsupersessionを適用し、EURUSD blockerはUSDJPYへ波及しない |
| R3 DataVersion復旧 | PARTIAL | EURUSDはACTIVEへ復旧。USDJPYはproviderの新DataVersion品質異常でrollback |
| R4 USDJPY scheduler契約 | QUARANTINED_PROVIDER | 基本universe定義は保持するが、active scopeはEURUSDだけを毎UTC時03分、deadline 10分で実行。USDJPYはprovider訂正版待ち |
| R5 gap分類 | PASS_ACCOUNTED | 両系列ともUNCLASSIFIED=0、blocking=0、duplicate=0、補間0 |
| R6 atomic Read API | PARTIAL | EURUSD合格。USDJPYはSTALE_DATA_VERSIONかつcurrent blocker 2件 |

## 2026-07-27 OAuth reconcile結果

Operator UIの固定OAuth reconcile job `db3-20260727T062212Z-2763176a`を実行した。各full-refetch前にrotating refresh credentialからaccess tokenを更新する経路は、1時間を超えるjobで正常に機能した。

- SPY、IWM、EFA、EEM、VNQ、SHY、IEF、TLT、TIP、LQD、GLD: full-refetch PASS
- EURUSD: run 822 PASS
  - old/new DataVersionのlineageとraw SHA-256を保持
  - inserted 75、updated 102,397、revision 102,404、rejected 9、removed 7
  - latest complete `2026-07-27T06:00:00Z`
- USDJPY: run 824 FAILED
  - error `FX_EXTREMA_QUARANTINE_ROW_LIMIT_EXCEEDED`
  - curated、derived、watermarkはrollback
  - raw chart 86 pageとrun manifestを保持
- Saxo write request、precheck、order: `0 / 0 / 0`

## USDJPY provider content-quality blocker

Saxo ChartのDataVersionは`29738065`から`29738069`へ変化した。公式Chart契約に従い全履歴を再取得したところ、新DataVersionのUSDJPY 1H rawにHigh/Low extremaの`Bid > Ask`が245 unique row存在した。

- High交差: 123
- Low交差: 122
- 期間: `2010-06-25T13:00:00Z`から`2026-07-01T20:00:00Z`
- 年別分布: 2010年から2026年に分散
- 差0.001 JPY: 164件
- 最大差: 0.294 JPY
- page間duplicate conflict: 0
- 245時刻のうち236時刻は旧DataVersionのcuratedでは交差0で正常

既存の凍結品質契約は、過去High/Low extremaだけを最大10 rowかつ全観測の0.01%以下に限り、値を修正せず隔離できる。245 rowは件数・率とも契約を超えるため、閾値を観測値へ合わせて緩和していない。旧DataVersionとの混在、swap、clamp、interpolation、forward fill、手動DELETEも行っていない。

raw evidenceは`data/acquisition/runs/20260727T070513Z-5786a3c7/`、監査runは824である。これはinterface/OAuth障害ではなく、provider履歴改訂に対するcontent-quality blockerである。

sanitized provider evidenceは`docs/usdjpy_provider_content_quality_evidence_20260727.md`と`manifests/fx_extrema_evidence/usdjpy_dv29738069_summary.json`へ保存した。86 chart pageすべてのsize・SHA-256を検証し、credential key 0、write request 0を確認した。

別件として、inclusive `Mode=UpTo`のpage境界81時刻で、次の古いpage末尾が同じOpenかつ異なるHigh/Low/Closeを返すことを確認した。これは245交差の集合には影響しないが、従来のmergeが完成側を部分形成側で上書きする実装不具合だった。rawを変更せず、request順のfirst-seen完成sampleを保持するよう修正し、回帰テストを追加した。

修正後のproduction normalizer/mergeによるoffline replayでもaccepted 102,317、rejected unique 245、最新形成中1、同一error codeを再現した。したがって境界merge不具合を直してもUSDJPY provider blockerは残る。

Saxoの[公式Chart仕様](https://www.developer.saxo/openapi/learn/chart)ではChart responseの`DataVersion`変更時に全sampleを無効化して再取得する必要があるため、旧DataVersionをcurrentへ残す方法は採用しない。各FxSpot sampleがBid/Ask OHLCを返す契約は[Chart endpoint reference](https://www.developer.saxo/openapi/referencedocs/chart/v3/charts/get__chart)で確認した。

## R5 gap分類

成果物:

- `manifests/fx_gap_classification/fx_gap_classification_manifest.json`
- `manifests/fx_gap_classification/fx_gap_classification.json`
- `manifests/fx_gap_classification/fx_gap_classification.csv`
- `manifests/fx_gap_classification/fx_gap_classification_summary.md`

gap classifierは完成足だけを対象にし、最新成功runより古いraw revisionをcurrent curated rejectionとして扱わない。providerの新DataVersionで消えた旧raw sampleは、最新成功full-refetchのlineageを根拠に`SAXO_RAW_NO_SAMPLE`へ分類する。

| Instrument | Missing | Blocking | Unclassified | Cause |
|---|---:|---:|---:|---|
| EURUSD | 576 | 0 | 0 | QUARANTINED_VALUE_ANOMALY=6、SAXO_RAW_NO_SAMPLE=570 |
| USDJPY | 405 | 0 | 0 | QUARANTINED_VALUE_ANOMALY=3、SAXO_RAW_NO_SAMPLE=402 |

missing値の生成・補間は0、curated duplicateは両系列0である。coverage WARNはcurrent freshness/content qualityと分離する。

## Read API current state

`GET /health`はPASS、database roleは`saxo_app_reader`、transactionはread-only、statement timeoutは30秒である。

| Gate | EURUSD | USDJPY |
|---|---|---|
| data status | ACTIVE | STALE_DATA_VERSION |
| DataVersion | 29738069 | 29738065 |
| latest complete | 2026-07-27T06:00:00Z | 2026-07-24T18:00:00Z |
| coverage | WARN | WARN |
| freshness | PASS | FAIL |
| quality | PASS | FAIL |
| current blockers | 0 | 2 |
| unknown blockers | 0 | 0 |
| eligibility | ELIGIBLE_WITH_WARNINGS | BLOCKED |

## schedulerと次の解除条件

schedulerは`STOPPED`、managed=falseのまま維持する。USDJPYを含むhourly slotや連続2 slot受入は開始しない。

解除条件は、Saxo側の新しいDataVersionでUSDJPYの交差が凍結品質契約内へ収まり、guard付きfull-refetchがPASSし、その後canonical通常runが連続2回PASSすることである。providerデータが変化していない状態でreconcileを連打しない。

## 検証結果

- unit / non-integration: `173 passed, 40 skipped`
- DMI1・migration integration: `10 passed`
- migration checksum: 0024、0025を含む全適用migrationがvalid
- Read API preflight: PASS
- DB4 sub-gate: PASS、artifact mismatch 0
- aggregate validator: FAIL（USDJPYのSTALE_DATA_VERSION 1件をfail-closedで反映）
- scheduler: STOPPED、managed=false
- Saxo write request / precheck / order: `0 / 0 / 0`

## rollbackと不変性

- migrationはforward-only
- database事故時だけ検証済みbackup run 48をrestore候補とする
- run 824の失敗transactionによりcurrent curated、derived、watermarkは変更されていない
- raw response、旧/新DataVersion、run manifest、quality event、scope evidenceは削除・上書きしない
- scheduler再開前にRead APIとwatermarkを再検証する
