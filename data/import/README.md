# Import bundle

このディレクトリは`../saxo_api`から検証付きでコピーしたDB import候補です。CSVを手作業で編集しないでください。

- `intraday/normalized`: Saxo 1H/4H。4Hはraw archive扱いです。
- `daily/saxo_multi_asset`: 旧マルチアセットSaxo日次。
- `daily/saxo_etf_raw`: Saxo ETF raw日次。
- `daily/etf11_sources`: ETF total-returnとmacroの外部原本。
- `daily/curated_etf_total_return`: 調整済みETF統合日次。
- `analysis_baseline`: Phase RA0の再現照合値。価格barではありません。

ファイル単位の行数・size・SHA-256・origin相対pathは`../../manifests/import_file_inventory.csv`に記録します。
