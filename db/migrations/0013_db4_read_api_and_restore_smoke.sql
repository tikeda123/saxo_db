SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0013_db4_read_api_and_restore_smoke.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

CREATE OR REPLACE PROCEDURE ops.record_restore_smoke(
    p_backup_run_id BIGINT,
    p_status TEXT,
    p_error_code TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF p_status NOT IN ('PASS', 'FAILED', 'BLOCKED') THEN
        RAISE EXCEPTION 'invalid restore smoke status';
    END IF;
    IF p_status = 'PASS' AND p_error_code IS NOT NULL THEN
        RAISE EXCEPTION 'PASS restore smoke cannot have an error code';
    END IF;

    UPDATE ops.backup_run
    SET restore_smoke_tested_at_utc = clock_timestamp(),
        restore_smoke_test_status = p_status,
        error_code = CASE
            WHEN p_status = 'PASS' THEN error_code
            ELSE COALESCE(NULLIF(BTRIM(p_error_code), ''), 'RESTORE_SMOKE_FAILED')
        END
    WHERE backup_run_id = p_backup_run_id
      AND status = 'PASS'
      AND sha256 IS NOT NULL
      AND pg_restore_list_pass IS TRUE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'backup run is missing or not verified PASS';
    END IF;
END
$$;

REVOKE ALL ON PROCEDURE ops.record_restore_smoke(BIGINT, TEXT, TEXT) FROM PUBLIC;
GRANT EXECUTE ON PROCEDURE ops.record_restore_smoke(BIGINT, TEXT, TEXT) TO saxo_ops_operator;

GRANT USAGE ON SCHEMA catalog, curated, derived, analytics, ops, quality TO saxo_app_reader;
GRANT SELECT ON
    catalog.source_dataset,
    catalog.instrument,
    curated.market_bar,
    derived.market_bar_4h,
    derived.market_bar_1d_risk,
    ops.research_snapshot,
    analytics.v_data_inventory,
    analytics.v_data_coverage,
    analytics.v_data_freshness,
    analytics.v_data_lineage,
    ops.v_ingestion_status,
    quality.v_open_event,
    ops.v_storage_usage,
    ops.v_backup_status
TO saxo_app_reader;
