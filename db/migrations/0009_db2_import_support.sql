SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() NOT IN ('saxo_market', 'saxo_research_v13') THEN
        RAISE EXCEPTION '0009_db2_import_support.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

CREATE TABLE raw.reference_observation (
    source_file_id BIGINT NOT NULL REFERENCES ops.source_file(source_file_id),
    row_number BIGINT NOT NULL CHECK (row_number > 0),
    reference_kind TEXT NOT NULL,
    reference_key TEXT NULL,
    layer TEXT NOT NULL CHECK (layer IN ('raw', 'research_metadata')),
    observation_time_utc TIMESTAMPTZ NULL,
    payload_json JSONB NOT NULL,
    payload_sha256 CHAR(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (source_file_id, row_number)
);

ALTER TABLE curated.etf_total_return_daily
    ADD COLUMN source_file_id BIGINT REFERENCES ops.source_file(source_file_id);
ALTER TABLE curated.etf_total_return_daily
    ALTER COLUMN source_file_id SET NOT NULL;

ALTER TABLE ops.research_snapshot
    ADD COLUMN snapshot_manifest_relative_path TEXT NULL CHECK (
        snapshot_manifest_relative_path IS NULL OR (
            snapshot_manifest_relative_path !~ '^/' AND
            snapshot_manifest_relative_path !~ '^[A-Za-z]:' AND
            snapshot_manifest_relative_path !~ '(^|/)\.\.(/|$)'
        )
    ),
    ADD COLUMN dump_relative_path TEXT NULL CHECK (
        dump_relative_path IS NULL OR (
            dump_relative_path !~ '^/' AND
            dump_relative_path !~ '^[A-Za-z]:' AND
            dump_relative_path !~ '(^|/)\.\.(/|$)'
        )
    ),
    ADD COLUMN dump_sha256 CHAR(64) NULL CHECK (
        dump_sha256 IS NULL OR dump_sha256 ~ '^[0-9a-f]{64}$'
    ),
    ADD COLUMN dump_size_bytes BIGINT NULL CHECK (
        dump_size_bytes IS NULL OR dump_size_bytes >= 0
    ),
    ADD COLUMN dump_pg_restore_list_pass BOOLEAN NULL;

CREATE INDEX reference_observation_lookup_idx
    ON raw.reference_observation (reference_kind, reference_key, observation_time_utc);
CREATE INDEX etf_total_return_source_file_idx
    ON curated.etf_total_return_daily (source_file_id);

ALTER VIEW analytics.v_data_inventory RENAME TO v_market_data_inventory;

CREATE VIEW analytics.v_data_inventory
WITH (security_barrier = true)
AS
SELECT
    source_dataset_id, instrument_id, symbol, category, layer, price_basis,
    horizon_minutes, row_count, min_time_utc, max_time_utc,
    latest_complete_time_utc, quality_status, latest_ingestion_run_id,
    freshness_seconds, freshness_status
FROM analytics.v_market_data_inventory
UNION ALL
SELECT
    sf.source_dataset_id,
    NULL::BIGINT AS instrument_id,
    COALESCE(NULLIF(ro.reference_key, ''), sf.source_dataset_id) AS symbol,
    'reference'::TEXT AS category,
    ro.layer,
    ro.reference_kind AS price_basis,
    NULL::SMALLINT AS horizon_minutes,
    COUNT(*)::BIGINT AS row_count,
    MIN(ro.observation_time_utc) AS min_time_utc,
    MAX(ro.observation_time_utc) AS max_time_utc,
    NULL::TIMESTAMPTZ AS latest_complete_time_utc,
    'NOT_EVALUATED'::TEXT AS quality_status,
    MAX(sf.ingestion_run_id) AS latest_ingestion_run_id,
    NULL::BIGINT AS freshness_seconds,
    'NOT_EVALUATED'::TEXT AS freshness_status
FROM raw.reference_observation ro
JOIN ops.source_file sf ON sf.source_file_id = ro.source_file_id
GROUP BY sf.source_dataset_id, ro.reference_key, ro.layer, ro.reference_kind;

CREATE OR REPLACE VIEW analytics.v_data_lineage
WITH (security_barrier = true)
AS
WITH raw_file_counts AS (
    SELECT source_file_id, COUNT(*)::BIGINT AS row_count
    FROM raw.market_bar_revision
    GROUP BY source_file_id
    UNION ALL
    SELECT source_file_id, COUNT(*)::BIGINT AS row_count
    FROM raw.reference_observation
    GROUP BY source_file_id
), raw_counts AS (
    SELECT source_file_id, SUM(row_count)::BIGINT AS raw_rows
    FROM raw_file_counts
    GROUP BY source_file_id
), curated_file_counts AS (
    SELECT r.source_file_id, COUNT(*)::BIGINT AS row_count
    FROM curated.market_bar b
    JOIN raw.market_bar_revision r
      ON r.ingestion_run_id = b.latest_ingestion_run_id
     AND r.instrument_id = b.instrument_id
     AND r.horizon_minutes = b.horizon_minutes
     AND r.time_utc = b.time_utc
     AND r.price_basis = b.price_basis
    GROUP BY r.source_file_id
    UNION ALL
    SELECT source_file_id, COUNT(*)::BIGINT AS row_count
    FROM curated.etf_total_return_daily
    GROUP BY source_file_id
), curated_counts AS (
    SELECT source_file_id, SUM(row_count)::BIGINT AS curated_rows
    FROM curated_file_counts
    GROUP BY source_file_id
)
SELECT
    sf.source_dataset_id,
    sf.source_file_id,
    sf.relative_path,
    sf.ingestion_run_id,
    COALESCE(r.raw_rows, 0)::BIGINT AS raw_rows,
    COALESCE(c.curated_rows, 0)::BIGINT AS curated_rows,
    0::BIGINT AS derived_rows
FROM ops.source_file sf
LEFT JOIN raw_counts r ON r.source_file_id = sf.source_file_id
LEFT JOIN curated_counts c ON c.source_file_id = sf.source_file_id;

DO $$
BEGIN
    IF current_database() = 'saxo_market' THEN
        GRANT SELECT, INSERT ON raw.reference_observation TO saxo_ingest;
        GRANT SELECT ON analytics.v_data_inventory TO saxo_app_reader, saxo_analyst_reader;
    ELSE
        GRANT SELECT ON analytics.v_data_inventory TO v13_research_reader;
    END IF;
END
$$;
