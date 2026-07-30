# FX追加3通貨ペア 実装・運用結果

## 判定

2026-07-28に、ユーザー承認済みの研究用警告契約を実装し、
AUDUSD→USDCAD→USDCHFの順にlive全履歴onboardingを実行した。3ペアとも
raw→curated→Read API公開まで完了し、`AVAILABLE_WITH_WARNINGS`で利用できる。
値の補間、Bid/Ask入替、clamp、raw削除は行っていない。

| Pair | UIC / AssetType | Full-history run | DataVersion | Curated 1H | Latest complete | Publication | Quality / coverage / freshness |
|---|---|---:|---:|---:|---|---|---|
| AUDUSD | 4 / FxSpot | 893（freshness 895） | 29749260 | 102,776 | 2026-07-28 03:00 UTC | `PUBLISHED` | `WARN / WARN / PASS` |
| USDCAD | 38 / FxSpot | 896 | 29749380 | 102,754 | 2026-07-28 03:00 UTC | `PUBLISHED` | `WARN / WARN / PASS` |
| USDCHF | 39 / FxSpot | 897 | 29749380 | 102,754 | 2026-07-28 03:00 UTC | `PUBLISHED` | `WARN / WARN / PASS` |

この表のWARNは、未分類の品質FAILではない。既知のprovider anomalyと実取得可能な
履歴境界を、ユーザー承認済みの限定契約として保持した状態である。各ペアで異なる
完成1H（02:00 UTC、03:00 UTC）の通常更新2回を確認し、候補scheduler profileを
2026-07-28T04:08:03Zに有効化した。

## 警告契約

### AUDUSD

- provider rawのBid/Ask extrema crossing 14件をimmutable rawへ保持した。
- 対象期間は`2013-09-15T21:00:00Z`～`2020-04-19T21:00:00Z`、対象fieldはHigh/Lowである。
- 14行はcuratedから除外し、欠損として分類した。価格は補間・入替・clamp・修正していない。
- 件数、期間、field、content fingerprintの完全一致を例外適用条件とする。
- 件数増加、期間拡大、Open/Close異常、または別規則違反では自動許容せず、AUDUSDだけを再reviewする。
- provider表示開始は`2002-09-25T02:40:00Z`、実取得で確認したeffective coverage startは`2003-05-12T00:00:00Z`である。それ以前を合成しない。

### USDCAD / USDCHF

- provider表示開始は`2002-09-25T02:40:00Z`である。
- 安定したpaged 1H取得のeffective coverage startを`2010-06-18T00:00:00Z`に固定した。
- 2002～2010年を欠損補間、推定、または観測済みとして表示しない。
- 既存のbounded extrema quarantine各6件はrawに保持し、curatedから無補間で除外した。
- provider表示開始とeffective startの差はRead APIのcoverage limitationとして公開する。

## 実装

- `fx_research_candidates_v1.json` schema version 2で警告契約、承認者・日時、coverage境界、AUDUSD 14件のfingerprintを固定した。
- migration `0030`でpublication stateへconsumer availability、research policy、provider/effective start、limitation、warning JSON、承認auditを追加した。
- migration `0031`、`0032`で、ingest roleに品質gateが必要とするviewだけの狭いSELECT権限を追加した。任意table読取やwrite権限は追加していない。
- candidate onboardingは一般品質gateを維持し、AUDUSDだけexact-matchの承認例外を適用する。既知行もrawから消さず、audit eventを残す。
- Read APIは`components.publication`と`components.coverage_assessment`へ、警告、件数・期間、値未修正、provider/effective startを返す。
- scheduler readinessは、3ペアが同じ承認policy、`AVAILABLE_WITH_WARNINGS`、freshness PASS、blockerなし、通常更新2回である場合だけ合格する。

## Read API確認

`GET /health`は`PASS`、DB roleは`saxo_app_reader`、transactionはread-onlyである。
3ペアの`GET /api/v1/series-status`は次を返す。

- `availability_status=AVAILABLE_WITH_WARNINGS`
- `quality_status=WARN`、`coverage_status=WARN`、`freshness_status=PASS`
- `current_blockers=[]`、`unknown_blocker_count=0`
- `research_policy_id=fx_research_candidate_user_approved_warnings_v1`
- `values_modified=false`、`interpolation_performed=false`

`GET /api/v1/bars`は3ペアとも公開barを200で返す。候補scheduler起動後の最初の
04:06 UTC slotもinstrument単位で実行し、SLA、watermark、品質gateを確認した。

## 安全性と既存運用

- Saxo requestはGETだけで、write / precheck / orderは`0 / 0 / 0`である。
- 既存EURUSD+ETF11のschedule条件を維持したまま、候補3ペアだけを独立した
  `fx_research_candidates_hourly` slotとして追加した。
- USDJPYのprovider-quality quarantineを変更・再取得・解除していない。
- DataVersion warning-only契約と既存curated履歴を変更していない。
- onboarding中の認証・権限問題はinterface/operationalとして扱い、data-quality FAILへ読み替えていない。

## 有効化gateの実績

同時刻の再実行を回数に含めず、各ペアで02:00 UTCと03:00 UTCを受け入れた。
normal pass runはAUDUSD `904 / 913`、USDCAD `905 / 914`、USDCHF
`906 / 915`である。全3ペアが`PUBLISHED / consecutive_normal_passes=2`、
承認policy一致、freshness PASS、blocker 0となったため、profile
`all_except_usdjpy_with_fx_research_candidates_20260727`を有効化した。最初の定期
slotはAUDUSD run 916、USDCAD run 917、USDCHF run 918で、すべてSLA PASS、
watermark `2026-07-28T03:00:00Z`、order/precheck 0だった。
