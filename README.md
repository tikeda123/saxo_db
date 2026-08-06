# saxo_db

Saxo OpenAPIと移管済みCSVから取得した市場データを、再現可能・追記可能・監査可能なPostgreSQLデータベースとして管理するローカルデータ基盤です。

このリポジトリの責務は、データの取得、原本保持、正規化、品質判定、派生足生成、履歴・由来管理、バックアップ、および読み取り専用での提供です。売買戦略、特徴量、シグナル、バックテスト、損益、WFO、Holdout評価、ポートフォリオ配分、発注は別プロジェクトの責務とします。

## 主な機能

- PostgreSQL 18上でraw、curated、derived、quality、operationsを分離管理
- localに配置した69個の移管CSVをimmutableな監査原本として登録・検証（public GitHubには非収録）
- clean Mac向けのGit管理synthetic CSV seedでmigration/import/Read API配線をoffline確認
- Saxo SIM OpenAPIからcanonical 13系列と研究候補FX 3系列の1Hデータを安全に増分取得
- raw revisionを保持しながらcanonical 1Hを更新
- 完成済み・品質PASSの1Hから4Hとリスク日足を生成
- ETF total-return日次系列をnative OHLCと区別して管理
- inventory、期間、鮮度、coverage、品質、lineage、取込run、容量、backupを参照
- TradingView Lightweight Chartsを用いた読み取り専用Web UI
- 商品の意味、価格系列の読み方、注意点、公式情報を確認できる商品・データ辞書
- ChatGPT/Codexのサブスクリプションから利用できる読み取り専用ローカルMCP
- 外部プロジェクト向けのlocalhost限定Read APIとParquet export
- 3データベースのbackup、restore smoke、retention運用

## プロジェクト境界

### このリポジトリで行うこと

1. 市場データと参照データを取得・保管する。
2. 原本、revision、canonical、派生データの関係を追跡する。
3. coverage、freshness、qualityをデータ利用可否とは分けて可視化する。
4. 読み取り専用API、Web UI、検証済みParquetで他プロジェクトへ提供する。
5. backup、restore、retention、migrationを監査可能に運用する。

### このリポジトリで行わないこと

- 売買ルール、特徴量、シグナル、予測モデルの実装
- PnL、コスト評価、WFO、Holdout、ポートフォリオ最適化
- Saxoへの注文、precheck、口座・ポジション操作
- 品質FAILを隠す補間、価格修正、手動DELETE（C2 ETF11の明示WARN付き有界overlayはraw/canonical非変更の例外契約）
- 外部ネットワークへのDBまたはRead APIの直接公開

戦略プロジェクトは本DBを変更せず、Read APIまたは検証済みParquetを入力として利用します。PostgreSQLへの直接接続は運用・保守用途に限定し、通常のデータ連携インターフェースにはしません。

## 管理しているデータ

| データ層 | 主な内容 | 時間軸 | 用途 |
|---|---|---|---|
| raw | Saxo response、移管CSV由来のrevision、参照観測 | 原本依存 | 監査・再構築 |
| curated | canonical market bar、ETF total-return | 1H、1D | 標準の利用入口 |
| derived | canonical 1Hから再生成したbar | 4H、1D risk | 統一ルールの派生足 |
| catalog | dataset、instrument、session calendar | メタデータ | 識別・取引時間判定 |
| quality | 品質event、状態、根拠 | メタデータ | guardrail・監査 |
| ops | ingestion run、watermark、source file、snapshot、backup | メタデータ | 運用・lineage |

native OHLCとETF total-returnは意味が異なるため、同一系列として扱いません。保存期間、行数、最新時刻、品質状態は固定値をREADMEへ転記せず、現在のDBから次で確認します。

```bash
.venv/bin/python -m market_db.inspect inventory
.venv/bin/python -m market_db.inspect coverage
.venv/bin/python -m market_db.inspect freshness
.venv/bin/python -m market_db.inspect quality --fail-on-alert
.venv/bin/python -m market_db.inspect lineage
```

`NOT_EVALUATED`はPASSではありません。calendarや鮮度判定の根拠が不足している状態を、そのまま保持します。

### C2 ETF11の現在状態

