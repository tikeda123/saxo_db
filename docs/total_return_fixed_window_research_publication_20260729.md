# ETF11固定期間total-return研究公開整合 実装結果

## 1. 結論

固定期間研究contract `etf11_fixed_window_20260712_v1`を実装し、既存dataset `20260712T135236Z`の11 ETFを、データ行を変更せずRead APIから研究利用可能にした。対象期間は2004-11-18〜2024-06-28、各銘柄4,935行である。SPY、IWM、EFA、VNQ、SHY、IEF、TLT、TIP、LQD、GLDは`AVAILABLE`、EEMは既存provider outlier 2行を値変更なしで保持するため`AVAILABLE_WITH_WARNINGS`である。全銘柄の`current_blockers=[]`をread-only実DB監査で確認した。

これは固定した過去期間の研究可用性である。current運用、live/shadow、最新日までのfreshness、戦略性能、WFO合格、注文可否を意味しない。

## 2. 対象とデータ定義

- contract ID: `etf11_fixed_window_20260712_v1`
- source dataset ID: `20260712T135236Z`
- provider: Yahoo Finance chart endpoint
- dataset kind: `total_return`
- price basis: `etf_total_return`
- horizon: 1D（1,440分）
- 共通期間: 2004-11-18〜2024-06-28
- 各銘柄: 4,935行
- total-return定義: provider adjusted closeを100へ正規化。dividendとsplitは監査用に保持し、二重加算しない
- Saxo DataVersion: 非該当。total-returnはSaxo native OHLCではない
- revision identity: 銘柄別immutable Yahoo raw SHA、source manifest SHA、正規化CSV SHA、API ordered content SHA

正本identityは[機械可読contract](../specs/total_return_research_contract_v1.json)に固定した。source manifest SHAは`57377bcd...758`、正規化CSV SHAは`429c59d8...998`である。完全値はcontractおよびRead API responseで取得する。

## 3. 撤廃した制限

次はデータの内容同一性や固定期間研究の再現性を証明しないため、研究公開のBLOCK条件から外した。ただしresponse metadataからは削除しない。

| 撤廃した制限 | 新しい扱い | 理由 |
|---|---|---|
| `legacy` / `current` namespace | 非blocking metadata | dataset名ではなくprovider/content/lineageで同一性を判定する |
| current datasetだけを候補にする規則 | fixed contractを明示解決 | 既存の正規11銘柄datasetを用途に合わせて選ぶ |
| catalog `research_eligibility=development_cutoff_only` | 非blocking metadata | 固定期間contractの客観gateで独立判定する |
| dataset公開時刻 | 非blocking metadata | 固定期間データの内容と無関係 |
| 2024-06-28以後のfreshness | `NOT_APPLICABLE_FIXED_WINDOW` | WFO固定期間外の最新性を要求しない |
| native 1H seriesのfreshness | total-return固定期間gateから分離 | 別price basis・別horizonであり、日次total-returnの品質ではない |
| `eligibility=stored_complete`だけの包括warning | contract承認済みwarningへ限定 | 未承認WARNと既知・無修正のEEM warningを混同しない |

current運用のfreshness gateは撤廃していない。`usage_mode=current_operations`では従来どおり最新性を評価する。

## 4. 残した最小ゲート

固定期間研究でも次はfail-closedである。

1. provider revisionまたは内容identityの不一致
2. instrument、1D horizon、`etf_total_return` price basisの不一致
3. raw、source manifest、正規化content、source-file lineageの不整合
4. 固定期間の行数・開始日・終了日の不一致、欠損、重複、null、非正値、時刻順序異常
5. 未承認のprovider content anomaly
6. total-return定義が不明、または未調整価格をtotal-returnとする状態

EEMは品質規則を無効化していない。既存quality evidenceに記録済みのdaily total-return outlierだけを、値補間・削除・clampなしの`AVAILABLE_WITH_WARNINGS`として扱う。未知のWARN、件数増加、lineage不一致はBLOCKする。

## 5. 11銘柄への影響

| ETF | 固定期間 | 行数 | quality | Read API availability | blocking |
|---|---|---:|---|---|---|
| SPY | 2004-11-18〜2024-06-28 | 4,935 | PASS | AVAILABLE | なし |
| IWM | 同上 | 4,935 | PASS | AVAILABLE | なし |
| EFA | 同上 | 4,935 | PASS | AVAILABLE | なし |
| EEM | 同上 | 4,935 | PASS_WITH_WARNINGS | AVAILABLE_WITH_WARNINGS | なし。既知outlier 2行、値修正0 |
| VNQ | 同上 | 4,935 | PASS | AVAILABLE | なし |
| SHY | 同上 | 4,935 | PASS | AVAILABLE | なし |
| IEF | 同上 | 4,935 | PASS | AVAILABLE | なし |
| TLT | 同上 | 4,935 | PASS | AVAILABLE | なし |
| TIP | 同上 | 4,935 | PASS | AVAILABLE | なし |
| LQD | 同上 | 4,935 | PASS | AVAILABLE | なし |
| GLD | 同上 | 4,935 | PASS | AVAILABLE | なし |

