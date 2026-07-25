SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0021_s6v5a_fx_maintenance_slot_boundary.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

CREATE OR REPLACE VIEW analytics.v_data_coverage
WITH (security_barrier = true)
AS
WITH actual AS (
    SELECT b.instrument_id, i.market_key, i.symbol, i.category, i.asset_type,
           i.session_calendar_id, b.horizon_minutes, b.price_basis,
           COUNT(*)::BIGINT AS actual_rows,
           COUNT(*) FILTER (WHERE b.is_complete)::BIGINT AS complete_rows,
           COUNT(*) FILTER (WHERE NOT b.is_complete)::BIGINT AS incomplete_rows,
           COUNT(*) - COUNT(DISTINCT b.time_utc) AS duplicate_rows,
           MIN(b.time_utc) AS min_time_utc, MAX(b.time_utc) AS max_time_utc
    FROM curated.market_bar b
    JOIN catalog.instrument i ON i.instrument_id=b.instrument_id
    WHERE b.horizon_minutes=60
    GROUP BY b.instrument_id, i.market_key, i.symbol, i.category, i.asset_type,
             i.session_calendar_id, b.horizon_minutes, b.price_basis
), expected AS (
    SELECT a.instrument_id, a.price_basis, COUNT(slot.time_utc)::BIGINT AS expected_rows
    FROM actual a
    JOIN catalog.session_interval si
      ON si.session_calendar_id=a.session_calendar_id
     AND si.session_status <> 'HOLIDAY'
    CROSS JOIN LATERAL generate_series(
        CASE WHEN a.asset_type='FxSpot'
             THEN date_trunc('hour', si.open_time_utc) + interval '1 hour'
             ELSE si.open_time_utc END,
        CASE WHEN a.asset_type='FxSpot'
             THEN date_trunc('hour', si.close_time_utc) - interval '2 hours'
             ELSE si.close_time_utc - interval '1 hour' END,
        interval '60 minutes'
    ) AS slot(time_utc)
    WHERE slot.time_utc BETWEEN a.min_time_utc AND a.max_time_utc
    GROUP BY a.instrument_id, a.price_basis
), aligned AS (
    SELECT b.instrument_id, b.price_basis, COUNT(*)::BIGINT AS aligned_rows
    FROM curated.market_bar b
    JOIN catalog.instrument i ON i.instrument_id=b.instrument_id
    JOIN catalog.session_interval si
      ON si.session_calendar_id=i.session_calendar_id
     AND si.session_status <> 'HOLIDAY'
     AND (
        (
            i.asset_type='FxSpot'
            AND b.time_utc >= date_trunc('hour', si.open_time_utc) + interval '1 hour'
            AND b.time_utc + interval '1 hour'
                <= date_trunc('hour', si.close_time_utc) - interval '1 hour'
            AND b.time_utc=date_trunc('hour', b.time_utc)
        )
        OR
        (
            i.asset_type <> 'FxSpot'
            AND b.time_utc >= si.open_time_utc
            AND b.time_utc + interval '1 hour' <= si.close_time_utc
            AND MOD(EXTRACT(EPOCH FROM (b.time_utc-si.open_time_utc))::BIGINT,3600)=0
        )
     )
    WHERE b.horizon_minutes=60 AND b.is_complete
    GROUP BY b.instrument_id, b.price_basis
)
SELECT NULL::TEXT AS source_dataset_id,
       a.instrument_id, a.symbol, a.horizon_minutes, e.expected_rows,
       a.actual_rows, a.complete_rows, a.incomplete_rows, a.duplicate_rows,
       CASE WHEN e.expected_rows IS NULL THEN NULL
            ELSE GREATEST(e.expected_rows-COALESCE(x.aligned_rows,0),0)
       END::BIGINT AS missing_rows,
       CASE
         WHEN a.session_calendar_id IS NULL
           OR c.metadata_json->>'verification_status' <> 'VERIFIED' THEN 'NOT_EVALUATED'
         WHEN e.expected_rows IS NULL THEN 'NOT_EVALUATED'
         WHEN a.duplicate_rows > 0 OR COALESCE(x.aligned_rows,0) > e.expected_rows THEN 'FAIL'
         WHEN COALESCE(x.aligned_rows,0) < e.expected_rows
           OR a.actual_rows > COALESCE(x.aligned_rows,0) THEN 'WARN'
         ELSE 'PASS'
       END::TEXT AS coverage_status,
       GREATEST(a.actual_rows-COALESCE(x.aligned_rows,0),0)::BIGINT AS out_of_session_rows,
       COALESCE(x.aligned_rows,0)::BIGINT AS calendar_aligned_rows,
       COALESCE(c.metadata_json->>'verification_status','UNASSIGNED')::TEXT
           AS calendar_verification_status,
       a.market_key AS instrument_key, a.category, a.price_basis