2026-08-04 15:13 UTCのread-only確認では、ETF11（SPY、IWM、EFA、EEM、VNQ、SHY、IEF、TLT、TIP、LQD、GLD）は全銘柄が`latest_session_date=2026-08-03`、次の期待sessionが`2026-08-04`、`freshness_status=STALE`、`update_status=UPDATE_REQUIRED`です。欠損銘柄は0で、全系列の最新日足自体は`AVAILABLE`です。これは確認時点で次sessionの日次closeが未到達であることを示す鮮度警告であり、品質破損を意味しません。固定値は運用中に変わるため、利用時は必ず`GET /api/v1/c2/daily-close-status`で再確認してください。

TIP/GLDには、2026-07-29 13:30Z・14:30Zの各2本だけをC2専用overlayで`IMPUTED_PREVIOUS_VALID`とした既知の警告が残ります。両銘柄は`quality_status=WARN`、`imputation_status=PASS_WITH_IMPUTATION_WARNING`で、他9銘柄は`quality_status=PASS`です。補完はraw/canonicalを変更せず、generic `/api/v1/bars`、total-return、official close、execution priceには混在させません。

同じ確認時点でRead APIはhealth `PASS`、role `saxo_app_reader`、transaction read-onlyです。scheduler processと認証は`PASS / AUTH_READY`ですが、SPY/IWM/EFA/SHY/IEF/TLT/TIP/LQDの当日第1 1H slotが`RETRY_EXHAUSTED:DATA_NOT_READY`となり、serviceはinstrument単位の`RUNNING_DEGRADED`です。EEM/VNQ/GLD、FX laneやRead APIまで全停止した状態ではありません。scopeは`all_except_usdjpy_with_fx_research_candidates_20260727`を維持し、USDJPYはprovider content-quality quarantineのため取得対象外のままです。根拠は[`C2 ETF11有界補完の実DB適用証跡`](manifests/c2_etf11_bounded_imputation_live_apply_20260801.json)と[`C2 ETF11有界補完仕様・結果`](docs/c2_etf11_bounded_imputation_design_20260801.md)を参照してください。

これはデータ基盤の利用可能性であり、Strategy Analysisの戦略、配分、PnL、WFO/Holdout、売買判断の合格を意味しません。本リポジトリは市場データと品質・lineage・Read APIを提供するだけで、注文、precheck、取消、口座・資金操作を行いません。過去のC2紙上トライアルやStrategy側の注文テストも、本リポジトリの機能または合格実績には含めません。コードの厳密なGit版は固定値をREADMEへ転記せず、`git rev-parse HEAD`と`git status --short --branch`で確認します。

## 構成

```text
Saxo SIM OpenAPI / immutable CSV
                |
                v
       raw revision + lineage
                |
                v
        canonical 1H market bar
                |
          +-----+-----+
          |           |
          v           v
     derived 4H   derived 1D risk
                |
                v
  Read API / Web UI / verified Parquet
                |
                v
    external analysis or strategy projects
```

物理データベースは次の3つです。

| DB | 役割 | 境界 |
|---|---|---|
| `saxo_market` | 更新可能な市場データ正本 | ingestとreaderをrole分離 |
| `saxo_research_v13` | cutoff以前の不変snapshot | default read-only |
| `saxo_forward_v13` | 既存v13計画用のappend-only領域 | gate前は一般readerへ非公開 |

## 利用インターフェース

| 目的 | 推奨インターフェース | 備考 |
|---|---|---|
| 別のローカルプロセスからOHLCを取得 | `GET /api/v1/bars` | 安定した外部利用契約、1H/4H/1D |
| 固定研究snapshotからOHLCを取得 | `GET /api/v1/snapshots/{snapshot_id}/bars` | snapshot 1の検証済み1H |
| データの在庫・品質・鮮度を確認 | `GET /api/v1/operations/*` | allow-list済みの8種類 |
| datasetとsnapshotを識別 | `GET /api/v1/manifests` | 再現性・lineage確認 |
| Strategy向け外部データ契約を確認 | `GET /api/v1/strategy-data/contracts` / `status` | current signal、official close、calendar、cash/quote/feeのavailabilityをfail-closed表示 |
| C2 SIM Read受領手順を確認 | [C2 SIM Read短命session・決定手順](docs/c2_sim_read_session_and_decision_flow_20260731.md) | 認証値を保存せずcapability・11 ETF reference/atomic quoteをreceipt候補化 |
| 人が期間・OHLC・品質を確認 | Web UI | localhost限定、完全read-only |
| 人またはAIが商品・系列の意味を確認 | Web UI / local MCP | 公式リンク付き、投資助言は対象外 |
| 大量データを受け渡す | `market_db.export_parquet` | SHA-256とread-backを検証 |
| DB運用者がterminalで確認 | `market_db.inspect` | 任意SQLを受け付けない |

