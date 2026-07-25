SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0023_s6v5a_calendar_view_boundary_correction.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

-- session_interval.session_date is the FX close date. Add one day after the
-- 17:00 New York boundary shift, and align the equity final slot back to the
-- session-open hourly grid. Applied 0022 remains immutable.
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
    SELECT a.instrument_id, a.price_basis,
           SUM(
             GREATEST(
               LEAST(
                 FLOOR(EXTRACT(EPOCH FROM (bounds.last_slot-bounds.first_slot))/3600),
                 FLOOR(EXTRACT(EPOCH FROM (a.max_time_utc-bounds.first_slot))/3600)
               )
               - GREATEST(
                   0,
                   CEIL(EXTRACT(EPOCH FROM (a.min_time_utc-bounds.first_slot))/3600)
                 )
               + 1,
               0
             )
           )::BIGINT AS expected_rows
    FROM actual a
    JOIN catalog.session_interval si
      ON si.session_calendar_id=a.session_calendar_id
     AND si.session_status <> 'HOLIDAY'
    CROSS JOIN LATERAL (
      SELECT
        CASE WHEN a.asset_type='FxSpot'
             THEN date_trunc('hour',si.open_time_utc)+interval '1 hour'
             ELSE si.open_time_utc END AS first_slot,
        CASE WHEN a.asset_type='FxSpot'
             THEN date_trunc('hour',si.close_time_utc)-interval '2 hours'
             ELSE si.close_time_utc-interval '1 hour' END AS last_slot
    ) bounds
    WHERE bounds.last_slot >= a.min_time_utc
      AND bounds.first_slot <= a.max_time_utc
    GROUP BY a.instrument_id, a.price_basis
), aligned AS (
    SELECT b.instrument_id, b.price_basis, COUNT(*)::BIGINT AS aligned_rows
    FROM curated.market_bar b
    JOIN catalog.instrument i ON i.instrument_id=b.instrument_id
    JOIN catalog.session_calendar c ON c.session_calendar_id=i.session_calendar_id
    JOIN catalog.session_interval si
      ON si.session_calendar_id=i.session_calendar_id
     AND si.session_status <> 'HOLIDAY'
     AND si.session_date = CASE
       WHEN i.asset_type='FxSpot'
         THEN (timezone(c.timezone_name,b.time_utc)-interval '17 hours')::DATE + 1
       ELSE timezone(c.timezone_name,b.time_utc)::DATE
     END
     AND (
       (
         i.asset_type='FxSpot'
         AND b.time_utc >= date_trunc('hour',si.open_time_utc)+interval '1 hour'
         AND b.time_utc + interval '1 hour'
             <= date_trunc('hour',si.close_time_utc)-interval '1 hour'
         AND b.time_utc=date_trunc('hour',b.time_utc)
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
  SELECT MAX(candidate.latest_slot) AS latest_expected_complete_time_utc,
         MIN(candidate.next_slot) AS next_expected_time_utc
  FROM catalog.session_interval si
  CROSS JOIN LATERAL (
    SELECT
      CASE WHEN i.asset_type='FxSpot'
           THEN date_trunc('hour',si.open_time_utc)+interval '1 hour'
           ELSE si.open_time_utc END AS first_slot,
      CASE WHEN i.asset_type='FxSpot'
           THEN date_trunc('hour',si.close_time_utc)-interval '2 hours'
           ELSE si.close_time_utc-interval '1 hour' END AS raw_last_slot,
      clock_timestamp()-interval '1 hour'
        - CASE WHEN i.asset_type='FxSpot'
               THEN interval '10 minutes' ELSE interval '3 minutes' END AS due_cutoff
  ) raw_bounds
  CROSS JOIN LATERAL (
    SELECT raw_bounds.first_slot,
           raw_bounds.first_slot + make_interval(
             hours => FLOOR(EXTRACT(EPOCH FROM
               (raw_bounds.raw_last_slot-raw_bounds.first_slot))/3600)::INTEGER
           ) AS last_slot,
           raw_bounds.due_cutoff
  ) bounds
  CROSS JOIN LATERAL (
    SELECT
      CASE
        WHEN bounds.due_cutoff < bounds.first_slot THEN NULL
        ELSE LEAST(
          bounds.last_slot,
          bounds.first_slot + make_interval(
            hours => FLOOR(EXTRACT(EPOCH FROM
              (bounds.due_cutoff-bounds.first_slot))/3600)::INTEGER
          )
        )
      END AS latest_slot,
      CASE
        WHEN bounds.due_cutoff < bounds.first_slot THEN bounds.first_slot
        WHEN bounds.due_cutoff >= bounds.last_slot THEN NULL
        ELSE bounds.first_slot + make_interval(
          hours => FLOOR(EXTRACT(EPOCH FROM
            (bounds.due_cutoff-bounds.first_slot))/3600)::INTEGER + 1
        )
      END AS next_slot
  ) candidate
  WHERE si.session_calendar_id=i.session_calendar_id
    AND si.session_status <> 'HOLIDAY'
) expected ON TRUE;

GRANT SELECT ON analytics.v_data_coverage, analytics.v_data_freshness
    TO saxo_app_reader, saxo_analyst_reader;
