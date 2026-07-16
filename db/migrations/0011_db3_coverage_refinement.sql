SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0011_db3_coverage_refinement.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

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
    GROUP BY b.instrument_id, i.symbol, i.session_calendar_id,
             b.horizon_minutes, b.price_basis
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
), aligned AS (
    SELECT b.instrument_id, COUNT(*)::BIGINT AS aligned_rows
    FROM curated.market_bar b
    JOIN catalog.instrument i ON i.instrument_id = b.instrument_id
    JOIN catalog.session_interval si
      ON si.session_calendar_id = i.session_calendar_id
     AND si.session_status <> 'HOLIDAY'
     AND b.time_utc >= si.open_time_utc
     AND b.time_utc < si.close_time_utc
     AND MOD(EXTRACT(EPOCH FROM (b.time_utc - si.open_time_utc))::BIGINT, 3600) = 0
    WHERE b.horizon_minutes = 60
    GROUP BY b.instrument_id
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
         ELSE GREATEST(e.expected_rows - COALESCE(x.aligned_rows, 0), 0)
    END::BIGINT AS missing_rows,
    CASE
        WHEN a.session_calendar_id IS NULL
          OR c.metadata_json->>'verification_status' <> 'VERIFIED' THEN 'NOT_EVALUATED'
        WHEN e.expected_rows IS NULL THEN 'NOT_EVALUATED'
        WHEN a.duplicate_rows > 0 OR COALESCE(x.aligned_rows, 0) > e.expected_rows THEN 'FAIL'
        WHEN COALESCE(x.aligned_rows, 0) < e.expected_rows
          OR a.actual_rows > COALESCE(x.aligned_rows, 0) THEN 'WARN'
        ELSE 'PASS'
    END::TEXT AS coverage_status,
    GREATEST(a.actual_rows - COALESCE(x.aligned_rows, 0), 0)::BIGINT AS out_of_session_rows,
    COALESCE(x.aligned_rows, 0)::BIGINT AS calendar_aligned_rows,
    COALESCE(c.metadata_json->>'verification_status', 'UNASSIGNED')::TEXT AS calendar_verification_status
FROM actual a
LEFT JOIN expected e ON e.instrument_id = a.instrument_id
LEFT JOIN aligned x ON x.instrument_id = a.instrument_id
LEFT JOIN catalog.session_calendar c ON c.session_calendar_id = a.session_calendar_id;

