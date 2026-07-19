# DMI0 / DMI1 実装結果

更新日: 2026-07-20 JST

総合状態: **DMI0 PASS / DMI1A PASS / DMI1B PASS / DMI2A NEXT**

## 実装済み

- 外部分析consumerは対象ERROR/CRITICAL eventのCURRENTとUNKNOWNをfail-closedで遮断する。
- scope/applicabilityが欠ける旧応答はUNKNOWNとなり、`BLOCKED_DATA_RECONCILIATION`を返す。
- migration 0015で追記専用の`quality.event_scope`と`quality.event_applicability_review`を追加した。
- `quality.v_event_status`と`quality.v_open_event`が安定identity、scope、applicability、`current_blocker`を公開する。
- inventory、coverage、freshness、barsへ既存列を壊さず安定identityを追加した。
- operations/bars responseは`api_version=1`、`contract_revision=1.1`、`generated_at_utc`を返す。
- `saxo_ops_operator`の固定procedure/CLIだけがscopeとreviewを追記でき、readerはbase tableを読めない。
- Web UIはCURRENT/UNKNOWNとreview済みHISTORICALを分離し、blockerを現在系列状態へ反映する。
- migration 0016/0017でrule policy、新規eventの同一transaction default、atomic runの後続PASSによる追記専用supersessionを追加した。
- consumerとWeb UIはlayer、horizon、price basisを照合し、raw archive eventをcanonical 1Hへ混入させない。

## Legacy reconciliation結果

適用前のOPEN ERROR/CRITICAL event 22件をevent単位で照合し、operator label `codex-dmi1b-20260720`でscope 22行、applicability review 22行を追記した。その後、instrumentなしのRUN event 2件はprice basisを限定しないscopeをさらに追記した。base `quality.event`は395,032件、digest `bc1d72a5ba8890ffc62e908b098d101134e84261c2376fe52e376d4ad36507e3`のまま不変である。

- `source_series_quality_gate` 5件: `SERIES / raw / CURRENT`。immutable raw archiveの既知異常として残す。
- `db3_atomic_run_gate` 17件: `RUN / curated / HISTORICAL`。各full-refetch PASSまたはall-13 normal PASSをsuperseding runとして記録した。
- UNKNOWN ERROR/CRITICAL: 0件。
- event-level CURRENT blocker: 5件。すべてraw 240分/1440分で、canonical 1H blocker: 0件。

詳細なevent→復旧run対応は[旧eventレビュー結果](dmi1_legacy_event_review_candidates.md)を参照する。DMI1 exit gateを満たしたため、次の実装対象はDMI2Aである。DMI2B–DMI4は引き続きLOCKEDとする。

## 検証結果

- migration 0015–0017: APPLIED / checksum VALID
- DMI0 consumer unit tests: 9 PASS
- DMI1 focused integration: 6 PASS
- live consumer gate: PASS、blocking 0、contract error 0、raw scope excluded 2件（対象5 ETF内）
- Web UI API: CURRENT 5、HISTORICAL 17、UNKNOWN 0、canonical blocker 0
- security smoke: `saxo_app_reader`はreview base tableをSELECT不可、status viewはSELECT可
- credential保存: なし
- Saxo注文/precheck/write request: 0

全体回帰とDB4 validatorの最終値はDMI1 manifestの検証証跡を正本とする。
