SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0010_db3_incremental_support.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

ALTER TABLE ops.ingestion_run
    ADD COLUMN run_manifest_relative_path TEXT NULL CHECK (
        run_manifest_relative_path IS NULL OR (
            run_manifest_relative_path !~ '^/' AND
            run_manifest_relative_path !~ '^[A-Za-z]:' AND
            run_manifest_relative_path !~ '(^|/)\.\.(/|$)'
        )
    ),
    ADD COLUMN last_success_step TEXT NULL,
    ADD COLUMN metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb;

ALTER TABLE ops.watermark
    ADD COLUMN last_ingestion_run_id BIGINT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    ADD COLUMN data_status TEXT NOT NULL DEFAULT 'ACTIVE' CHECK (
        data_status IN ('ACTIVE', 'STALE_DATA_VERSION', 'BLOCKED_QUALITY')
    );

CREATE UNLOGGED TABLE staging.market_bar (
    ingestion_run_id BIGINT NOT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    source_file_id BIGINT NOT NULL REFERENCES ops.source_file(source_file_id),
    instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(instrument_id),
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes = 60),
    time_utc TIMESTAMPTZ NOT NULL,
    open NUMERIC(24,12) NOT NULL,
    high NUMERIC(24,12) NOT NULL,
    low NUMERIC(24,12) NOT NULL,
    close NUMERIC(24,12) NOT NULL,
    open_bid NUMERIC(24,12) NULL,
    high_bid NUMERIC(24,12) NULL,
    low_bid NUMERIC(24,12) NULL,
    close_bid NUMERIC(24,12) NULL,
    open_ask NUMERIC(24,12) NULL,
    high_ask NUMERIC(24,12) NULL,
    low_ask NUMERIC(24,12) NULL,
    close_ask NUMERIC(24,12) NULL,
    volume NUMERIC(30,8) NULL,
    market_trading_state TEXT NULL,
    price_basis TEXT NOT NULL,
    is_complete BOOLEAN NOT NULL,
    data_version BIGINT NULL,
    delayed_by_minutes INTEGER NULL,
    retrieved_at_utc TIMESTAMPTZ NOT NULL,
    payload_sha256 CHAR(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (ingestion_run_id, instrument_id, time_utc, price_basis)
);

CREATE INDEX curated_market_bar_session_lookup_idx
    ON curated.market_bar (instrument_id, time_utc)
    WHERE horizon_minutes = 60 AND is_complete AND quality_status = 'PASS';
CREATE INDEX session_interval_time_lookup_idx
    ON catalog.session_interval (session_calendar_id, open_time_utc, close_time_utc)
    WHERE session_status <> 'HOLIDAY';

CREATE OR REPLACE VIEW analytics.v_data_coverage
WITH (security_barrier = true)
AS
WITH actual AS (
    SELECT
        b.instrument_id,
        i.symbol,
        i.session_calendar_id,
        b.horizon_minutes,
        b.price_basis,
        COUNT(*)::BIGINT AS actual_rows,
        COUNT(*) FILTER (WHERE b.is_complete)::BIGINT AS complete_rows,
        COUNT(*) FILTER (WHERE NOT b.is_complete)::BIGINT AS incomplete_rows,
        COUNT(*) - COUNT(DISTINCT b.time_utc) AS duplicate_rows,
        MIN(b.time_utc) AS min_time_utc,
        MAX(b.time_utc) AS max_time_utc
    FROM curated.market_bar b
    JOIN catalog.instrument i ON i.instrument_id = b.instrument_id
    WHERE b.horizon_minutes = 60
    GROUP BY b.instrument_id, i.symbol, i.session_calendar_id, b.horizon_minutes, b.price_basis
), expected AS (
    SELECT
        a.instrument_id,
        COUNT(slot.time_utc)::BIGINT AS expected_rows
    FROM actual a
    JOIN catalog.session_interval si
      ON si.session_calendar_id = a.session_calendar_id
     AND si.session_status <> 'HOLIDAY'
    CROSS JOIN LATERAL generate_series(
        si.open_time_utc,
        si.close_time_utc - interval '1 minute',
        interval '60 minutes'
    ) AS slot(time_utc)
    WHERE slot.time_utc BETWEEN a.min_time_utc AND a.max_time_utc
    GROUP BY a.instrument_id
)
SELECT
    NULL::TEXT AS source_dataset_id,
    a.instrument_id,
    a.symbol,
    a.horizon_minutes,
    e.expected_rows,
    a.actual_rows,
    a.complete_rows,
    a.incomplete_rows,
    a.duplicate_rows,
    CASE WHEN e.expected_rows IS NULL THEN NULL
         ELSE GREATEST(e.expected_rows - a.actual_rows, 0) END::BIGINT AS missing_rows,
    CASE
        WHEN a.session_calendar_id IS NULL
          OR c.metadata_json->>'verification_status' <> 'VERIFIED' THEN 'NOT_EVALUATED'
        WHEN e.expected_rows IS NULL THEN 'NOT_EVALUATED'
        WHEN a.duplicate_rows > 0 OR a.actual_rows > e.expected_rows THEN 'FAIL'
        WHEN a.actual_rows < e.expected_rows THEN 'WARN'
        ELSE 'PASS'
    END::TEXT AS coverage_status
