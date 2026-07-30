# ETF11 Full-history共通研究公開

## 結論

11 ETFの正規化adjusted total-returnを、Holdout専用ではない共通GET契約`etf11_full_history_20260712_v1`で公開する。endpointは既存の`GET /api/v1/total-return`であり、Strategy Analysisの実験manifestが`start`（inclusive）と`end`（exclusive）を指定する。

## 契約

- instrument: SPY、IWM、EFA、EEM、VNQ、SHY、IEF、TLT、TIP、LQD、GLD
- price basis: `etf_total_return`
- total-return definition: Yahoo adjusted closeを各系列の先頭で100に正規化。分配・splitはprovider調整値に含まれるため二重加算しない
- source dataset: `20260712T135236Z`
- common available history: 2004-11-18〜2026-07-10、各5,443行
- freshness: frozen research sourceには適用しない。live/shadowのcurrent freshnessとは別契約
- identity: source manifest SHA、銘柄別immutable source-file SHA、銘柄別full-history ordered content SHA
- quality: duplicate 0、null/nonpositive 0、UTC日付昇順。EEMの2008-10-13と2008-10-28は既知provider outlierを値変更なしで`WARN`保持

期間名、legacy/current namespace、公開時刻、catalog eligibility labelは研究公開のblocking gateにしない。Data/lineage/content identity、instrument/horizon、破損・欠損・重複・時刻順、未承認provider anomaly、total-return定義だけをfail-closed gateとする。

## 取得例

```bash
curl --fail --get 'http://127.0.0.1:8766/api/v1/total-return' \
  --data-urlencode 'instrument_key=SPY' \
  --data-urlencode 'start=2024-07-01T00:00:00Z' \
  --data-urlencode 'end=2026-07-01T00:00:00Z' \
  --data-urlencode 'usage_mode=full_history_research' \
  --data-urlencode 'research_contract_id=etf11_full_history_20260712_v1' \
  --data-urlencode 'limit=10000'
```

この範囲は各銘柄501行を返す。専用Holdout契約、期間秘匿、一回取得制限は設けない。consumerはresponseの`ordered_content_sha256`、`source.source_file_sha256`、`source.full_history_ordered_content_sha256`を実験manifestへ保存する。

## 不変性と安全境界

既存fixed-window契約、DB row、source CSV、fixed-window dataset、watermark、schedulerを変更しない。新規取得、外部接続、値補正、DB write、注文、precheck、口座操作は行わない。Read APIは既存のread-only DB contextと承認mappingを確認したうえで、repository内のhash固定sourceを返す。

## 2026-07-30検証結果

- contract audit: 11/11 `AVAILABLE`または`AVAILABLE_WITH_WARNINGS`、blocker 0
- common history: 各5,443行、duplicate 0、null/nonpositive 0、ordered time PASS
- Strategy指定範囲2024-07-01〜2026-06-30: 11/11各501行、先頭・末尾一致、GET 200
- Read API: `/health` PASS、role `saxo_app_reader`、transaction read-only `on`
- manifests: fixed-window契約とfull-history契約を同時公開
- fixed-window回帰: LQD 4,935行、2004-11-18〜2024-06-28のまま
- automated tests: 45 passed
- acquisition request、DB write、order、precheck: すべて0