外部プロジェクトとの正式な契約、response schema、Python例、エラー処理、ページ分割、total-return取得、Docker/ブラウザ制約は[Read APIインターフェース仕様](docs/read_api_interface.md)を参照してください。

## クイックスタート

### 初回セットアップ

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/create_local_db_secrets.py
docker compose -p saxo-market-data config
docker compose -p saxo-market-data up -d postgres
.venv/bin/python -m market_db.migrate all
```

secretは`.secrets/`にだけ保存し、Git、`.env`、terminal引数、ログへ出力しません。より詳しい初回構築、migration、障害対応は[データ管理・運用ランブック](docs/database_operations_runbook.md)を参照してください。

別のMacへ安全に再構築する場合は、旧MacのKeychain、token、`.runtime/`、DB volume・実データ、log、backupをcopyせず、[別Mac構築ランブック](docs/new_mac_setup_runbook.md)に従ってclean cloneから開始してください。

### PostgreSQLの起動と確認

```bash
docker compose -p saxo-market-data up -d postgres
docker compose -p saxo-market-data ps
.venv/bin/python -m market_db.migrate validate
```

PostgreSQLはhostの`127.0.0.1:54329`だけにbindされます。

### 別MacのCSV bootstrap

public repositoryへ既存69 CSV（160,403,659 bytes）の実市場データは追加していません。Saxo、Yahoo Finance、FRED由来値は再配布権を確認できず、Git LFSも権利問題を解消しないためです。正確な69 file一覧・size・row count・SHA-256は[`manifests/import_file_inventory.csv`](manifests/import_file_inventory.csv)、判定根拠は[別Mac用CSV bootstrap監査](docs/new_mac_csv_bootstrap_audit.md)を参照してください。

clean Macでschema→CSV import→Read APIの技術経路を確認する場合は、外部データを含まないGit管理の人工seed 3 CSV / 55行だけを使用します。

```bash
.venv/bin/python scripts/verify_bootstrap_seed.py
.venv/bin/python -m market_db.migrate all --through 0018
.venv/bin/python -m market_db.bootstrap_seed import
.venv/bin/python -m market_db.migrate apply
.venv/bin/python -m market_db.migrate validate
.venv/bin/python -m market_db.read_api_service start
.venv/bin/python -m market_db.read_api_preflight --format json
```

このseedは`SYNTHETIC_BOOTSTRAP_ONLY`で、実価格、current data、total-return品質、official close、研究・運用readinessを表しません。既存DBや正規DBへ混在させず、schedulerを起動しません。正規69 CSVを利用する場合は、権利確認済みbundleをpublic GitHub以外のアクセス制御済み経路でignored `data/import/`へ置き、同じくmigration 0018 → import → 0019以降の順で構築します。全手順は[別Mac構築ランブック](docs/new_mac_setup_runbook.md)を参照してください。

### Read APIとWeb UI

```bash
.venv/bin/python -m market_db.read_api_service start
.venv/bin/python -m market_db.read_api_service status --format json
.venv/bin/python -m market_db.read_api_preflight --format json
```

`start`はPostgreSQL healthy、port競合、process identityを確認して`127.0.0.1:8766`へ起動し、healthyな同一processへの再実行は冪等です。`preflight`は`/`、`/health`と必須parameterなしのroute probeだけを使い、市場・metadata rowを取得しません。外部consumerはデータ取得前にpreflightの`status=PASS`を保存してください。これはservice運用準備のPASSであり、coverage、freshness、quality、または戦略性能のPASSではありません。

- health: <http://127.0.0.1:8766/health>
- データ概要: <http://127.0.0.1:8766/ui/overview>
- データ在庫: <http://127.0.0.1:8766/ui/inventory>
- 商品・データ辞書: <http://127.0.0.1:8766/ui/catalog>
- 系列チャート: データ在庫から対象系列の「チャート」を選択
- 品質・鮮度: <http://127.0.0.1:8766/ui/quality>

品質画面はCURRENT/HISTORICAL/UNKNOWNを分離し、eventのlayer・足・price basisをcanonical 1Hと照合します。raw archiveに残る既知異常はCURRENTの監査証跡として保持されますが、scopeが異なるcanonical 1HをFAILにしません。UNKNOWNは常にfail-closedです。

Read APIはcurrent DB用の`saxo_app_reader` poolと、固定研究DB用の`v13_research_reader` poolを分離します。どちらもread-only、30秒statement timeoutで、最大接続数はそれぞれ5と3です。Saxo tokenは不要です。停止は`.venv/bin/python -m market_db.read_api_service stop`を使い、repoが記録した同一processだけを終了します。PID不一致や別processは停止しません。runtime state/logはGit管理外の`.runtime/read_api/`に保存されます。

商品辞書をAIから説明させる場合は、[商品・時系列データ説明MCP](docs/mcp_instrument_explanation.md)を参照してください。AIモデルはChatGPT/Codex側で動作し、ローカルMCPは`saxo_app_reader`による読み取り結果だけを提供します。OpenAI APIキーをこのリポジトリへ設定する必要はありません。

OHLC取得例:

```bash
curl --fail --get 'http://127.0.0.1:8766/api/v1/bars' \
  --data-urlencode 'instrument_key=iwm' \
  --data-urlencode 'layer=1h' \
  --data-urlencode 'start=2026-07-15T00:00:00Z' \
  --data-urlencode 'end=2026-07-16T00:00:00Z' \
  --data-urlencode 'limit=1000'
