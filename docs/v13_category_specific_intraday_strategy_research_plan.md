# 1H・4H・1D分析に基づくカテゴリー別短期戦略研究計画 v13

作成日: 2026-07-16  
研究線: `v13categoryintraday`  
状態: **RESEARCH PLAN FROZEN / PERFORMANCE NOT EVALUATED**

## 1. 結論

先ほどのv12計画は見直す。今後は4カテゴリーへ同じトレンド判定を適用しない。2024-06-28までの1H・4H・1Dデータを使ったPhase RA0の市場特性分析から、次のPrimary候補を採用候補としてRT0へ送る。

| カテゴリー | Primary候補 | 主な売買対象 | 売買周期 | 現時点の判定 |
|---|---|---|---|---|
| 株式・REIT | 寄付き1時間の方向・5 ETF breadthを使う同日継続 | SPY | 1H | 弱い混合証拠。検証候補であり利益証明ではない |
| 債券・Credit | LQD–IEFのbeta調整済み相対ショック回帰 | LQD + IEF | 1H | ローカル証拠は4群で最も明瞭。ただし2レッグ実装ゲート必須 |
| Gold | 米国寄付きレンジ拡大＋終値位置のブレイク継続 | GLD | 1H | 寄付きボラティリティは高いが方向優位性は未証明 |
| FX | セッション別に標準化した1時間過伸びの平均回帰 | EURUSD、USDJPY | 1H | 緩やかな短期回帰。ペア別・時間帯別に単独合格が必要 |

`1H`は主シグナルと執行、`4H`は完成済み1Hから作る確認・除外フィルタ、`1D`は前日までのボラティリティ、tail、risk capだけに使う。1Dを方向エントリー・決済に使わない。

この表は「採用戦略」の確定ではない。Phase RA0はPnL、手数料、WFOを一度も計算していないため、収益性の結論はまだ存在しない。

## 2. 旧計画を見直す理由

旧v12は市場特性を十分に確定する前に、カテゴリーごとの保有期間と戦略工程を先に置いていた。これは手順が逆だった。v12の取得データと成果物は監査履歴として残すが、ST2以降のunlockは取り消し、v13で再開する。

中長期Buy & Holdは人間が判断する投資として研究対象外とする。ここで研究するalphaは、同日または数時間で閉じる短期アルゴリズムだけである。Buy & Holdは市場背景の参考値にはできるが、アルゴリズムの合格判定には使わない。

## 3. 使用データと先読み防止境界

- 開発に使用した最終時点: `2024-06-28T23:59:59Z`
- 既存の2024-06-29以後のデータ: `LOCKED_LEGACY_SHADOW`
- 既存の後半データを、後から「未見Holdout」と呼び替えない
- 最終確認: 最終仕様凍結後に、新しい完全24か月のforward期間を積み上げる
- ETF 1D: 分配・分割調整済みtotal-return系列
- ETF 1H: Saxo raw OHLC。overnight gapは日中リターンと分離
- FX: Bid/Ask mid。raw 4Hに交差Bid/Ask異常があるため、戦略用4Hは合格済み1Hから再構築する

この境界は、以前の試験結果や既知の2024–2026相場を新しい戦略選択に混ぜないためのものでもある。

## 4. Phase RA0の主要観測

### 4.1 株式・REIT

- SPYの1H自己相関は `-0.0295`、同一セッション内だけでは `+0.0282`。
- 5 ETFの同一セッション内自己相関はIWMを除き小幅プラスだが、絶対値は小さい。
- SPY寄付き1時間のopen-to-close平均は `+1.33 bps`、標準偏差は `45.71 bps`。
- 自動gap fadeを支持する証拠はない。gapと同日日中反転の相関は概ねゼロ。
- IWM–SPY相対ショックの次バー回帰は `+1.74 bps`だが、正の割合は `50.24%`で強くない。他の株式ペアは一貫しない。

したがって、全時間帯の単純トレンドではなく、寄付き1時間が終了して初めて利用できる方向、5 ETFのbreadth、寄付きrangeを条件にSPYだけを取引する仮説を検証する。複数ETFを同時売買して小口口座のコストを増やさない。VNQ等はbreadth説明系列であり、株式戦略の損失を別資産で補う目的ではない。