current dataset `SIMTR_20260725T002419Z_9682c226`は株式・REIT 5銘柄だけであり、最新性もcurrent運用上は別途評価される。この状態を固定期間研究11銘柄のBLOCK理由には使用しない。current datasetへlegacy行を移動・複製・上書きしていない。

## 6. 実装

- `specs/total_return_research_contract_v1.json`: 固定window、identity、定義、11銘柄、許可warning、最小gate
- `market_db/total_return_contract.py`: contract・関連artifact SHAのfail-closed検証
- migration `0033`: `analytics.v_total_return_research_series`。readerへ元表の広い権限を与えず、集計済みlineage証跡だけを公開
- `GET /api/v1/total-return-status`: 銘柄別の固定期間availability、coverage、quality、lineage、非blocking metadata
- `GET /api/v1/total-return`: `usage_mode=fixed_window_research`と`research_contract_id`を追加。承認済みcontractのdatasetを解決し、ordered content SHAを返す
- `GET /api/v1/manifests`: `total_return_research_contracts`を追加
- `market_db.total_return_contract_audit`: 11銘柄read-only監査

migrationはviewとSELECT権限だけであり、raw、curated、watermark、derived、scheduler stateを更新しない。

## 7. 利用方法

まずstatusを確認する。

```bash
curl --fail --get 'http://127.0.0.1:8766/api/v1/total-return-status' \
  --data-urlencode 'instrument_key=SPY' \
  --data-urlencode 'research_contract_id=etf11_fixed_window_20260712_v1'
```

`availability_status`が`AVAILABLE`または承認済み`AVAILABLE_WITH_WARNINGS`、`coverage_status=PASS_LOCKED_WINDOW`、`current_blockers=[]`であることを確認する。次に値を取得する。

```bash
curl --fail --get 'http://127.0.0.1:8766/api/v1/total-return' \
  --data-urlencode 'instrument_key=SPY' \
  --data-urlencode 'start=2004-11-18T00:00:00Z' \
  --data-urlencode 'end=2024-06-29T00:00:00Z' \
  --data-urlencode 'usage_mode=fixed_window_research' \
  --data-urlencode 'research_contract_id=etf11_fixed_window_20260712_v1' \
  --data-urlencode 'limit=10000'
```

consumerは11銘柄で同一`research_contract_id`、`source_dataset_id`、期間、definition IDを保存し、銘柄ごとの`ordered_content_sha256`もrun manifestへ固定する。EEM warningを黙ってPASSへ変換しない。

## 8. 検証結果

- repository artifact hash validation: PASS
- migration 0001〜0032 checksum validation: PASS
- migration 0033 applied: PASS
- 11銘柄DB audit: PASS、blocked 0
- 各銘柄row count: 4,935
- duplicate: 全銘柄0
- null/nonpositive: 全銘柄0
- FAIL / NOT_EVALUATED rows: 全銘柄0
- normalized content SHA lineage: 全銘柄一致
- EEM WARN rows: 2、承認済みwarningと一致、値修正0
- Read API `/health`: PASS、role=`saxo_app_reader`、transaction read-only=`on`
- live `/api/v1/total-return-status`（LQD）: AVAILABLE、current blockers 0
- unit regression: 48 passed（contract、Read API、migration対象。EEM警告件数ドリフトのfail-closedを含む）
- full repository regression: 222 passed、45 skipped。sandboxでlocalhost bindが禁止された1件は、loopback許可条件で単独PASS
- database writes by audit: 0
- orders/prechecks/account operations: 0

## 9. MA1再実行条件

Strategy側はMA1の`current dataset only`、`freshness=PASS`、`development_cutoff_only代替禁止`を固定期間WFOのHard Gateから外し、次へ置き換える。

1. `/api/v1/manifests`からcontract `etf11_fixed_window_20260712_v1`を解決する。
2. 11銘柄の`/api/v1/total-return-status`が`AVAILABLE`または承認済み`AVAILABLE_WITH_WARNINGS`で、`current_blockers=[]`である。
3. 共通期間2004-11-18〜2024-06-28、各4,935行、同一source dataset、同一定義、同一lineage identityである。
4. `/api/v1/total-return`を`usage_mode=fixed_window_research`で取得し、ordered content SHAを保存する。
5. EEMの既知warningを研究artifactへ保持する。

この条件でMA1のデータ契約を再評価できる。WFO、Holdout、候補選定、PnL、注文は`saxo_db`の実装・受入対象外である。

## 10. Rollback

Read APIコードを旧版へ戻す場合も、既存データを削除しない。新endpointと固定契約解決を無効化し、migration `0033`を戻す必要がある場合は、`analytics.v_total_return_research_series`をdropするforward migrationを新番号で作る。適用済み0033を書き換えない。current dataset、legacy dataset、raw、curated、watermark、scheduler、USDJPY quarantineはそのまま保持する。
