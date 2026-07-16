SET LOCAL ROLE saxo_db_owner;

REVOKE ALL ON SCHEMA public FROM PUBLIC;

DO $$
DECLARE
    schema_name TEXT;
BEGIN
    FOREACH schema_name IN ARRAY ARRAY['catalog', 'ops', 'raw', 'staging', 'curated', 'derived', 'quality', 'analytics']
    LOOP
        IF EXISTS (SELECT 1 FROM pg_namespace WHERE nspname = schema_name) THEN
            EXECUTE format('REVOKE ALL ON SCHEMA %I FROM PUBLIC', schema_name);
            EXECUTE format('REVOKE ALL ON ALL TABLES IN SCHEMA %I FROM PUBLIC', schema_name);
            EXECUTE format('REVOKE ALL ON ALL SEQUENCES IN SCHEMA %I FROM PUBLIC', schema_name);
            EXECUTE format('REVOKE ALL ON ALL ROUTINES IN SCHEMA %I FROM PUBLIC', schema_name);
        END IF;
    END LOOP;

    IF current_database() = 'saxo_market' THEN
        GRANT USAGE ON SCHEMA catalog, ops, raw, staging, curated, derived, quality TO saxo_ingest;
        GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA catalog TO saxo_ingest;
        GRANT SELECT, INSERT, UPDATE ON ops.ingestion_run, ops.source_file, ops.watermark TO saxo_ingest;
        GRANT SELECT, INSERT ON raw.market_bar_revision TO saxo_ingest;
        GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA curated, derived, quality TO saxo_ingest;
        GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA catalog, ops, quality TO saxo_ingest;

        GRANT USAGE ON SCHEMA curated TO saxo_app_reader;
        GRANT SELECT ON ALL TABLES IN SCHEMA curated TO saxo_app_reader;

        GRANT USAGE ON SCHEMA ops, quality TO saxo_ops_operator;
    ELSIF current_database() = 'saxo_research_v13' THEN
        GRANT USAGE ON SCHEMA catalog, curated, derived, analytics, ops TO v13_research_reader;
        GRANT SELECT ON ALL TABLES IN SCHEMA catalog, curated, derived TO v13_research_reader;
        GRANT SELECT ON ops.research_snapshot TO v13_research_reader;
    ELSIF current_database() = 'saxo_forward_v13' THEN
        GRANT USAGE ON SCHEMA raw TO v13_forward_writer;
        GRANT EXECUTE ON PROCEDURE raw.append_forward_market_bar(
            BIGINT, BIGINT, BIGINT, SMALLINT, TIMESTAMPTZ, TEXT, JSONB
        ) TO v13_forward_writer;
    ELSE
        RAISE EXCEPTION 'unexpected migration database %', current_database();
    END IF;
END
$$;