学術研究には、株式の日中リターンが時刻固有の構造を持ち得るとの報告がある。ただし、これは本データ上の寄付き戦略の利益を証明しないため、「全時間帯を一様に扱わない」というRT0の仮説源としてだけ扱う。参考: [Heston, Korajczyk and Sadka (2010)](https://onlinelibrary.wiley.com/doi/10.1111/j.1540-6261.2010.01573.x)。

### 4.2 債券・Credit

- LQD–IEF相対1Hリターンのlag-1自己相関は `-0.1360`、variance ratio 4は `0.8011`。
- 2 sigmaショック後の次バー平均回帰は `+9.74 bps`、正の割合は `56.49%`。
- TIP–IEFもlag-1 `-0.1671`、ショック後回帰 `+5.71 bps`、正の割合 `55.82%`。
- ETF gapの寄与はSHY `61.44%`、IEF `56.66%`、TLT `53.74%`と大きく、overnightと日中を混ぜない必要がある。

PrimaryはLQD–IEFのbeta調整済み相対ショック回帰とする。ただし2銘柄の一方をshortする必要がある。対象Saxo口座でshort、借株、最低単位、スプレッド、手数料が100万円口座に適合しなければ `BLOCKED_IMPLEMENTATION` とする。長期債ETFのlong-onlyへ置き換えて結果を救済しない。

### 4.3 Gold

- GLDの1H自己相関は `+0.0034`でほぼゼロ。
- variance ratioは4時間 `1.0258`、12時間 `1.0278`とわずかに1を上回る。
- 寄付き1時間の標準偏差は `45.15 bps`、中間時間帯は `22.22 bps`。

単なる1Hトレンドは支持されない。Primary候補は、高い寄付きrangeと終値位置が同方向に一致した日のみ、次の1Hから最大2–3時間のrange expansionを狙うルールとする。寄付きrangeが大きいだけで反射的に売買しない。商品市場の日中モメンタム研究は仮説源に限定し、本データの採用根拠はWFOで別に確認する。参考: [Gao, Han, Li and Zhou, Intraday Momentum in Commodity Markets](https://www.sciencedirect.com/science/article/abs/pii/S0275531919311328)。

### 4.4 FX

- EURUSD 1Hのlag-1は `-0.0143`、variance ratio 4は `0.8999`、12は `0.8280`。
- USDJPY 1Hのlag-1は `-0.0055`、variance ratio 4は `0.8662`、12は `0.7820`。
- 最大ボラティリティはLondon/New York overlapで、EURUSD `13.33 bps`、USDJPY `13.73 bps`。
- 4H raw FXには品質異常があるため、raw 4Hの有利な数字を戦略選択に使わない。

Primary候補は、07–12 UTCまたは13–16 UTCの完成済み1Hリターンを、ペア・UTCセッション別の過去分布で標準化し、過伸び方向と逆向きに次バーから入る平均回帰とする。完成済み4Hが強い継続状態ならfadeを除外する。最大3時間で閉じ、21–23 UTCに新規ポジションを作らず、rolloverを跨がない。このためpoint-in-time swap履歴に依存しない。

FX fixing等に関連する時間帯効果が存在し得るという研究は、時間帯を無視しない設計の参考にする。ただし本計画の平均回帰を証明するものではない。参考: [Evans, O'Neill and Rime, Conditions in the Foreign Exchange Market Around the Fix](https://onlinelibrary.wiley.com/doi/full/10.1111/jofi.13306)。

## 5. 各候補の共通執行原則

1. シグナルは完成済みバーだけで作る。
2. 最短でも次の1Hバーで約定する。同じバーのcloseで判定・約定しない。
3. 4Hは合格済み1Hから決定論的に再構築し、完成前の4Hを読まない。
4. 1Dは前日closeまで。方向エントリー・決済には使わない。
5. overnight/rolloverは持たない。
6. whole unit、最低注文額、bid/ask、手数料、slippage、short/borrowを100万円口座で再現する。
7. カテゴリー単独でFAILした戦略を、FX利益やportfolio weightで救済しない。

## 6. 改訂Phase計画

### Phase RA0 — 1H・4H・1D市場特性分析（完了）

39 frequency行、43 session行、11 gap行、8 relative-value行を作成した。シグナル、ポジション、PnL、parameter search、WFO、portfolio weightはゼロ。成果物完成は収益性PASSではない。

次に開くPhaseは `RT0` だけである。

### Phase RT0 — 戦略・コスト・試行回数の仕様凍結（次）

- 各カテゴリーPrimary 1本、Challenger最大1本に固定
- 売買状態遷移、signal timestamp、next-bar fill、exit、stopを固定
- family当たりparameter cellを最大12に固定
- 対象Saxo口座のinstrument、whole unit、shortability、borrow、手数料、spread/slippageをmanifest化
- 債券2レッグが実装不能なら、この時点でBLOCKED
- 戦略PnLを見る前に凍結し、hashを記録

### Phase RD1 — 共通日次台帳・データ会計

- 1H共通calendar、DST、欠損、completed barを監査
- 1Hから4Hを再構築しraw FX 4Hを排除
- 前日1D risk stateを時点整合
- ETF corporate actionとraw intraday gapを分離
- UTCセッション別bid/ask・slippage表を作成

### Phase RE1-E/B/G/F — 決定論的実装・先読み防止監査

順序は株式・REIT、債券・Credit、Gold、FX。各カテゴリーを別々に実装し、人工データでsignal、entry、exit、position、cost、PnL、欠損時の状態遷移を検証する。先のカテゴリーの成績で後のルールを変更しない。

### Phase RW1-E/B/G/F — カテゴリー別14-fold WFO

- outer test: 2021 Q1から2024 Q2までの14四半期
- expanding training: 各foldより前だけ
- 初回test前training: 最低3年
- inner selection: RT0で凍結した最大12 cellだけ
- embargo: 最大lookback + 最大holding以上
- outer OOSだけを1回連結して評価
- post-boundary dataは読まない

最低合格条件は、base cost後expectancy正、HAC Sharpe正、positive fold比率60%以上、cost 2倍でtotal return非負、entry 1-bar遅延で非負、正のgross PnLの50%超を単一foldに依存しない、独立trade 100以上、100万円whole-unit実装可能、である。HAC t値、deflated Sharpe、stationary bootstrap区間、総trial数も併記する。

### Phase RC0 — 単独採否

各カテゴリーを `PASS`、`REJECT`、`BLOCKED_DATA`、`BLOCKED_IMPLEMENTATION` のいずれかで閉じる。portfolioへ送れるのはPASSだけ。最低2カテゴリーがPASSしなければportfolio phaseは開かない。

### Phase RP0–RP2 — Portfolio risk allocation

単独PASS戦略だけを使い、risk allocation、相関、流動性、カテゴリー上限を検証する。これはalphaの救済ではない。FAIL/未実装カテゴリー分は現金とし、成功カテゴリーへ自動再配分しない。配分最適化はouter training内だけで行い、final forwardで再最適化しない。

### Phase RF0 — 最終仕様・DEMO shadow・新24か月forward

最終ルールをhash凍結し、Saxo DEMOでorder lifecycle、拒否、partial fill、reconnect、kill switchをshadow確認する。その後、新しい完全24か月forwardを積み上げる。Live移行は別の利用者承認事項であり、この計画の自動unlockにはしない。

## 7. 重要な解釈

- RA0から言えるのは「検証すべき戦略family」であり、「儲かる戦略」ではない。
- 債券・Creditの記述統計が最も強く見えるが、100万円口座の2レッグコストで消える可能性がある。
- 株式とGoldの証拠は弱い。WFOでFAILなら `NO_STRATEGY` を受け入れる。
- FXは2ペアをまとめて合格させず、ペア別に合格したものだけを後で組み合わせる。
- 1Dを外したのではなく、短期alphaと中長期risk regimeを混同しない役割に限定した。

## 8. 成果物

- 市場特性仕様: `config/v13_phase_ra0_market_characterization_spec.json`
- 改訂研究仕様: `config/v13_revised_strategy_research_plan.json`
- 集計manifest: `data/v13/ra0/phase_ra0_20260716_v1/manifest.json`
- カテゴリー解釈: `data/v13/ra0/phase_ra0_20260716_v1/category_interpretation.csv`
- 詳細CSV: 同ディレクトリの`frequency_metrics.csv`、`session_metrics.csv`、`etf_gap_metrics.csv`、`relative_value_metrics.csv`