```

外部projectが取得前に系列の利用可否を確認する場合は、複数のoperations endpointをclient側で結合せず、atomic status endpointを使用します。

```bash
curl --fail --get 'http://127.0.0.1:8766/api/v1/series-status' \
  --data-urlencode 'instrument_key=iwm' \
  --data-urlencode 'layer=1h' \
  --data-urlencode 'price_basis=native_ohlc'
```

identity、coverage、freshness、quality、watermark、latest runは同じread-only repeatable-read snapshotから返されます。初期契約はcanonical 1Hだけを対象とし、`UNKNOWN` ERROR/CRITICALは必ず`BLOCKED`です。

固定研究snapshotからOHLCを取得する場合は、current `/api/v1/bars`ではなくsnapshot-bound endpointを使用します。

```bash
curl --fail --get 'http://127.0.0.1:8766/api/v1/snapshots/1/bars' \
  --data-urlencode 'instrument_key=spy' \
  --data-urlencode 'layer=1h' \
  --data-urlencode 'price_basis=native_ohlc' \
  --data-urlencode 'start=2024-06-28T13:00:00Z' \
  --data-urlencode 'end=2024-06-29T00:00:00Z' \
  --data-urlencode 'limit=100'
```

serverは`saxo_research_v13`を直接読み、snapshot ID、cutoff、manifest SHA-256、DB内件数・最大時刻を照合します。responseの`ordered_content_sha256`と`snapshot_sha256`を外部runの証跡に保存してください。4H/1D、未知snapshot、manifest不一致はcurrent DBへfallbackせず拒否します。

### Parquet export

```bash
.venv/bin/python -m market_db.export_parquet \
  --instrument-key iwm \
  --layer 1h \
  --start 2026-07-15T00:00:00Z \
  --end 2026-07-17T00:00:00Z \
  --output iwm_sample.parquet
