# Synthetic bootstrap seed

このdirectoryのCSVは、clean Macでmigration、CSV import、Read APIの配線だけを確認するために作成した**人工データ**です。

- Saxo、Yahoo Finance、FREDその他の外部市場データを含みません。
- 実価格、official close、total return、取引可能性、研究品質を表しません。
- `market_db.bootstrap_seed`以外からproduction/研究DBへ投入しません。
- import後のdatabaseは`SYNTHETIC_BOOTSTRAP_ONLY`です。scheduler、Saxo取得、Strategy consumerへ接続しません。
- 正規データへ切り替える場合は、このseed DBへ追記せず、別の空database clusterを正規bundleから構築します。

`manifest.json`がfile size、row count、SHA-256と非市場データ宣言を固定します。
