SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0032_ingest_quality_event_view_read_privilege.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

-- Read-only candidate publication gate input.  No quality-event DML privilege
-- is added here.
GRANT SELECT ON quality.v_open_event TO saxo_ingest;