```

出力先はGit管理外の`exports/parquet/`だけです。最大100,000行で、SHA-256とDuckDB read-back件数が一致した場合だけ成功します。Parquetは配布用snapshotであり、PostgreSQLの正本へ逆importしません。

## Saxoデータ更新

日常の定期更新にはSaxo SIM OAuth PKCEを使用します。初回だけユーザーがSaxoへloginし、refresh credentialをmacOS Keychainへ保存します。access tokenはscheduler processのメモリだけで保持し、refresh時に返る新しいrefresh tokenでKeychain値を置き換えます。token値はfile、`.env`、DB、manifest、log、ブラウザstorageへ保存しません。

C2 SIM Readも同じKeychain rotationを使用し、毎回のaccess token手入力は廃止しました。OAuth完了後は、provider/gateが未決定でも、利用者の明示クリックによりSIMの初回GET-only技術観測（15 GET）を実行できます。この観測はresponse形式・account/instrument identity・11 ETF quote整合性だけを確認し、raw保存、receipt/DB登録、periodic、allocation/PnL評価、注文へ進みません。provider、official close、total-return、SLA、distribution、fee、receipt acceptanceは後続の`SIM_ALLOCATION/PAPER_EVALUATION` gateであり、`LIVE_ORDER_ELIGIBILITY`は引き続き禁止です。SIM/AppKey/accountのbinding、GET allow-list、kill switch、失効時fail-closed、初回だけ必要な人間操作は[`C2 SIM Read 初回OAuth・自動更新runbook`](docs/c2_sim_read_oauth_keychain_runbook_20260731.md)を参照してください。

2026-07-31の通常取引時間内の再観測は`SUCCEEDED / PASS_WITH_WARNINGS`、GET 15件、write/DB/receipt/order 0件だった。ETF11のidentityは一致した一方、全件が`PriceType=NoAccess`でBid/Ask未提供だった。Saxo公式情報では、純SIMのdemo accountへ非FX market dataは提供されず、SaxoTraderのOpenAPI Accessでmarket-data免責に同意する操作もSIM自体には価格を追加しない。これはOAuth/App Key障害や価格値破損ではない。

C2は四半期リバランスの低頻度検証であり、ティック、リアルタイム、二方向Bid/Askを初回観測・通常監視・`SIM_ALLOCATION/PAPER_EVALUATION`の必須条件にしない。通常監視は1時間ごとの遅延`Indicative`価格、またはsaxo_db Read APIの正規日次終値を使う。`DelayedByMinutes > 0`と`PriceType=Indicative`は正常値として受け入れ、少なくとも`Mid`、`Bid`、`Ask`のどれか一つが正値なら低頻度reference priceとして扱う。`NoAccess`の場合は日次終値fallbackを選び、低頻度paper評価全体を停止しない。実約定確認が必要な将来段階だけは別contractで扱い、現在は対象外である。

この日次終値fallbackで進める限り、現在必要な利用者設定はない。Saxoの遅延InfoPriceも併用する場合だけ、公式手順どおりDeveloper Portalの`Apps > Live Applications`からSIMをLIVE accountへlinkし、LIVE側SaxoTraderの`Account/Settings > Other > OpenAPI Access`でmarket-data免責に同意する一つの設定フローが必要になる。今回、この外部設定・契約追加は実行していない。根拠と代替評価は[`C2 SIM ETF11 quote NoAccess調査結果`](docs/c2_sim_quote_noaccess_resolution_20260731.md)、機械可読方針は[`c2_low_frequency_price_policy_v1.json`](specs/c2_low_frequency_price_policy_v1.json)を参照してください。

日次fallbackはC2専用`GET /api/v1/c2/daily-close-status`で、ETF11全銘柄の`native_ohlc` actual terminal closeと鮮度を取得する。この`close`は低頻度reference priceであり、total return、primary-exchange official close、execution priceではない。60分足の短い内部欠落は最大2本だけ別overlayへ前値補完できるが、raw/canonicalを変更せず`AVAILABLE_WITH_IMPUTATION_WARNING`、元timestamp、理由、連続欠落数を返す。補完された1Hは`GET /api/v1/c2/hourly-overlay`で明示的に取得し、generic `/api/v1/bars`へ混在させない。規則と実DB適用結果は[`C2 ETF11有界補完仕様・結果`](docs/c2_etf11_bounded_imputation_design_20260801.md)を参照する。

Developer PortalのApplication ManagementでSIMアプリを作成し、PKCE用redirect URIを`http://localhost/saxo/oauth/callback`（portなし）として登録する。SaxoのPKCE登録ではportを指定せず、実行時callbackだけが`http://localhost:8765/saxo/oauth/callback`を使用する。AppKeyはOAuth client IDでありtokenではないが、repositoryへ固定せず実行環境から与える。

Operator UIの起動・コード更新は、ポート8765のlistenerをrepo cwd・起動command・`/health`で照合する単一ランチャーを使います。同一Operator UIだけを引き継ぎ、不明processは停止しません。DB3 schedulerには作用しません。

```bash
.venv/bin/python -m market_db.operator_ui_service restart --port 8765
```

