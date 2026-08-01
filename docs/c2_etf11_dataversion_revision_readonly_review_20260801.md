# C2 ETF11 DataVersion改訂 read-onlyレビュー

> 後続方針: 本文は補完禁止時点のimmutable review結果である。TIP/GLDの欠落事実は変わらないが、2026-08-01にC2専用の有界overlay候補として扱う新方針が承認された。正本は[`c2_etf11_bounded_imputation_design_20260801.md`](c2_etf11_bounded_imputation_design_20260801.md)。production migration/apply/backfillは未実施である。

## 技術要約

**現時点では11件ともapplyしない。** immutable raw、revision event、現行curatedをread-onlyで照合した結果、9銘柄は60分足の限定apply候補として品質条件を満たしたが、C2日次derivedまで同時に正常化できる状態ではない。全11銘柄で2026-07-31 19:30Zの最終スロットが現行正規化により未確定扱いとなり、日次足は6/7スロットの`WARN`になる。TIPとGLDには、これに加えて2026-07-29 13:30Z・14:30Zのprovider row欠落がある。

DataVersion変更自体に破損の兆候はない。各銘柄のoverlap 21行中20行は値が同一でDataVersionだけが変化し、残る1行は以前保存された形成中barが確定値へ変わったものだった。重複、時刻逆転、null、非正値、OHLC不整合、providerによる削除は0件で、取得証跡のSHA-256もすべて一致した。

## 主要な判定

| 判定層 | 結果 | 対象 |
|---|---:|---|
| immutable evidence整合 | PASS 11/11 | ETF11全件 |
| 60分curated限定apply候補 | 9/11 | SPY, IWM, EFA, EEM, VNQ, SHY, IEF, TLT, LQD |
| 60分curated適用保留 | 2/11 | TIP, GLD（7月29日2スロット欠落） |
| C2日次derived適用可能 | 0/11 | 7月31日最終スロット未確定のため全件保留 |
| 総合判断 | `DO_NOT_APPLY_YET` | DB applyは未実行 |

今回のcontent差分は、通常の「形成中barから確定barへの更新」と整合する。全件で`is_complete: false -> true`を含み、価格またはvolumeも同じ時刻で確定した。広範なhistorical rewrite、時刻削除、銘柄identity変更は観測されていない。

## 銘柄別証拠

| key | event | old -> new DataVersion | provider / overlap / new | content差分時刻とfield | 反復証拠 | 60m | C2 1D |
|---|---:|---|---|---|---:|---|---|
| SPY | 18 | 29751202 -> 29749571 | 48 / 21 / 27 | 7/28 13:30Z close, volume, complete | 6 | eligible | hold |
| IWM | 19 | 29752148 -> 29750537 | 48 / 21 / 27 | 7/28 13:30Z close, volume, complete | 6 | eligible | hold |
| EFA | 20 | 29752151 -> 29751157 | 48 / 21 / 27 | 7/28 13:30Z high, close, volume, complete | 5 | eligible | hold |
| EEM | 21 | 29752155 -> 29751157 | 47 / 21 / 26 | 7/28 14:30Z high, close, volume, complete | 5 | eligible | hold |
| VNQ | 22 | 29753236 -> 29752449 | 47 / 21 / 26 | 7/28 14:30Z close, volume, complete | 5 | eligible | hold |
| SHY | 51 | 29752511 -> 29755044 | 48 / 21 / 27 | 7/28 13:30Z close, volume, complete | 2 | eligible | hold |
| IEF | 53 | 29753231 -> 29759067 | 48 / 21 / 27 | 7/28 13:30Z close, volume, complete | 2 | eligible | hold |
| TLT | 55 | 29752511 -> 29750537 | 48 / 21 / 27 | 7/28 13:30Z close, volume, complete | 2 | eligible | hold |
| TIP | 57 | 29753233 -> 29759068 | 46 / 21 / 25 | 7/28 13:30Z close, volume, complete | 2 | blocked | hold |
| LQD | 59 | 29753233 -> 29759068 | 48 / 21 / 27 | 7/28 13:30Z volume, complete | 2 | eligible | hold |
| GLD | 23 | 29752711 -> 29749768 | 45 / 21 / 24 | 7/28 14:30Z close, volume, complete | 3 | blocked | hold |

