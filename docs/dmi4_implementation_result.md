# DMI4 Cursor・consumer contract kit 実装結果

更新日: 2026-07-20 JST

状態: **PASS**

## 実装

- `GET /api/v1/snapshots/{snapshot_id}/bars`に、snapshot SHA bound・query boundのopaque cursorを追加した。
- snapshot pageの順序キーを`time_utc + instrument_id + price_basis`の複合キーとして固定した。
- `GET /api/v1/total-return`に、source dataset・manifest SHA-256 state revision・query boundのcursorを追加した。
- currentの`GET /api/v1/bars`は従来どおりbounded time-window取得とし、cursorを必須にしていない。
- cursorはprocess-local HMAC-SHA256署名。API再起動後は署名secretが変わるため再利用できず、改変・query変更・state変更をfail-closedで拒否する。
- 全v1安定endpointの機械可読契約を`specs/read_api_v1_openapi.yaml`に固定した。
- consumer再利用用fixtureを`tests/fixtures/read_api_contract_v1/contract_cases.json`に固定し、複数page parityとfail-closed error codeを検証した。

## 実DB runtime evidence

2026-07-20 JSTにRead APIを再起動して実データで確認した。

| endpoint | page limit | page rows | direct rows | missing | duplicate | order reversal |
|---|---:|---:|---:|---:|---:|---:|
| `/api/v1/total-return` IWM 2024-06 | 3 | 19 | 19 | 0 | 0 | 0 |
| `/api/v1/snapshots/1/bars` SPY 2024-06-28 | 3 | 7 | 7 | 0 | 0 | 0 |

- total-return state revision: `57377bcd1ca13eaf5b9150ac77c2929efed3a177f9dcdd4bed6fb83ed8db2758`
- snapshot SHA: `c275d078dcdff418ff2d34eb8e2e38a8d790510881556341f6bafbf0c8b63d6b`
- total-return direct ordered digest: `b0f608bd25dbdc72cf24a466d2119fa3c2e57077f1c72bee3dce0da6855dd590`
- snapshot direct ordered digest: `0d5b1c9b1f804fa1a294322d15fa6c33e9f670880a7a2609ccff6d935fb2594b`
- tampered cursor: HTTP 400 `CURSOR_INVALID`
- query mismatch: HTTP 409 `CURSOR_QUERY_MISMATCH`
- state revision change: unit contract HTTP 409 `CURSOR_EXPIRED`

## Test evidence

- DMI4 cursor・OpenAPI・consumer fixture・既存Read API回帰: `27 passed`
- DMI3/DMI2B既存integrationはDMI4変更後も再実行対象とし、current bounded barsの互換性を維持した。
- OpenAPI compatibility artifact: `PASS`
- DB4 cumulative validator chain: `PASS`（migration、backup/restore、API、既存DMI1B〜DMI4 artifact chain）。

## Security / rollback

- bindは`127.0.0.1`、DB transactionはread-only、任意SQL・DB write route・Saxo write requestは0。
- access token、AccountKey、口座識別子は保存しない。
- rollbackはcursor query parameterを使わない旧routeへ戻せる。既存v1 field、snapshot、total-return rowsは削除しない。

## 次の扱い

DMI4をPASSとして確定する。consumer側はfixtureを契約回帰に取り込み、以後はデータ基盤の運用改善または別プロジェクトでの戦略分析を行う。`saxo_db`へ売買戦略・PnL・発注ロジックは追加しない。
