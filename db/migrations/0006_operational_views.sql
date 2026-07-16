SET LOCAL ROLE saxo_db_owner;

CREATE OR REPLACE VIEW analytics.v_data_inventory
WITH (security_barrier = true)
AS
WITH inventory_base AS (
    SELECT
        sf.source_dataset_id,
        r.instrument_id,
        i.symbol,
        i.category,
        'raw'::TEXT AS layer,
        r.price_basis,
        r.horizon_minutes,
        COUNT(*)::BIGINT AS row_count,
        MIN(r.time_utc) AS min_time_utc,
        MAX(r.time_utc) AS max_time_utc,
        MAX(r.time_utc) FILTER (WHERE r.is_complete) AS latest_complete_time_utc,
        'NOT_EVALUATED'::TEXT AS quality_status,
        MAX(r.ingestion_run_id) AS latest_ingestion_run_id,
        ds.expected_update_interval_seconds,
        ds.freshness_grace_seconds
    FROM raw.market_bar_revision r
    JOIN catalog.instrument i ON i.instrument_id = r.instrument_id
    JOIN ops.source_file sf ON sf.source_file_id = r.source_file_id
    JOIN catalog.source_dataset ds ON ds.source_dataset_id = sf.source_dataset_id
    GROUP BY sf.source_dataset_id, r.instrument_id, i.symbol, i.category,
             r.price_basis, r.horizon_minutes,
             ds.expected_update_interval_seconds, ds.freshness_grace_seconds

    UNION ALL

    SELECT
        sf.source_dataset_id,
        b.instrument_id,
        i.symbol,
        i.category,
        'curated'::TEXT AS layer,
        b.price_basis,
        b.horizon_minutes,
        COUNT(*)::BIGINT AS row_count,
        MIN(b.time_utc) AS min_time_utc,
        MAX(b.time_utc) AS max_time_utc,
        MAX(b.time_utc) FILTER (WHERE b.is_complete) AS latest_complete_time_utc,
        CASE
            WHEN BOOL_OR(b.quality_status = 'FAIL') THEN 'FAIL'
            WHEN BOOL_OR(b.quality_status = 'WARN') THEN 'WARN'
            WHEN BOOL_OR(b.quality_status = 'NOT_EVALUATED') THEN 'NOT_EVALUATED'
            ELSE 'PASS'
        END AS quality_status,
        MAX(b.latest_ingestion_run_id) AS latest_ingestion_run_id,
        ds.expected_update_interval_seconds,
        ds.freshness_grace_seconds
    FROM curated.market_bar b
    JOIN catalog.instrument i ON i.instrument_id = b.instrument_id
    LEFT JOIN raw.market_bar_revision r
      ON r.ingestion_run_id = b.latest_ingestion_run_id
     AND r.instrument_id = b.instrument_id
     AND r.horizon_minutes = b.horizon_minutes
     AND r.time_utc = b.time_utc
     AND r.price_basis = b.price_basis
    LEFT JOIN ops.source_file sf ON sf.source_file_id = r.source_file_id
    LEFT JOIN catalog.source_dataset ds ON ds.source_dataset_id = sf.source_dataset_id
    GROUP BY sf.source_dataset_id, b.instrument_id, i.symbol, i.category,
             b.price_basis, b.horizon_minutes,
             ds.expected_update_interval_seconds, ds.freshness_grace_seconds

    UNION ALL

    SELECT
        d.source_dataset_id,
        NULL::BIGINT AS instrument_id,
        d.ticker AS symbol,
        'equity_reit'::TEXT AS category,
        'curated'::TEXT AS layer,
        'etf_total_return'::TEXT AS price_basis,
        1440::SMALLINT AS horizon_minutes,
        COUNT(*)::BIGINT AS row_count,
        MIN(d.date::TIMESTAMP AT TIME ZONE 'UTC') AS min_time_utc,
        MAX(d.date::TIMESTAMP AT TIME ZONE 'UTC') AS max_time_utc,
        MAX(d.date::TIMESTAMP AT TIME ZONE 'UTC') AS latest_complete_time_utc,
        CASE
            WHEN BOOL_OR(d.quality_status = 'FAIL') THEN 'FAIL'
            WHEN BOOL_OR(d.quality_status = 'WARN') THEN 'WARN'
            WHEN BOOL_OR(d.quality_status = 'NOT_EVALUATED') THEN 'NOT_EVALUATED'
            ELSE 'PASS'
        END AS quality_status,
        NULL::BIGINT AS latest_ingestion_run_id,
        ds.expected_update_interval_seconds,
        ds.freshness_grace_seconds
    FROM curated.etf_total_return_daily d
    JOIN catalog.source_dataset ds ON ds.source_dataset_id = d.source_dataset_id
    GROUP BY d.source_dataset_id, d.ticker,
             ds.expected_update_interval_seconds, ds.freshness_grace_seconds

    UNION ALL

    SELECT
        NULL::TEXT AS source_dataset_id,
        d.instrument_id,
        i.symbol,
        i.category,
        'derived'::TEXT AS layer,
        d.price_basis,
        240::SMALLINT AS horizon_minutes,
        COUNT(*)::BIGINT AS row_count,
        MIN(d.time_utc) AS min_time_utc,
        MAX(d.time_utc) AS max_time_utc,
        MAX(d.time_utc) FILTER (WHERE d.is_complete) AS latest_complete_time_utc,
        CASE
            WHEN BOOL_OR(d.quality_status = 'FAIL') THEN 'FAIL'
            WHEN BOOL_OR(d.quality_status = 'WARN') THEN 'WARN'
            WHEN BOOL_OR(d.quality_status = 'NOT_EVALUATED') THEN 'NOT_EVALUATED'
            ELSE 'PASS'
        END AS quality_status,
        MAX(d.source_last_ingestion_run_id) AS latest_ingestion_run_id,
        NULL::BIGINT AS expected_update_interval_seconds,
        NULL::BIGINT AS freshness_grace_seconds
    FROM derived.market_bar_4h d
    JOIN catalog.instrument i ON i.instrument_id = d.instrument_id
    GROUP BY d.instrument_id, i.symbol, i.category, d.price_basis

    UNION ALL

    SELECT
        NULL::TEXT AS source_dataset_id,
        d.instrument_id,
        i.symbol,
        i.category,
        'derived'::TEXT AS layer,
        d.price_basis,
        1440::SMALLINT AS horizon_minutes,
        COUNT(*)::BIGINT AS row_count,
        MIN(d.session_date::TIMESTAMP AT TIME ZONE 'UTC') AS min_time_utc,
        MAX(d.session_date::TIMESTAMP AT TIME ZONE 'UTC') AS max_time_utc,
        MAX(d.session_date::TIMESTAMP AT TIME ZONE 'UTC') FILTER (WHERE d.is_complete) AS latest_complete_time_utc,
        CASE
            WHEN BOOL_OR(d.quality_status = 'FAIL') THEN 'FAIL'
            WHEN BOOL_OR(d.quality_status = 'WARN') THEN 'WARN'
            WHEN BOOL_OR(d.quality_status = 'NOT_EVALUATED') THEN 'NOT_EVALUATED'
            ELSE 'PASS'
        END AS quality_status,
        MAX(d.source_last_ingestion_run_id) AS latest_ingestion_run_id,
        NULL::BIGINT AS expected_update_interval_seconds,
        NULL::BIGINT AS freshness_grace_seconds
    FROM derived.market_bar_1d_risk d
    JOIN catalog.instrument i ON i.instrument_id = d.instrument_id
    GROUP BY d.instrument_id, i.symbol, i.category, d.price_basis
)
SELECT
    source_dataset_id,
    instrument_id,
    symbol,
    category,
    layer,
    price_basis,
    horizon_minutes,
    row_count,
    min_time_utc,
    max_time_utc,
    latest_complete_time_utc,
    quality_status,
    latest_ingestion_run_id,
    CASE
        WHEN latest_complete_time_utc IS NULL THEN NULL
        ELSE GREATEST(0, EXTRACT(EPOCH FROM (clock_timestamp() - latest_complete_time_utc))::BIGINT)
    END AS freshness_seconds,
    CASE
        WHEN latest_complete_time_utc IS NULL
          OR expected_update_interval_seconds IS NULL
          OR freshness_grace_seconds IS NULL THEN 'NOT_EVALUATED'
        WHEN clock_timestamp() <= latest_complete_time_utc
             + make_interval(secs => expected_update_interval_seconds + freshness_grace_seconds) THEN 'PASS'
        ELSE 'STALE'
    END AS freshness_status
