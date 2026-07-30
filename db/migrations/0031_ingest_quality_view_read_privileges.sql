SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0031_ingest_quality_view_read_privileges.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

-- The candidate post-commit gate is read-only but runs as the bounded ingest
-- role so the gate and commit share one operational identity.  Grant only the
-- two derived quality views it reads; do not grant base-table write privileges.
GRANT USAGE ON SCHEMA analytics TO saxo_ingest;
GRANT SELECT ON analytics.v_data_coverage,analytics.v_data_freshness
    TO saxo_ingest;
