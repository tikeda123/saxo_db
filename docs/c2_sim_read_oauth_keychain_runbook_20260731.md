# C2 SIM Read 初回OAuth・自動更新runbook

対象日: 2026-07-31
目的: 毎回のaccess token手入力を廃止し、初回の明示OAuth後はSIM ReadをKeychain内refresh credentialで継続する。
安全境界: Saxo GET開始、DB receipt登録、scheduler変更、注文、precheck、取消、資金・口座操作はこのrunbookの初回準備には含めない。

## 結論

個人向けSaxo OAuthを完全な無人初回認証にはできない。Saxo公式は、通常のOAuthは初回に人間操作が必要であり、その後はrefresh tokenを定期更新してsessionを維持できるとしている。PKCEのrefresh応答では新しいaccess tokenとrefresh tokenが返り、新refresh tokenが旧refresh tokenを置き換える。

- [Saxo: How can I configure OAuth without manual steps?](https://openapi.help.saxo/hc/en-us/articles/4416637088017-How-can-I-configure-OAuth-without-manual-steps)
- [Saxo: Authorization Code Grant (PKCE)](https://www.developer.saxo/openapi/learn/oauth-authorization-code-grant-pkce)
- [Saxo: Security / refresh token rotation](https://www.developer.saxo/openapi/learn/security)
- [Saxo: tokenの無効化](https://openapi.help.saxo/hc/en-us/articles/4417696479761-How-can-I-invalidate-an-access-refresh-token)

## 人間が行う4段階

1. Saxo Developer PortalのSIM PKCE applicationが、redirect URI `http://localhost/saxo/oauth/callback`、trading disabledであることを確認する。AppKeyはtokenではないがrepositoryへ記録しない。
2. Operator UIを安全な単一ランチャーで更新・起動して`http://127.0.0.1:8765/`を開く。

   ```bash
   .venv/bin/python -m market_db.operator_ui_service restart --port 8765
   ```

   ランチャーは8765 listenerのrepo cwd、`market_db.operator_ui --port 8765` command、loopback `/health` identityをすべて照合する。同一processだけを終了して新UIへ引き継ぎ、別application・不明process・複数listenerは`BLOCKED_PORT_CONFLICT_UNKNOWN_PROCESS`として停止しない。process command本文はdiagnosticへ返さず、PID、cwd、一致判定、command hashだけを返す。DB3 schedulerとRead APIは停止しない。

   App Key未設定時は画面の「1. App Key設定」にだけpassword型入力欄を表示する。利用者が「安全に保存してOAuthを有効化」を押した時だけ、App Keyを専用service `com.tikeda.saxodb.oauth.sim.app-key`のmacOS Keychainへ保存する。App KeyはPKCE public client identifierだが、値は再表示・HTML・log・DB・Git・browser storageへ残さない。POSTはloopback、same-origin、CSRF、no-store固定である。保存後は同じprocess内で安全に再読込され、OAuthボタンが有効になるため再起動は不要である。

   既存OAuth Keychain entryがある一方でApp Keyだけが未設定の場合、Portalで必要な操作は1つだけである。[Saxo Application Management](https://www.developer.saxo/openapi/appmanagement)で以前使用したSIM PKCE applicationを開き、App Keyを1回コピーする。新しいapplicationの作成・既存設定の変更は行わない。コピー後は画面の入力欄へ貼り付け、保存ボタンを押す。Keychain書込みはこのブラウザ操作自体を利用者の明示指示として扱い、それ以外の自動保存経路は設けない。

3. UIの「2. C2 OAuth接続」で`C2用Saxo OAuth接続`を一度だけ選び、Saxo SIMへlogin/同意する。provider/gateが未決定でもこの認証は実行できる。macOS Keychainへの初回保存時はOSの承認表示に従う。token値は画面、argv、環境変数、repository、DB、log、browser storageへ入れない。
4. UIの「3. 初回SIM観測開始」で、`AUTH_READY`、認証方式`OAUTH_PKCE_KEYCHAIN_ROTATING_REFRESH`、kill switch `OFF`を確認する。「SIM限定・trading disabledであり、GET-only観測15件だけを開始する」のcheckboxを選び、「初回SIM観測を開始」を押す。この明示クリックだけが観測を開始できる。provider/gate未決定でも実行できるが、raw保存、receipt/DB登録、periodic、allocation/PnL評価、注文は行わない。

5. UIの「4. 後続provider / allocation・paper評価gate」で、2 provider roleと運用gateを確認する。画面には既存台帳の候補、推奨、未解決リスク、現在の保存状態が表示される。利用者が各「判断を記録」ボタンを押した時だけ、承認者・server生成UTC時刻・判断根拠と選択値を`.runtime/c2/`のruntime decision contractへowner-only・atomic writeする。未決定値を推測で埋めない。ここが未決定でも初回SIM観測は実行できるが、receipt登録、periodic、SIM allocation/PnL・paper evaluationは`DECISION_REQUIRED`のままにする。

   現在の推奨は、特定licensed providerの契約証拠がない2 roleを「保留」とし、account/quote receiptおよびprovider依存SLAを確認できない運用gateも「保留」とすることである。証拠が揃った場合だけ「証拠付きで承認」または「入力値で承認」を選ぶ。これは初回技術観測を止める判断ではない。

## 以後の自動処理

- access tokenは必要時にprocess memoryへ取得し、file/DB/browserへ保存しない。
- refresh credential、PKCE verifier、期限、SIM AppKey fingerprintだけをmacOS Keychainへ保存する。
- refresh前にrepo-local lockを取得し、Keychainの最新credentialを再読込する。refresh成功時は新refresh credentialをKeychainへ置換し、旧tokenを再利用しない。
- provider/gate未決定中もOAuth refresh chainだけは維持できる。この維持処理はtoken endpointに限定し、Saxo OpenAPI GET clientを生成しない。
- Operator UIの`refresh維持`は`OAUTH_REFRESH_ONLY` keeperの状態である。`RUNNING`でもC2 data periodicが動いていることを意味しない。
- 初回の成功したGET preflightで、raw AccountKey/ClientKeyではなくHMAC fingerprintだけをKeychainへbindingする。以後にaccountが変わればC2だけをfail-closedする。
- SIM endpoint固定、trading-disabledの人間確認、GET allow-list、write counter 0を重ねてRead-onlyを維持する。OAuth `scope`はSaxo PKCEでは未使用のため、application設定とruntime allow-listが権限境界である。

初回SIM観測の標準範囲はcapability 1 GET、accounts 1 GET、balances 1 GET、11 ETF detail 11 GET、atomic InfoPrice 1 GETの計15 GETである。最低限の形式、identity、quote集合を検査する。四半期リバランス用途では`Mid`、`Bid`、`Ask`の少なくとも一つが正値なら低頻度reference priceとして扱い、二方向Bid/Askやreal-timeを要求しない。`Indicative`と`DelayedByMinutes > 0`は正常な観測値である。`NoAccess`、`NoMarket`、`Pending`、閉場などSaxoが価格未提供を明示した場合は技術接続成功と価格利用不可を分け、初回観測を`PASS_WITH_WARNINGS`とし、通常監視・低頻度paper評価は日次終値fallbackへ切り替える。提供された価格の非正値、両sideがある場合のcrossed、identity不一致は引き続きFAILする。価格値とraw identifierは画面へ返さない。historical transactionは別のdate-bounded gateが承認されるまで0件。receipt登録はGET成功とは別の後続stepである。

## 失敗・停止・失効

| 状態 | 動作 |
|---|---|
| refresh期限切れ、401/403、rotation失敗 | `BLOCKED_INTERFACE_OPERATIONAL`または`AUTH_LOGIN_REQUIRED`。旧token、24H token、環境変数へfallbackしない |
| account binding不一致 | C2だけを停止し、Keychain bindingを自動上書きしない |
| `.runtime/c2/sim_read_disabled`が存在 | refreshと新規/継続C2操作を停止。既存evidenceは削除しない |
| local credential削除 | access leaseを破棄し、C2 OAuth/Binding Keychain entryだけを削除。明示操作時のみ |
| remote revoke | SaxoTraderGOのApplication Accessから対象applicationをRemoveする。既発行access tokenは短い有効期限まで残り得る |

kill switch解除、local credential削除、remote revoke後の再認証はいずれも人間の明示操作とする。認証失敗をdata-quality FAILとして記録しない。

## 移行・互換性

既存`EphemeralSIMReadSession`はGET検証coreとして残す。変更はtoken供給元だけで、UIの手動access token paste/1〜15分leaseはdeprecatedかつ拒否する。既存のraw/curated/receipt/DB schemaは変更しない。

今回の準備実施値: OAuth 0、Saxo API GET 0、Keychain書込み0、DB write/receipt登録0、scheduler変更0、order/precheck/cancel/account/fund mutation 0。