<http://127.0.0.1:8765/>で「Saxo OAuth接続」を選択し、Saxo画面で初回認証する。`AUTH_READY`後に「定期更新を開始」を選択する。Operator UIからの汎用reconcileはreview-first policyで無効である。DataVersion warningはRead APIでreviewし、apply承認を別記録したeventだけを専用CLIで明示適用する。Keychain経路のaccess tokenはprocess memoryだけで使用し、画面・log・DB・fileへ表示／保存しない。

App Key未設定時はC2欄に`SIM_OAUTH_APP_KEY_NOT_SET`の日本語説明、Saxo Portalで確認するPKCE／redirect URI／trading disabled、画面内のApp Key設定欄が表示され、OAuthボタンは無効になる。利用者が「安全に保存してOAuthを有効化」を押した場合だけ、PKCE public client identifierをrefresh credentialとは別のmacOS Keychain entryへ保存する。値は再表示・HTML・log・DB・Git・browser storageへ残さず、保存成功後は同じUI processでOAuth設定を再読込するため再起動は不要である。削除・置換も別確認付きの明示操作だけで行う。

今後のDataVersion変更は`REVIEW_PENDING / AVAILABLE_WITH_REVISION_WARNING`として監査記録し、scheduler、対象instrument、category、serviceを自動停止しない。新versionのChart JSONと限定sample差分はrevision evidenceとして隔離し、明示review・applyまでcurrent curated、watermark、4H/1Dへ混在させない。reviewとapplyの固定手順は[`data_version_warning_review_policy_20260728.md`](docs/data_version_warning_review_policy_20260728.md)を参照する。

```bash
.venv/bin/python -m market_db.saxo_auth status --callback-port 8765
.venv/bin/python -m market_db.periodic_update schedule \
  --scope-profile all_except_usdjpy_with_fx_research_candidates_20260727
.venv/bin/python -m market_db.periodic_update_service start --callback-port 8765 \
  --scope-profile all_except_usdjpy_with_fx_research_candidates_20260727
.venv/bin/python -m market_db.periodic_update_service status
```

schedulerはdata jobがない時間もtoken期限を監視してrefresh chainを維持する。現在のscopeは`all_except_usdjpy_with_fx_research_candidates_20260727`で、ETF 11系列、EURUSD、研究候補FX 3系列（AUDUSD、USDCAD、USDCHF）を取得する。USDJPYはprovider content-quality blockerが解消するまで対象外である。scope正本は[`fx_research_candidate_scheduler_scope_v1.json`](specs/source_collection/fx_research_candidate_scheduler_scope_v1.json)、実行中の値は`.runtime/periodic_update/state.json`の`scheduler_scope`で確認する。各slotはinstrument laneで独立し、future DataVersion warningはlaneもサービスもdegradedへ変えない。

ETFはXNYS calendarの営業日・短縮日を使い、株式・REIT、債券・Credit、Goldの各instrument laneを各完成可能なregular 1Hのbar終了15秒後から取得する。第1barは10:30:15 ETに開始し、10:33 ETをdeadlineとする。加えてsession終了45分後にETF11各銘柄の独立`etf_daily_close` laneを実行し、最終regular-session 1Hと派生1Dの同一session到達を確認する。過去slotの`DATA_NOT_READY` retry枯渇は監査証跡として残すが、次のhourly/daily slotを永久停止しない。EURUSDはSBFX 24x5 calendarに従って毎UTC時03分に取得し、時10分をdeadlineとする。401はrefresh後1回再試行し、network／429／未確定barは有限回だけretryする。canonical watermarkやinstrument driftの実障害は系列単位、future DataVersion変更は非停止warningとして記録する。認証・timeout・429はinterface/operational、未確定barはdata-not-ready、値異常はdata qualityとして分離する。

FX 1Hの履歴coverage WARNは`python -m market_db.fx_gap_report`でexpected slotとcurated/rawを照合する。結果は`manifests/fx_gap_classification/`にJSON・CSV・Markdownで保存し、欠損値の補間、forward fill、別provider代替は行わない。`series-status`の`components.coverage_assessment`から同じ証跡pathを確認できる。

Developer Portalの24時間tokenと従来reconcile画面は手動fallbackとして残す。24時間token自体を永続保存・自動更新・Portal画面から自動採取しない。Macの停止やsleepがrefresh期限を超えた場合はWeb UIから再認証する。repo-local serviceは実装済みだが、LaunchAgentの自動installは行わない。