FROM inventory_base;

CREATE OR REPLACE VIEW analytics.v_data_coverage
WITH (security_barrier = true)
AS
SELECT
    source_dataset_id,
    instrument_id,
    symbol,
    horizon_minutes,
    NULL::BIGINT AS expected_rows,
    row_count AS actual_rows,
    NULL::BIGINT AS complete_rows,
    NULL::BIGINT AS incomplete_rows,
    0::BIGINT AS duplicate_rows,
    NULL::BIGINT AS missing_rows,
    'NOT_EVALUATED'::TEXT AS coverage_status
FROM analytics.v_data_inventory;

CREATE OR REPLACE VIEW analytics.v_data_lineage
WITH (security_barrier = true)
AS
WITH raw_counts AS (
    SELECT source_file_id, COUNT(*)::BIGINT AS raw_rows
    FROM raw.market_bar_revision
    GROUP BY source_file_id
), curated_counts AS (
    SELECT latest_ingestion_run_id AS ingestion_run_id, COUNT(*)::BIGINT AS curated_rows
    FROM curated.market_bar
    GROUP BY latest_ingestion_run_id
)
SELECT
    sf.source_dataset_id,
    sf.source_file_id,
    sf.relative_path,
    sf.ingestion_run_id,
    COALESCE(r.raw_rows, 0) AS raw_rows,
    COALESCE(c.curated_rows, 0) AS curated_rows,
    NULL::BIGINT AS derived_rows
