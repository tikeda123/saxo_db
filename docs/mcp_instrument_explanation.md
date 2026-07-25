# 商品・時系列データ説明MCP

## 目的

`saxo_db` が管理する商品と時系列データの意味を、ChatGPT/Codexから日本語で確認するための読み取り専用MCPです。AIモデルはChatGPT/Codex側で動作し、`saxo_db` は商品辞書とDBの現在状態だけをMCPで提供します。OpenAI APIキー、Saxo token、口座識別子はMCPへ渡しません。

## 提供内容

- `list_managed_instruments`: 管理対象商品の一覧
- `describe_instrument`: 商品内容、価格の意味、注意点、公式リンク
- `get_managed_series`: 管理中の足、期間、最新complete時刻、品質・鮮度
- `saxo-db://instrument-catalog`: 商品辞書resource
- `saxo-db://instruments/{instrument_key}`: 商品別resource
- `explain_saxo_db_series`: 初心者向け説明prompt

MCPは `saxo_app_reader` だけを使用し、PostgreSQL transactionをread-onlyで実行します。戦略、シグナル、PnL、ポジション、注文は対象外です。

## Codexでの利用

プロジェクトの [`.codex/config.toml`](../.codex/config.toml) にローカルSTDIOサーバーを登録しています。Codexアプリでこのプロジェクトを信頼済みにし、設定を読み直すために新しいタスクを開くかアプリを再起動します。その後、MCP一覧で `saxo_db` の接続を確認します。

質問例:

```text
saxo_db MCPを使って instrument_key=spy の商品内容、DB価格の意味、管理中の足と期間、最新時刻、品質状態を初心者向けの日本語で説明してください。公式情報リンクも示してください。投資助言はしないでください。
```

Data Consoleの「商品・データ辞書」または系列チャートの「定義」タブから、この質問文をコピーできます。

## 手動動作確認

```bash
.venv/bin/python -m market_db.mcp_server
```

このコマンドはSTDIO上でMCPクライアントからの接続を待ちます。通常の端末に人間向けログは出しません。停止は `Ctrl-C` です。