更新処理はSIMのGET allow-listだけを使用し、注文・precheckは送信しません。DataVersion変更は警告とimmutable evidenceを保存するだけで、自動reconcile、自動置換、自動full-refetchを行いません。既存のSPY/SHY/GLD `APPLIED`履歴と旧bounded policy文書は過去監査として保持し、future defaultは[`data_version_warning_review_policy_20260728.md`](docs/data_version_warning_review_policy_20260728.md)です。

total-returnは用途を分離します。固定期間研究contract `etf11_fixed_window_20260712_v1`は従来どおり11 ETF、2004-11-18〜2024-06-28、各4,935行です。一般研究contract `etf11_full_history_20260712_v1`は同じ正規sourceの共通履歴2004-11-18〜2026-07-10、各5,443行を公開し、WFO/Holdoutの境界はStrategy側manifestが日時queryで選択します。どちらも`legacy/current` namespaceや研究用途に不要なfreshnessをblocking条件にしません。current運用の定期取得は別契約であり、provider運用契約未確定のため`BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED`のままです。詳細は[固定期間total-return研究公開整合](docs/total_return_fixed_window_research_publication_20260729.md)、[Full-history共通研究公開](docs/total_return_full_history_research_publication_20260730.md)、[定期更新実装計画](docs/periodic_market_data_update_implementation_plan.md)を参照してください。

## 安全境界

- `data/import/`の69 CSVはimmutable。上書き、整形、削除をしない。
- `data/import/`の実市場CSVをGit add、Git LFS、release asset、public URLで配布しない。
- `bootstrap/seed/`は人工smoke専用で、正規DB・研究・schedulerへ使わない。
- 適用済みmigrationを変更せず、新しい番号のmigrationで修正する。
- `docker compose down -v`、volume削除、DB dropは明示承認なしに行わない。
- token、password、AccountKey、ClientKey、口座識別子を保存・表示しない。
- rawの異常値を補間、swap、clamp、手動DELETEで隠さない。
- Read APIとWeb UIはloopback限定のまま利用する。
- strategy projectにはDB writer権限を与えない。

## ディレクトリ

```text
compose.yaml              PostgreSQLサービス定義
db/migrations/            checksum付きforward migration
market_db/                取得・取込・派生・運用・API実装
tests/                    unit / database integration test
docs/                     仕様、運用、実装結果、interface文書
specs/                    機械可読な凍結仕様
manifests/                成果物hashとruntime evidence
bootstrap/seed/           Git管理の人工CSV smoke seed
data/import/              Git管理外のimmutable実市場入力
data/acquisition/         Git管理外の取得artifact
backups/postgres/         Git管理外のbackup
exports/parquet/          Git管理外の外部受渡し用export
```

## 検証

```bash
.venv/bin/python -m pytest
SAXO_DB_INTEGRATION=1 .venv/bin/python -m pytest
.venv/bin/python -m market_db.validate --phase db4
```

統合testをskipした実行は、実DB gateのPASSを意味しません。固定manifestのbaselineと現在DBの可変件数は区別して検証します。

## 現在の状態

