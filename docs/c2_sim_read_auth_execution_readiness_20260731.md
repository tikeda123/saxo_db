# C2 SIM Read認証 実行readiness結果

確認時点: 2026-07-31T04:35:18Z
対象: `c2_saxo_sim_ephemeral_read_session_v1`
判定: **READY_FOR_SIM_OBSERVATION**（初回15 GETだけ明示開始可能）

## 現在状態

| 項目 | 状態 | 判定 |
|---|---|---|
| OAuth/SIM Read設定 | `AUTH_READY` | SIM OAuth技術設定・認証済み |
| 認証方式 | `OAUTH_PKCE_KEYCHAIN_ROTATING_REFRESH` | access token手入力を廃止 |
| operational gate | `BLOCKED_EXTERNAL_CONTRACT_OPERATIONAL_GATE_NOT_ACCEPTED` | 数値・許容方針未決定 |
| current total-return provider | `DECISION_REQUIRED` | 今回選定しない |
| official-close provider | `DECISION_REQUIRED` | 今回選定しない |
| OAuth接続 | `COMPLETE` | Keychain rotation方式。再接続不要 |
| initial SIM observation | `READY` / explicit click only | 15 GETだけ許可。provider/gateは非ブロッキング |
| Saxo API GET | 0 | 実行していない |
| receipt registration | 0 | 実行していない |

確認コマンド:

```bash
.venv/bin/python -m market_db.c2_sim_read_readiness status
```

このコマンドはOAuthを開始せず、Keychain/token値を出力せず、Saxo APIへ接続しない。

## 実装したローカルOAuth契約

Operator UIの`C2 SIM Read実行準備`欄には、未設定時だけApp Key入力と「安全に保存してOAuthを有効化」を表示する。利用者のクリック時だけApp Key専用Keychain entryへ保存し、同じprocess内でOAuth設定を再読込する。続いて初回だけ`C2用Saxo OAuth接続`を使用し、以後はKeychain内refresh credentialをrotationする。

- `GET /api/c2/sim-read/readiness`: 秘密値を含まないreadiness、許可GET計画、次のユーザー操作
- `POST /api/c2/sim-read/prepare` / `clear`: 公開しない（404）。access token pasteをrequest bodyとしても受け取らない
- `POST /api/c2/sim-read/observe`: loopback、same-origin、CSRF、no-store、正確なread-only確認文字列を要求する。利用者が画面のcheckboxと開始buttonを操作した時だけ15 GETを実行し、自動開始しない

machine-readable正本は`specs/c2_saxo_sim_oauth_keychain_v1.json`。旧`specs/c2_sim_read_operator_input_contract_v1.json`はcompatibility-onlyである。

checked-in operational gateは未承認だが、OAuth接続と初回SIM技術観測は独立して実施できる。C2のtoken入力欄と受付POSTは存在しない。`AUTH_READY`、SIM/trading-disabled確認、kill switch OFF、利用者の明示クリックが揃えば、provider/gate未承認でもallow-list 15 GETを1回実行できる。raw保存、receipt登録、periodic、allocation/PnL評価、注文は引き続きBLOCKEDにする。

## 最小のユーザー操作

1. Operator UIの「1. App Key設定」でSIM AppKeyを保存する。値は再表示されず、保存後の再起動は不要である。
2. Operator UIの「2. C2 OAuth接続」で初回認証する。provider/gate未決定でも実行可能で、ここではSaxo OpenAPI GETを行わない。
3. 画面の確認checkboxを選び、「初回SIM観測を開始」を押す。これはprovider/gate未決定でも実行できるが、15 GETの技術観測だけでありreceipt/DB登録は行わない。
4. 後続のSIM allocation/paper evaluationへ進む場合だけ、`specs/c2_external_operational_gate_decision_template_v1.json`を`.runtime/c2/operational_gate_decision.json`へコピーし、以下を入力する。`.runtime/`はGit管理外である。
   - accepted account base currency
   - quote最大age秒、atomic span秒、delay分、SIM delay許容、accepted PriceType
   - fee `UNKNOWN`のconsumer block/warning方針
   - issuer revision lookback、cash correction lookback、negative-event必須性
   - role別SLA秒、承認者、UTC承認時刻
5. provider decisionの2 roleを証拠付き`APPROVED`にし、validatorでprovider/gate両方を確認する。runtime fileがない場合は未決定templateが読まれ、allocation/PnL・paper evaluationのSTOPを維持するが、初回SIM観測は止めない。

詳しい初回/自動更新/kill switch/revoke手順は[`c2_sim_read_oauth_keychain_runbook_20260731.md`](c2_sim_read_oauth_keychain_runbook_20260731.md)を正本とする。

## 明示開始後の許可GET計画

| endpoint ID | 標準call数 | 内容 |
|---|---:|---|
| `session_capabilities` | 1 | data capability確認 |
| `accounts_me` | 1 | identifierをHMAC fingerprintへ変換 |
| `balances_me` | 1 | currency/decimalsのみ採用。残高額は保存しない |
| `instrument_detail` | 11 | canonical 11 ETF identity。tradability/quantity ruleは後続gateで評価 |
| `info_prices` | 1 | 11 UIC atomic snapshot |
| `historical_transactions` | 0 | 配当訂正gateとdate range決定後の独立run |

標準preflight/atomic observationは15 GET。write endpointはallow-listに存在しない。認証・permission・network失敗は`BLOCKED_INTERFACE_OPERATIONAL`であり、data-quality FAILへ変換しない。

## provider決定入力

`specs/c2_external_provider_decision_template_v1.json`を`.runtime/c2/provider_decision.json`へコピーして使用する。各roleについて、provider legal name、契約参照、license/redistribution、definition、11 ETF coverage、revision、SLA、lineage、content identity、承認者・UTC時刻を要求する。不足fieldがある`APPROVED`はvalidatorが拒否する。

## 検証結果

- C2 OAuth/readiness/UI/session/decision、既存receipt/Saxo client/periodic関連回帰: `126 passed`（広範な未commit成果物を再署名するmanifest attestation 1件は対象外）
- loopback/origin/CSRF/no-store: PASS
- 未承認gateでcredential保存0、client生成0、Saxo GET 0: PASS
- dummy token/account identifier/`TradableOn`/balance amountのresponse・receipt非露出: PASS
- DB監査: receipt 10、raw revision 2,623,747、raw reference 90,894、curated 789,713、ingestion 1,087で不変

## 今回の実装・再起動後のゼロ操作証跡

OAuth再接続0、初回SIM観測0、Saxo OpenAPI GET 0、DB write/receipt登録0、order/precheck/cancel0、fund/account mutation 0、scheduler変更0、refetch/backfill 0。Operator UIだけをrepo cwd・command・health照合付きランチャーで再起動した。`/health=PASS`、`SIM_OBSERVATION_START=READY`、観測状態`IDLE`、既存DB3 periodic状態`BLOCKED_STALE_PID`・scope・0注文カウンタが再起動前後で不変である。