各反復取得は同じprovider Chart content SHA-256となった。したがってTIP/GLDの7月29日欠落は単発のローカル読込み失敗ではなく、保持済みprovider responseに一貫して存在する。値の補間、forward fill、別銘柄からの代用は認めない。

## 対象データと定義

- 対象: SPY/IWM/EFA/EEM/VNQ/SHY/IEF/TLT/TIP/LQD/GLD
- grain: instrument x 60分UTC bar
- price basis: `native_ohlc`
- 比較baseline: 現在acceptedの`curated.market_bar`
- provider evidence: revision eventが参照するimmutable `chart_0001.json`
- 「content差分」: OHLC、Bid/Ask OHLC、volume、market state、完成状態のいずれかがaccepted rowと異なる同時刻row
- 「version-only」: contentは同一でDataVersionだけが異なる同時刻row
- 日次完成条件: XNYS regular sessionの期待7スロットがすべて`is_complete=true`

DataVersion値の大小は新旧判定に使用していない。Saxoが後から返したversion identityと、現在watermarkでacceptedされているidentityを比較した。

## 検証方法

再現コマンドは次で、Saxo GETもDB writeも行わない。

```bash
.venv/bin/python -m scripts.review_c2_etf11_revision
```

このコマンドは`ops.v_series_revision_availability`、revision event/step、`curated.market_bar`、session calendarをread-only transactionで読む。各eventのartifact hash、run manifestのartifact hash、raw JSONの正規化、timestamp uniqueness/order、OHLC正値・整合、regular-session slot coverage、既存rowとの差分、候補日次足のslot数を再計算する。

機械可読な凍結結果は[`../manifests/c2_etf11_dataversion_revision_readonly_review_20260801.json`](../manifests/c2_etf11_dataversion_revision_readonly_review_20260801.json)に保存した。

## 適用した場合の影響

applyを後日承認する場合も1銘柄ずつguarded bounded applyを使う。影響対象は当該instrumentの60分curated range、watermark DataVersion、対象instrumentだけの4H/1D derived、revision event状態である。他のETF、FX、USDJPY quarantineには触れない。

60分候補9銘柄では、比較window内の20 version-only row、1 finalized row、および24〜27 new rowが対象になる。TIP/GLDは欠落rangeが解消またはprovider limitationとして別途明示判定されるまでapply対象にしない。全11銘柄とも、最終regular-session barの完成判定を解消するまではC2日次公開の更新としてapplyしない。

## rollback可能性

bounded applyは1 transaction内でraw登録、curated限定置換、watermark更新、対象instrument derived再構築、event更新を行うため、途中失敗は全rollbackされる。旧rawとrevision evidenceは削除しない。

apply成功後に誤りが判明した場合、手動DELETEやwatermark直接UPDATEは使わない。旧・新のimmutable raw、revision event、apply run manifestを根拠に、対象instrumentだけをguard付き再構築する。したがってrollbackは可能だが、「任意のSQLで即座に戻す」のではなく監査付き再構築として実施する。

## 制限と次アクション

1. `merge_pages()`がpage末尾を常に未確定にする現行仕様と、取引終了後の日次close laneの完成判定を整合させる。Saxo値を変更せず、session closeと取得時刻に基づく完成判定を別レビュー・回帰テストする。
2. TIP/GLDの2026-07-29 13:30Z・14:30Zについて、限定read-only Saxo GETまたは次回DataVersion evidenceでprovider rowの存在を再確認する。欠損を補間しない。
3. 上記がPASSした後、再度このread-only reviewを実行し、11銘柄それぞれのapply対象rangeと期待derived row hashを固定する。
4. DB applyは別途明示承認後にのみ実施する。

USDJPYは本レビューの対象外で、取得・full-refetch・quarantine解除・状態変更は0件である。注文、precheck、cancel、口座操作も0件である。
