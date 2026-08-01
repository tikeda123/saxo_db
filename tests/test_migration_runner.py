from __future__ import annotations

import hashlib

import pytest

from market_db.migrate import MigrationError, list_migrations, migration_number, migration_sha256


def test_repository_migrations_are_declared_and_ordered():
    paths = list_migrations()
    assert [migration_number(path) for path in paths] == [f"{number:04d}" for number in range(1, 39)]
    assert all(migration_sha256(path) == hashlib.sha256(path.read_bytes()).hexdigest() for path in paths)


def test_revision_warning_migration_separates_warning_review_and_apply():
    migration = next(path for path in list_migrations() if migration_number(path) == "0029")
    sql = migration.read_text(encoding="utf-8")
    assert "AVAILABLE_WITH_REVISION_WARNING" in sql
    assert "PENDING_REVIEW" in sql
    assert "APPLY_APPROVED" in sql
    assert "review_data_version_revision" in sql
    assert "selected_data_status='ACTIVE'" in sql
    assert "data_version_revision_warning_v2" in sql


def test_fx_research_warning_migration_is_scoped_and_auditable():
    migration = next(path for path in list_migrations() if migration_number(path) == "0030")
    sql = migration.read_text(encoding="utf-8")
    assert "AVAILABLE_WITH_WARNINGS" in sql
    assert "fx_research_candidate_user_approved_warnings_v1" in sql
    assert "c4039ebdef6caadad6f70cdce3d5c909ed88cbc042e362ecd4e58ad42337196e" in sql
    assert "2010-06-18T00:00:00Z" in sql
    assert "interpolation',FALSE" in sql
    assert "p.consumer_availability_status" in sql


def test_candidate_gate_privilege_migration_is_read_only_and_narrow():
    migration = next(path for path in list_migrations() if migration_number(path) == "0031")
    sql = migration.read_text(encoding="utf-8")
    assert "GRANT USAGE ON SCHEMA analytics TO saxo_ingest" in sql
    assert "analytics.v_data_coverage,analytics.v_data_freshness" in sql
    assert "GRANT INSERT" not in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql

    event_migration = next(
        path for path in list_migrations() if migration_number(path) == "0032"
    )
    event_sql = event_migration.read_text(encoding="utf-8")
    assert "GRANT SELECT ON quality.v_open_event TO saxo_ingest" in event_sql
    assert "GRANT INSERT" not in event_sql
    assert "GRANT UPDATE" not in event_sql
    assert "GRANT DELETE" not in event_sql


def test_total_return_research_view_exposes_only_aggregated_lineage():
    migration = next(path for path in list_migrations() if migration_number(path) == "0033")
    sql = migration.read_text(encoding="utf-8")
    assert "analytics.v_total_return_research_series" in sql
    assert "WITH (security_barrier = true)" in sql
    assert "source_file_sha256_values" in sql
    assert "GRANT SELECT ON analytics.v_total_return_research_series" in sql
    assert "GRANT SELECT ON ops.source_file" not in sql
    assert "INSERT INTO curated" not in sql
    assert "UPDATE curated" not in sql
    assert "DELETE FROM curated" not in sql


def test_strategy_external_receipts_are_immutable_read_only_publication():
    migration = next(path for path in list_migrations() if migration_number(path) == "0034")
    sql = migration.read_text(encoding="utf-8")
    assert "catalog.strategy_external_data_contract" in sql
    assert "ops.strategy_external_data_receipt" in sql
    assert "analytics.v_strategy_external_data_contract_status" in sql
    assert "analytics.v_strategy_external_data_receipt" in sql
    assert "strategy external data receipts are immutable" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "GRANT SELECT ON analytics.v_strategy_external_data_contract_status" in sql
    assert "GRANT SELECT ON analytics.v_strategy_external_data_receipt" in sql
    assert "GRANT INSERT ON ops.strategy_external_data_receipt TO saxo_ingest" in sql
    assert "GRANT SELECT ON ops.strategy_external_data_receipt" not in sql
    assert "GRANT UPDATE" not in sql
    assert "GRANT DELETE" not in sql
    assert "BLOCKED_EXTERNAL_CONTRACT_SIGNAL_CURRENT" in sql
    assert "WHEN c.contract_state = 'BLOCKED_EXTERNAL_CONTRACT'" in sql

    status_fix = next(path for path in list_migrations() if migration_number(path) == "0035")
    fix_sql = status_fix.read_text(encoding="utf-8")
    assert "COALESCE(r.availability_state,c.availability_state)" in fix_sql
    assert "last_good" in fix_sql
    assert "INSERT INTO" not in fix_sql
    assert "UPDATE " not in fix_sql
    assert "DELETE FROM" not in fix_sql


def test_c2_imputation_overlay_is_bounded_immutable_and_explicit():
    migration = next(path for path in list_migrations() if migration_number(path) == "0036")
    sql = migration.read_text(encoding="utf-8")
    assert "derived.c2_market_bar_1h_imputation" in sql
    assert "IMPUTED_PREVIOUS_VALID" in sql
    assert "consecutive_gap_count BETWEEN 1 AND 2" in sql
    assert "volume IS NULL" in sql
    assert "official_close_claim IS FALSE" in sql
    assert "total_return_claim IS FALSE" in sql
    assert "execution_price_claim IS FALSE" in sql
    assert "C2 imputation evidence is immutable" in sql
    assert "BEFORE UPDATE OR DELETE" in sql
    assert "analytics.v_c2_market_bar_1h_overlay" in sql
    assert "analytics.v_c2_daily_close_with_imputation" in sql
    assert "actual_terminal_close_present" in sql
    assert "GRANT INSERT ON derived.c2_market_bar_1h_imputation TO saxo_ingest" in sql
    assert "GRANT SELECT ON derived.c2_market_bar_1h_imputation" not in sql

    latest = next(path for path in list_migrations() if migration_number(path) == "0037")
    latest_sql = latest.read_text(encoding="utf-8")
    assert "analytics.v_c2_daily_close_status_latest" in latest_sql
    assert "derived.market_bar_1d_risk" in latest_sql
    assert "NOT EXISTS" in latest_sql
    assert "C2_BOUNDED_IMPUTED_PREVIOUS_VALID" in latest_sql
    assert "GRANT SELECT ON analytics.v_c2_daily_close_status_latest" in latest_sql
    assert "INSERT INTO" not in latest_sql
    assert "UPDATE " not in latest_sql
    assert "DELETE FROM" not in latest_sql

    privilege = next(path for path in list_migrations() if migration_number(path) == "0038")
    privilege_sql = privilege.read_text(encoding="utf-8")
    assert "GRANT SELECT (" in privilege_sql
    assert "policy_id,instrument_id,time_utc,candidate_data_version" in privilege_sql
    assert "TO saxo_ingest" in privilege_sql
    assert "GRANT SELECT ON derived.c2_market_bar_1h_imputation" not in privilege_sql


def test_unknown_migration_is_rejected(tmp_path):
    (tmp_path / "0039_unknown.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="not declared"):
        list_migrations(tmp_path)


def test_invalid_migration_filename_is_not_selected(tmp_path):
    (tmp_path / "not_a_migration.sql").write_text("SELECT 1;", encoding="utf-8")
    with pytest.raises(MigrationError, match="invalid migration filename"):
        list_migrations(tmp_path)