FROM actual a
LEFT JOIN expected e ON e.instrument_id=a.instrument_id AND e.price_basis=a.price_basis
LEFT JOIN aligned x ON x.instrument_id=a.instrument_id AND x.price_basis=a.price_basis
LEFT JOIN catalog.session_calendar c ON c.session_calendar_id=a.session_calendar_id;

CREATE OR REPLACE VIEW analytics.v_data_freshness
WITH (security_barrier = true)
AS
SELECT w.instrument_id, i.symbol, i.category, w.horizon_minutes, w.price_basis,
       w.latest_seen_time_utc, w.latest_complete_time_utc, w.data_version,
       w.data_status, w.last_ingestion_run_id,
       CASE WHEN w.latest_complete_time_utc IS NULL THEN NULL
            ELSE GREATEST(0,EXTRACT(EPOCH FROM
                 (clock_timestamp()-w.latest_complete_time_utc))::BIGINT)
       END AS freshness_seconds,
       expected.next_expected_time_utc,
       CASE
         WHEN w.data_status <> 'ACTIVE' THEN 'FAIL'
         WHEN i.session_calendar_id IS NULL
           OR c.metadata_json->>'verification_status' <> 'VERIFIED'
           OR w.latest_complete_time_utc IS NULL
           OR expected.latest_expected_complete_time_utc IS NULL THEN 'NOT_EVALUATED'
         WHEN w.latest_complete_time_utc >= expected.latest_expected_complete_time_utc THEN 'PASS'
         ELSE 'STALE'
       END::TEXT AS freshness_status,
       i.market_key AS instrument_key,
       expected.latest_expected_complete_time_utc
FROM ops.watermark w
JOIN catalog.instrument i ON i.instrument_id=w.instrument_id
LEFT JOIN catalog.session_calendar c ON c.session_calendar_id=i.session_calendar_id
LEFT JOIN LATERAL (
    SELECT
      MAX(slot.time_utc) FILTER (
        WHERE clock_timestamp() >= slot.time_utc + interval '1 hour'
          + CASE WHEN i.asset_type='FxSpot'
                 THEN interval '10 minutes' ELSE interval '3 minutes' END
      ) AS latest_expected_complete_time_utc,
      MIN(slot.time_utc) FILTER (
        WHERE clock_timestamp() < slot.time_utc + interval '1 hour'
          + CASE WHEN i.asset_type='FxSpot'
                 THEN interval '10 minutes' ELSE interval '3 minutes' END
      ) AS next_expected_time_utc
    FROM catalog.session_interval si
    CROSS JOIN LATERAL generate_series(
      CASE WHEN i.asset_type='FxSpot'
           THEN date_trunc('hour',si.open_time_utc)+interval '1 hour'
           ELSE si.open_time_utc END,
      CASE WHEN i.asset_type='FxSpot'
           THEN date_trunc('hour',si.close_time_utc)-interval '2 hours'
           ELSE si.close_time_utc-interval '1 hour' END,
      interval '60 minutes'
    ) AS slot(time_utc)
    WHERE si.session_calendar_id=i.session_calendar_id
      AND si.session_status <> 'HOLIDAY'
) expected ON TRUE;

GRANT SELECT ON analytics.v_data_coverage, analytics.v_data_freshness
    TO saxo_app_reader, saxo_analyst_reader;