- DB1: PostgreSQL、role、migration、運用view — PASS
- DB2: legacy import、lineage、research snapshot — PASS
- DB3: SIM増分取得、revision、calendar、watermark、4H/1D派生 — PASS
- DB4: Read API、backup/restore、retention、Parquet — PASS
- DMUI4: データ管理Web UI、TradingView Lightweight Charts — PASS
- DMI0: 外部consumerのquality event fail-closed判定 — PASS
- DMI1A: 安定identity、event scope/applicability、API contract 1.1 — PASS
- DMI1B: 旧22 eventの運用review、CURRENT 5 / HISTORICAL 17 / UNKNOWN 0 — PASS
- DMI2A: atomic series status — PASS
- DMI2B: snapshot-bound 1H read — PASS
- DMI3: stable total-return API — PASS
- DMI4: cursor・consumer contract kit — PASS
- DMI5: Read API lifecycle・non-data operational preflight — PASS
- DPU1: OAuth PKCE・Keychain rotation・定期更新service — 実装PASS / 2026-08-04 15:13 UTC時点は`AUTH_READY`・service `RUNNING_DEGRADED`
- DPU2: ETF11・EURUSD scheduler — scope有効 / SPY・IWM・EFA・SHY・IEF・TLT・TIP・LQDは当日第1 1Hの`DATA_NOT_READY`でinstrument単位degraded、USDJPYはprovider-quality quarantine
- FX研究候補: AUDUSD・USDCAD・USDCHF — `PUBLISHED / AVAILABLE_WITH_WARNINGS`、独立scheduler稼働中
- C2 ETF11日次close — 2026-08-04 15:13 UTC時点で11/11銘柄が2026-08-03まで到達、次session待ちで全系列`STALE / UPDATE_REQUIRED`、欠損銘柄0 / TIP・GLDのみ各2本の有界overlay警告
- DPU3: current total-return定期取得 — BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED
- TRR1: ETF11固定期間total-return研究契約 — PASS（EEMのみ既知warning、値修正0）
- C2外部データ契約surface — migration `0034`/`0035`適用済み / common calendarはwarning付き利用可 / 未確定sourceはfail-closed
- C2 SIM Read短命session — `AUTH_READY`、初回観測は`SUCCEEDED / PASS_WITH_WARNINGS` / 純SIMのETF quoteは`NoAccess`のため低頻度paper監視はRead API日次closeを使用

これらはデータ基盤の実装・運用ゲートです。戦略の優位性や収益性を証明するものではありません。旧計画に含まれるRT0以降の戦略文書は履歴資料として保持しますが、このリポジトリの現行スコープには含めません。

## 主要ドキュメント

- [別Mac構築ランブック](docs/new_mac_setup_runbook.md)
- [別Mac用CSV bootstrap監査](docs/new_mac_csv_bootstrap_audit.md)
- [外部プロジェクト向けRead APIインターフェース](docs/read_api_interface.md)
- [Strategy Analysis向け外部データ契約・引渡し](docs/strategy_external_data_contract_handoff_20260730.md)
- [C2 SIM Read短命session・provider／運用gate決定手順](docs/c2_sim_read_session_and_decision_flow_20260731.md)
- [C2 SIM Read認証 実行readiness結果](docs/c2_sim_read_auth_execution_readiness_20260731.md)
- [固定期間total-return研究公開整合](docs/total_return_fixed_window_research_publication_20260729.md)
- [FX追加3通貨ペアの実装計画](docs/fx_additional_pairs_implementation_plan_20260727.md)
- [FX追加3通貨ペアの事前調査](docs/fx_additional_pairs_preimplementation_investigation_20260727.md)
- [FX追加3通貨ペアの実装・運用結果](docs/fx_additional_pairs_implementation_result_20260727.md)
- [データ管理・運用ランブック](docs/database_operations_runbook.md)
- [データ管理Web UI仕様](docs/data_management_web_ui_spec.md)
- [データ管理Web UI実装結果](docs/data_management_web_ui_implementation_result.md)
- [商品・時系列データ説明MCP](docs/mcp_instrument_explanation.md)
- [データ管理インターフェース改善計画](docs/data_management_interface_improvement_plan.md)
- [DMI4 cursor・consumer contract実装結果](docs/dmi4_implementation_result.md)
- [DMI5 Read API運用準備 実装結果](docs/read_api_operational_readiness_implementation_result.md)
- [DMI5 Read API運用準備 実装計画](docs/read_api_operational_readiness_implementation_plan.md)
- [S6V5A向け定期更新 実装計画](docs/periodic_market_data_update_implementation_plan.md)
- [Read API OpenAPI契約](specs/read_api_v1_openapi.yaml)
- [DMI0/DMI1実装結果](docs/dmi1_implementation_result.md)
- [旧quality eventレビュー候補](docs/dmi1_legacy_event_review_candidates.md)
- [DB4実装結果](docs/db4_implementation_result.md)
- [データ取得ハンドオフ](docs/saxo_api_data_acquisition_handoff.md)
- [DB機械仕様](specs/v13_phase_db0_database_spec.json)
- [移管仕様](specs/saxo_db_import_spec.json)

フェーズ別の実装計画・結果・manifestは監査証跡として`docs/`、`specs/`、`manifests/`に保持します。日常利用ではREADME、Read APIインターフェース仕様、運用ランブックの順に参照してください。