FROM actual a
LEFT JOIN expected e ON e.instrument_id = a.instrument_id
LEFT JOIN catalog.session_calendar c ON c.session_calendar_id = a.session_calendar_id;

CREATE VIEW analytics.v_data_freshness
WITH (security_barrier = true)
AS
SELECT
    w.instrument_id,
    i.symbol,
    i.category,
    w.horizon_minutes,
    w.price_basis,
    w.latest_seen_time_utc,
    w.latest_complete_time_utc,
    w.data_version,
    w.data_status,
    w.last_ingestion_run_id,
    CASE WHEN w.latest_complete_time_utc IS NULL THEN NULL
         ELSE GREATEST(0, EXTRACT(EPOCH FROM (clock_timestamp() - w.latest_complete_time_utc))::BIGINT)
    END AS freshness_seconds,
    next_slot.next_expected_time_utc,
    CASE
        WHEN w.data_status <> 'ACTIVE' THEN 'FAIL'
        WHEN i.session_calendar_id IS NULL
          OR c.metadata_json->>'verification_status' <> 'VERIFIED'
          OR w.latest_complete_time_utc IS NULL
          OR next_slot.next_expected_time_utc IS NULL THEN 'NOT_EVALUATED'
        WHEN clock_timestamp() <= next_slot.next_expected_time_utc + interval '2 hours' THEN 'PASS'
        ELSE 'STALE'
    END::TEXT AS freshness_status
FROM ops.watermark w
JOIN catalog.instrument i ON i.instrument_id = w.instrument_id
LEFT JOIN catalog.session_calendar c ON c.session_calendar_id = i.session_calendar_id
LEFT JOIN LATERAL (
    SELECT MIN(slot.time_utc) AS next_expected_time_utc
    FROM catalog.session_interval si
    CROSS JOIN LATERAL generate_series(
        si.open_time_utc,
        si.close_time_utc - interval '1 minute',
        interval '60 minutes'
    ) AS slot(time_utc)
    WHERE si.session_calendar_id = i.session_calendar_id
      AND si.session_status <> 'HOLIDAY'
      AND slot.time_utc > w.latest_complete_time_utc
) next_slot ON TRUE;

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
    END AS freshness_seconds,
    r.last_success_step,
    r.run_manifest_relative_path
FROM ops.ingestion_run r;

GRANT SELECT, INSERT, DELETE ON staging.market_bar TO saxo_ingest;
GRANT DELETE ON catalog.session_interval TO saxo_ingest;
GRANT DELETE ON derived.market_bar_4h, derived.market_bar_1d_risk TO saxo_ingest;
GRANT SELECT ON analytics.v_data_freshness TO saxo_app_reader;
