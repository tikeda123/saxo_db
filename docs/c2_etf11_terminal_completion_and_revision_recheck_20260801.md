# C2 ETF11 terminal bar完了判定・revision再レビュー結果

> 後続方針: 本文は補完禁止時点のread-only監査記録として保持する。2026-08-01にC2限定・raw/canonical非変更・最大2本・明示WARNのoverlay方針が承認された。現在の実装契約は[`c2_etf11_bounded_imputation_design_20260801.md`](c2_etf11_bounded_imputation_design_20260801.md)を正本とする。migration/backfillは未適用である。

## 技術要約

2026-07-31のXNYS最終1Hスロットは、検証済み市場calendarのsession closeとSaxo `DelayedByMinutes`を使えば、価格を補正せずcompletedと判定できる。実装した判定は、最終スロット開始時刻がcalendarと完全一致し、raw取得時刻が`session_close + DelayedByMinutes`以後の場合だけ`is_complete=true`へ変更する。calendar未検証、delay未提供、負のdelay、開始時刻不一致はすべて未確定のままfail-closedとする。

この判定をimmutable revision evidenceへ適用して11 ETFを再レビューした結果、SPY/IWM/EFA/EEM/VNQ/SHY/IEF/TLT/LQDの9銘柄は60m curatedとdaily derivedの両方で、明示承認後のinstrument限定guarded applyが可能と判定した。TIP/GLDは2026-07-29の13:30Z・14:30Zがprovider responseに存在せず、同じDataVersionで行った限定GETでも欠落を再確認したため、apply不可を維持する。11銘柄一括applyは不可である。

本作業ではDB書込み、revision apply、raw保存、scheduler変更、注文、precheck、USDJPY操作を行っていない。

## 9銘柄はdaily apply候補、TIP/GLDは欠落継続

| 判定 | 銘柄 | 2026-07-31 terminal slot | 2026-07-29欠落 | daily derived apply |
|---|---|---:|---:|---|
| eligible | SPY/IWM/EFA/EEM/VNQ/SHY/IEF/TLT/LQD | completed | 0 | instrument限定で可 |
| blocked | TIP/GLD | completed | 各2 | 不可 |

TIPとGLDについてはSaxo SIM Chartを`Mode=From`、`Count=10`で各1回だけGETした。両方ともtimestamp順序・一意性・OHLC正規化はPASSしたが、検証済みXNYS calendarが要求する7スロットのうち5スロットだけが存在した。

- TIP: DataVersion `29759068`、欠落`2026-07-29T13:30:00Z` / `2026-07-29T14:30:00Z`
- GLD: DataVersion `29749768`、欠落`2026-07-29T13:30:00Z` / `2026-07-29T14:30:00Z`

DataVersionは保存済みrevision evidenceと一致する。したがって、旧rawファイルの欠損、local pagination、時刻変換だけでは説明できず、現在の同一provider versionでも再現する限定的なprovider row欠落として扱う。

## 判定対象と定義

- 対象: `SPY, IWM, EFA, EEM, VNQ, SHY, IEF, TLT, TIP, LQD, GLD`
- grain: Saxo SIM `Etf`, horizon 60分, `native_ohlc`
- review window: 各revision eventに保存された2026-07-23または24〜2026-07-31の比較範囲
- terminal slot: session openから1時間刻みで生成される最後の開始時刻。XNYS通常日は19:30Z開始・20:00Z終了の30分stub
- daily PASS: calendar期待7スロットがすべて`is_complete=true`かつ、既存OHLC品質条件を満たすこと
- apply可: immutable evidence hash、DataVersion、時刻一意性、calendar coverage、内容差分、daily完成性が全てPASSしたinstrumentだけ

## completion判定方法

`market_db.normalize_bars.mark_terminal_session_bar_complete`は次の条件をANDで評価する。

1. 対象は`merge_pages`後の最新かつ未確定の1行だけ。
2. DB上の`catalog.session_calendar`が`VERIFIED`である。
3. 行の`time_utc`がsessionから計算した最終スロット開始時刻と一致する。
4. `DelayedByMinutes`が存在し、0以上である。
5. immutable raw rowの`retrieved_at_utc >= session_close_utc + DelayedByMinutes`である。

成立時に変更するのは`is_complete`だけである。Open/High/Low/Close、volume、timestamp、DataVersion、payload hash、artifact lineageは同一であることを単体テストで確認した。将来の通常incremental/full-refetchではEtfだけにこの判定を適用し、FxSpotおよびUSDJPYには適用しない。

## 再レビュー方法と堅牢性

1. 保存済みrevision event、step、manifest、Chart raw artifactのSHA-256を再検証した。
2. `saxo_ingest` transactionを`READ ONLY`にしてaccepted curated rowsとverified calendarを照合した。
3. terminal rowは保存済み取得時刻・delay・session closeだけで再判定した。
4. TIP/GLDだけをSaxo Chart GET各1回で再確認し、raw価格を保存・表示せずDataVersion、timestamp coverage、正規化、canonical response hashだけを保存した。
5. provider overlay後の各sessionについてexpected/completed slotを再計数し、daily derived品質をシミュレーションした。

SPY等9銘柄は7月28〜31日の影響sessionがすべて7/7スロットとなった。TIP/GLDだけは7月29日が5/7のためWARN相当であり、他日は7/7である。欠落を補間、隣接値で代用、sessionから削除、OHLC補正する処理は一切ない。

## 制約・不確実性

- 限定GETは2026-08-01時点の同一DataVersion応答を確認したもので、Saxoが将来新DataVersionで欠落を訂正する可能性は残る。
- 今回のeligibleはapply実行許可ではなく、技術レビュー結果である。実際のDB変更にはrevision eventごとの明示承認とguarded bounded applyが必要である。
- 11銘柄をatomicな同一datasetとして同時更新する要件がある場合、TIP/GLDがBLOCKEDのため全体は未完了である。
- DBのcurrent daily as-of、水位、derived rowは今回変更していない。

## 推奨する次アクション

1. 明示承認が得られた場合のみ、eligible 9銘柄を1銘柄ずつrevision event IDに固定してguarded bounded applyする。
2. 各apply直後に他instrumentのcurated/watermark/derived不変性とRead API daily statusを確認する。
3. TIP/GLDは現DataVersionのままapplyしない。新DataVersion検知またはprovider訂正根拠が出た時だけ同じread-only reviewを再実行する。

## 未解決事項

- C2 consumerが9銘柄の部分更新を許容するか、11銘柄同時更新まで待つかはStrategy側の実験契約判断である。
- TIP/GLDの2行欠落がprovider訂正対象か、市場データ仕様上の恒久的欠落かは、現在のSaxo応答だけでは確定できない。

## 証跡

- 機械可読再レビュー: `manifests/c2_etf11_dataversion_revision_readonly_recheck_20260801.json`
- TIP/GLD限定GET: `manifests/c2_tip_gld_20260729_readonly_probe_20260801.json`
- 再現スクリプト: `scripts/review_c2_etf11_revision.py`
- 限定probe: `scripts/probe_tip_gld_gap_readonly.py`
- completion実装: `market_db/normalize_bars.py`

安全カウンターは、Saxo GET 2、Saxo write 0、DB write 0、raw persistence 0、orders/prechecks 0、USDJPY touched=falseである。
