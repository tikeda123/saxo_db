# Import bundle

このディレクトリは`../saxo_api`から検証付きでコピーしたDB import候補です。CSVを手作業で編集しないでください。

CSVはSaxo/Yahoo Finance/FRED由来の実市場値を含み、public GitHubへは収録しません。`data/import/**/*.csv`のGit ignoreを解除せず、Git add、Git LFS、release asset、public URLで配布しないでください。clean Macのinterface smokeには外部データを含まない`../../bootstrap/seed/`を使用します。公開可否と正規bundle移送条件は[`../../docs/new_mac_csv_bootstrap_audit.md`](../../docs/new_mac_csv_bootstrap_audit.md)を参照してください。

- `intraday/normalized`: Saxo 1H/4H。4Hはraw archive扱いです。
- `daily/saxo_multi_asset`: 旧マルチアセットSaxo日次。
- `daily/saxo_etf_raw`: Saxo ETF raw日次。
- `daily/etf11_sources`: ETF total-returnとmacroの外部原本。
- `daily/curated_etf_total_return`: 調整済みETF統合日次。
- `analysis_baseline`: Phase RA0の再現照合値。価格barではありません。

ファイル単位の行数・size・SHA-256・origin相対pathは`../../manifests/import_file_inventory.csv`に記録します。