FROM ops.source_file sf
LEFT JOIN raw_counts r ON r.source_file_id = sf.source_file_id
LEFT JOIN curated_counts c ON c.ingestion_run_id = sf.ingestion_run_id;

CREATE OR REPLACE VIEW ops.v_ingestion_status
WITH (security_barrier = true)
AS
SELECT
    r.ingestion_run_id,
    r.status,
    r.started_at_utc,
    r.finished_at_utc,
    r.inserted_rows,
    r.updated_rows,
    r.revision_rows,
    r.rejected_rows,
    r.error_code,
    (SELECT MAX(w.latest_complete_time_utc) FROM ops.watermark w) AS latest_complete_time_utc,
    CASE
        WHEN (SELECT MAX(w.latest_complete_time_utc) FROM ops.watermark w) IS NULL THEN NULL
        ELSE GREATEST(0, EXTRACT(EPOCH FROM (
            clock_timestamp() - (SELECT MAX(w.latest_complete_time_utc) FROM ops.watermark w)
        ))::BIGINT)
    END AS freshness_seconds
FROM ops.ingestion_run r;

CREATE OR REPLACE VIEW quality.v_open_event
WITH (security_barrier = true)
AS
SELECT
    quality_event_id,
    status,
    severity,
    rule_id,
    instrument_id,
    time_utc,
    action,
    created_at_utc
FROM quality.event
WHERE status IN ('OPEN', 'ACKNOWLEDGED');

CREATE OR REPLACE VIEW ops.v_storage_usage
WITH (security_barrier = true)
AS
SELECT
    current_database()::TEXT AS database_name,
    n.nspname::TEXT AS schema_name,
    c.relname::TEXT AS relation_name,
    pg_total_relation_size(c.oid)::BIGINT AS size_bytes,
    CASE
        WHEN pg_total_relation_size(c.oid) >= 8589934592 THEN 'REVIEW'
        ELSE 'BELOW_THRESHOLD'
    END::TEXT AS partition_review_threshold_status
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind IN ('r', 'p', 'm')
  AND n.nspname IN ('catalog', 'ops', 'raw', 'staging', 'curated', 'derived', 'quality', 'analytics');

CREATE OR REPLACE VIEW ops.v_backup_status
WITH (security_barrier = true)
AS
SELECT
    d.database_name,
    b.finished_at_utc AS last_success_at_utc,
    b.sha256,
    b.pg_restore_list_pass,
    b.restore_smoke_tested_at_utc,
    b.restore_smoke_test_status,
    CASE
        WHEN b.finished_at_utc IS NULL THEN NULL
        ELSE GREATEST(0, EXTRACT(EPOCH FROM (clock_timestamp() - b.finished_at_utc))::BIGINT)
    END AS age_seconds
FROM (VALUES
    ('saxo_market'::TEXT),
    ('saxo_research_v13'::TEXT),
    ('saxo_forward_v13'::TEXT)
) AS d(database_name)
LEFT JOIN LATERAL (
    SELECT br.finished_at_utc, br.sha256, br.pg_restore_list_pass,
           br.restore_smoke_tested_at_utc, br.restore_smoke_test_status
    FROM ops.backup_run br
    WHERE br.database_name = d.database_name AND br.status = 'PASS'
    ORDER BY br.finished_at_utc DESC
    LIMIT 1
) b ON TRUE;

DO $$
BEGIN
    IF current_database() = 'saxo_market' THEN
        GRANT USAGE ON SCHEMA analytics, ops, quality TO saxo_app_reader;
        GRANT SELECT ON analytics.v_data_inventory, ops.v_ingestion_status,
                        quality.v_open_event, ops.v_backup_status TO saxo_app_reader;

        GRANT USAGE ON SCHEMA analytics, ops TO saxo_analyst_reader;
        GRANT SELECT ON analytics.v_data_inventory, analytics.v_data_coverage,
                        analytics.v_data_lineage, ops.v_storage_usage TO saxo_analyst_reader;
    ELSIF current_database() = 'saxo_research_v13' THEN
        DROP VIEW ops.v_ingestion_status;
        DROP VIEW quality.v_open_event;
        DROP VIEW ops.v_backup_status;

        GRANT USAGE ON SCHEMA analytics, ops TO v13_research_reader;
        GRANT SELECT ON analytics.v_data_inventory, analytics.v_data_coverage,
                        analytics.v_data_lineage, ops.v_storage_usage TO v13_research_reader;
    ELSE
        RAISE EXCEPTION 'unexpected operational-view database %', current_database();
    END IF;
END
$$;
