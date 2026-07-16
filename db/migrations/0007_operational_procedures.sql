SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0007_operational_procedures.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

CREATE OR REPLACE PROCEDURE quality.acknowledge_event(
    p_quality_event_id BIGINT,
    p_operator_label TEXT,
    p_resolution_note TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF NULLIF(BTRIM(p_operator_label), '') IS NULL OR NULLIF(BTRIM(p_resolution_note), '') IS NULL THEN
        RAISE EXCEPTION 'operator label and resolution note are required';
    END IF;

    UPDATE quality.event
    SET status = 'ACKNOWLEDGED',
        resolved_by = BTRIM(p_operator_label),
        resolution_note = BTRIM(p_resolution_note)
    WHERE quality_event_id = p_quality_event_id
      AND status = 'OPEN';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'quality event is missing or not OPEN';
    END IF;
END
$$;

CREATE OR REPLACE PROCEDURE quality.resolve_event(
    p_quality_event_id BIGINT,
    p_operator_label TEXT,
    p_resolution_note TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF NULLIF(BTRIM(p_operator_label), '') IS NULL OR NULLIF(BTRIM(p_resolution_note), '') IS NULL THEN
        RAISE EXCEPTION 'operator label and resolution note are required';
    END IF;

    UPDATE quality.event
    SET status = 'RESOLVED',
        resolved_at_utc = clock_timestamp(),
        resolved_by = BTRIM(p_operator_label),
        resolution_note = BTRIM(p_resolution_note)
    WHERE quality_event_id = p_quality_event_id
      AND status IN ('OPEN', 'ACKNOWLEDGED');

    IF NOT FOUND THEN
        RAISE EXCEPTION 'quality event is missing or already RESOLVED';
    END IF;
END
$$;

CREATE OR REPLACE PROCEDURE ops.start_backup_run(
    p_database_name TEXT,
    p_relative_path TEXT,
    INOUT p_backup_run_id BIGINT DEFAULT NULL
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF p_database_name NOT IN ('saxo_market', 'saxo_research_v13', 'saxo_forward_v13') THEN
        RAISE EXCEPTION 'invalid backup database';
    END IF;
    IF NULLIF(BTRIM(p_relative_path), '') IS NULL
       OR p_relative_path ~ '^/'
       OR p_relative_path ~ '^[A-Za-z]:'
       OR p_relative_path ~ '(^|/)\.\.(/|$)' THEN
        RAISE EXCEPTION 'backup path must be repository-relative';
    END IF;

    INSERT INTO ops.backup_run (database_name, status, relative_path)
    VALUES (p_database_name, 'RUNNING', p_relative_path)
    RETURNING backup_run_id INTO p_backup_run_id;
END
$$;

CREATE OR REPLACE PROCEDURE ops.finish_backup_run(
    p_backup_run_id BIGINT,
    p_status TEXT,
    p_sha256 CHAR(64),
    p_size_bytes BIGINT,
    p_pg_restore_list_pass BOOLEAN,
    p_error_code TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF p_status NOT IN ('PASS', 'FAILED', 'BLOCKED') THEN
        RAISE EXCEPTION 'invalid terminal backup status';
    END IF;
    IF p_status = 'PASS' AND (
        p_sha256 IS NULL OR p_sha256 !~ '^[0-9a-f]{64}$'
        OR p_size_bytes IS NULL OR p_size_bytes < 0
        OR p_pg_restore_list_pass IS NOT TRUE
    ) THEN
        RAISE EXCEPTION 'PASS requires sha256, size, and pg_restore list validation';
    END IF;

    UPDATE ops.backup_run
    SET status = p_status,
        finished_at_utc = clock_timestamp(),
        sha256 = p_sha256,
        size_bytes = p_size_bytes,
        pg_restore_list_pass = p_pg_restore_list_pass,
        error_code = p_error_code
    WHERE backup_run_id = p_backup_run_id
      AND status = 'RUNNING';

    IF NOT FOUND THEN
        RAISE EXCEPTION 'backup run is missing or already terminal';
    END IF;
END
$$;

REVOKE ALL ON PROCEDURE quality.acknowledge_event(BIGINT, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON PROCEDURE quality.resolve_event(BIGINT, TEXT, TEXT) FROM PUBLIC;
REVOKE ALL ON PROCEDURE ops.start_backup_run(TEXT, TEXT, BIGINT) FROM PUBLIC;
REVOKE ALL ON PROCEDURE ops.finish_backup_run(BIGINT, TEXT, CHAR, BIGINT, BOOLEAN, TEXT) FROM PUBLIC;

GRANT EXECUTE ON PROCEDURE quality.acknowledge_event(BIGINT, TEXT, TEXT) TO saxo_ops_operator;
GRANT EXECUTE ON PROCEDURE quality.resolve_event(BIGINT, TEXT, TEXT) TO saxo_ops_operator;
GRANT EXECUTE ON PROCEDURE ops.start_backup_run(TEXT, TEXT, BIGINT) TO saxo_ops_operator;
GRANT EXECUTE ON PROCEDURE ops.finish_backup_run(BIGINT, TEXT, CHAR, BIGINT, BOOLEAN, TEXT) TO saxo_ops_operator;
