# saxo_db

Saxo OpenAPIと移管済みCSVから取得した市場データを、再現可能・追記可能・監査可能なPostgreSQLデータベースとして管理するローカルデータ基盤です。

このリポジトリの責務は、データの取得、原本保持、正規化、品質判定、派生足生成、履歴・由来管理、バックアップ、および読み取り専用での提供です。売買戦略、特徴量、シグナル、バックテスト、損益、WFO、Holdout評価、ポートフォリオ配分、発注は別プロジェクトの責務とします。

## 主な機能

- PostgreSQL 18上でraw、curated、derived、quality、operationsを分離管理
- 69個の移管CSVをimmutableな監査原本として登録・検証
- Saxo SIM OpenAPIから13銘柄の1Hデータを安全に増分取得
- raw revisionを保持しながらcanonical 1Hを更新
- 完成済み・品質PASSの1Hから4Hとリスク日足を生成
- ETF total-return日次系列をnative OHLCと区別して管理
- inventory、期間、鮮度、coverage、品質、lineage、取込run、容量、backupを参照
- TradingView Lightweight Chartsを用いた読み取り専用Web UI
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
- 品質FAILを隠す補間、価格修正、手動DELETE
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
| 人が期間・OHLC・品質を確認 | Web UI | localhost限定、完全read-only |
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

### PostgreSQLの起動と確認

```bash
docker compose -p saxo-market-data up -d postgres
docker compose -p saxo-market-data ps
.venv/bin/python -m market_db.migrate validate
```

PostgreSQLはhostの`127.0.0.1:54329`だけにbindされます。

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
- 系列チャート: データ在庫から対象系列の「チャート」を選択
- 品質・鮮度: <http://127.0.0.1:8766/ui/quality>

品質画面はCURRENT/HISTORICAL/UNKNOWNを分離し、eventのlayer・足・price basisをcanonical 1Hと照合します。raw archiveに残る既知異常はCURRENTの監査証跡として保持されますが、scopeが異なるcanonical 1HをFAILにしません。UNKNOWNは常にfail-closedです。

Read APIはcurrent DB用の`saxo_app_reader` poolと、固定研究DB用の`v13_research_reader` poolを分離します。どちらもread-only、30秒statement timeoutで、最大接続数はそれぞれ5と3です。Saxo tokenは不要です。停止は`.venv/bin/python -m market_db.read_api_service stop`を使い、repoが記録した同一processだけを終了します。PID不一致や別processは停止しません。runtime state/logはGit管理外の`.runtime/read_api/`に保存されます。

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

Developer PortalのApplication ManagementでSIMアプリを作成し、PKCE用redirect URIを`http://localhost:8765/saxo/oauth/callback`として登録する。AppKeyはOAuth client IDでありtokenではないが、repositoryへ固定せず実行環境から与える。

```bash
export SAXO_OAUTH_APP_KEY='<SIM application key>'
.venv/bin/python -m market_db.operator_ui
```

<http://127.0.0.1:8765/>で「Saxo OAuth接続」を選択し、Saxo画面で初回認証する。`AUTH_READY`後に「定期更新を開始」を選択する。CLIでも同じ操作を行える。

```bash
.venv/bin/python -m market_db.saxo_auth status --callback-port 8765
.venv/bin/python -m market_db.periodic_update schedule
.venv/bin/python -m market_db.periodic_update_service start --callback-port 8765
.venv/bin/python -m market_db.periodic_update_service status
```

schedulerはdata jobがない時間もtoken期限を監視してrefresh chainを維持する。そのうえでXNYS calendarの営業日・短縮日をDBから読み、SPY、IWM、EFA、EEM、VNQの各完成可能なregular 1Hをbar終了15秒後から取得する。第1barは10:30:15 ETに開始し、10:33 ETをdeadlineとする。EURUSDは毎UTC時03分に取得する。定期runは6系列だけを原子的に更新し、401はrefresh後1回再試行、DataVersion変更は対象系列だけをguard付きfull-refetchして再実行する。未確定barは`DATA_NOT_READY`、認証・timeout・429はinterface/operational、値異常はdata qualityとして分離する。

Developer Portalの24時間tokenと従来reconcile画面は手動fallbackとして残す。24時間token自体を永続保存・自動更新・Portal画面から自動採取しない。Macの停止やsleepがrefresh期限を超えた場合はWeb UIから再認証する。repo-local serviceは実装済みだが、LaunchAgentの自動installは行わない。

更新処理はSIMのGET allow-listだけを使用し、注文・precheckは送信しません。DataVersion変更時は通常更新を止め、guard付きfull refetchでraw revisionを保持したまま対象銘柄を再構築します。

total-return定期取得はprovider contract未確定のため`BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED`であり、既存`development_cutoff_only` datasetをcurrentへ昇格しません。詳細は[定期更新実装計画](docs/periodic_market_data_update_implementation_plan.md)を参照してください。

## 安全境界

- `data/import/`の69 CSVはimmutable。上書き、整形、削除をしない。
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
data/import/              immutableな移管入力
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
- DPU1: OAuth PKCE・Keychain rotation・定期更新service — IMPLEMENTED / 初回OAuth実証待ち
- DPU2: S6V5A優先6系列scheduler — IMPLEMENTED / 取引日SLA実証待ち
- DPU3: current total-return定期取得 — BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED

これらはデータ基盤の実装・運用ゲートです。戦略の優位性や収益性を証明するものではありません。旧計画に含まれるRT0以降の戦略文書は履歴資料として保持しますが、このリポジトリの現行スコープには含めません。

## 主要ドキュメント

- [外部プロジェクト向けRead APIインターフェース](docs/read_api_interface.md)
- [データ管理・運用ランブック](docs/database_operations_runbook.md)
- [データ管理Web UI仕様](docs/data_management_web_ui_spec.md)
- [データ管理Web UI実装結果](docs/data_management_web_ui_implementation_result.md)
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
