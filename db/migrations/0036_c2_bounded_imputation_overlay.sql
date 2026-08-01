SET LOCAL ROLE saxo_db_owner;

-- C2-only overlay evidence.  Provider raw, accepted curated rows, canonical
-- watermarks and canonical derived bars are intentionally not modified.
CREATE TABLE IF NOT EXISTS derived.c2_market_bar_1h_imputation (
    imputation_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    policy_id TEXT NOT NULL CHECK (policy_id='c2_etf_bounded_previous_valid_v1'),
    instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(instrument_id),
    session_calendar_id TEXT NOT NULL REFERENCES catalog.session_calendar(session_calendar_id),
    session_date DATE NOT NULL,
    time_utc TIMESTAMPTZ NOT NULL,
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes=60),
    price_basis TEXT NOT NULL CHECK (price_basis='native_ohlc'),
    open NUMERIC(24,12) NOT NULL CHECK (open > 0),
    high NUMERIC(24,12) NOT NULL CHECK (high > 0),
    low NUMERIC(24,12) NOT NULL CHECK (low > 0),
    close NUMERIC(24,12) NOT NULL CHECK (close > 0),
    volume NUMERIC(30,8) NULL CHECK (volume IS NULL),
    source_kind TEXT NOT NULL CHECK (source_kind='IMPUTED_PREVIOUS_VALID'),
    reason TEXT NOT NULL CHECK (
        reason IN ('PROVIDER_SESSION_OPEN_ROWS_MISSING','PROVIDER_INTERNAL_SESSION_ROWS_MISSING')
    ),
    source_time_utc TIMESTAMPTZ NOT NULL CHECK (source_time_utc < time_utc),
    consecutive_gap_index SMALLINT NOT NULL CHECK (consecutive_gap_index BETWEEN 1 AND 2),
    consecutive_gap_count SMALLINT NOT NULL CHECK (consecutive_gap_count BETWEEN 1 AND 2),
    candidate_data_version BIGINT NOT NULL,
    source_data_version BIGINT NOT NULL CHECK (source_data_version=candidate_data_version),
    source_ingestion_run_id BIGINT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    source_payload_sha256 CHAR(64) NOT NULL CHECK (source_payload_sha256 ~ '^[0-9a-f]{64}$'),
    source_artifact_relative_path TEXT NOT NULL CHECK (
        source_artifact_relative_path !~ '^/' AND
        source_artifact_relative_path !~ '^[A-Za-z]:' AND
        source_artifact_relative_path !~ '(^|/)\.\.(/|$)'
    ),
    quality_status TEXT NOT NULL CHECK (quality_status='WARN'),
    official_close_claim BOOLEAN NOT NULL DEFAULT FALSE CHECK (official_close_claim IS FALSE),
    total_return_claim BOOLEAN NOT NULL DEFAULT FALSE CHECK (total_return_claim IS FALSE),
    execution_price_claim BOOLEAN NOT NULL DEFAULT FALSE CHECK (execution_price_claim IS FALSE),
    review_id TEXT NOT NULL,
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (open=high AND high=low AND low=close),
    CHECK (consecutive_gap_index <= consecutive_gap_count),
    UNIQUE (policy_id,instrument_id,time_utc,candidate_data_version)
);

CREATE OR REPLACE FUNCTION derived.reject_c2_imputation_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'C2 imputation evidence is immutable';
END;
$$;

DROP TRIGGER IF EXISTS c2_imputation_immutable
ON derived.c2_market_bar_1h_imputation;
CREATE TRIGGER c2_imputation_immutable
BEFORE UPDATE OR DELETE ON derived.c2_market_bar_1h_imputation
FOR EACH ROW EXECUTE FUNCTION derived.reject_c2_imputation_mutation();

CREATE OR REPLACE VIEW analytics.v_c2_market_bar_1h_overlay
WITH (security_barrier = true)
AS
WITH ranked_imputation AS (
    SELECT x.*,
           ROW_NUMBER() OVER (
               PARTITION BY x.instrument_id,x.time_utc,x.price_basis
               ORDER BY x.created_at_utc DESC,x.imputation_id DESC
           ) AS selected_number
    FROM derived.c2_market_bar_1h_imputation x
), accepted AS (
    SELECT lower(i.market_key) AS instrument_key,
           b.instrument_id,b.time_utc,b.price_basis,
           b.open,b.high,b.low,b.close,b.volume,b.is_complete,
           'SAXO_ACCEPTED'::TEXT AS source_kind,
           b.quality_status,ARRAY[]::TEXT[] AS warning_ids,
           NULL::TEXT AS imputation_reason,
           NULL::TIMESTAMPTZ AS source_time_utc,
           NULL::SMALLINT AS consecutive_gap_index,
           NULL::SMALLINT AS consecutive_gap_count,
           b.data_version AS candidate_data_version,
           b.data_version AS source_data_version,
           b.latest_ingestion_run_id AS source_ingestion_run_id,
           NULL::CHAR(64) AS source_payload_sha256,
           NULL::TEXT AS source_artifact_relative_path,
           NULL::TEXT AS imputation_policy_id,
           FALSE AS official_close_claim,
           FALSE AS total_return_claim,
           FALSE AS execution_price_claim
    FROM curated.market_bar b
    JOIN catalog.instrument i USING (instrument_id)
    WHERE lower(i.market_key)=ANY(
        ARRAY['spy','iwm','efa','eem','vnq','shy','ief','tlt','tip','lqd','gld']::TEXT[]
    )
      AND b.horizon_minutes=60 AND b.price_basis='native_ohlc'
      AND b.is_complete AND b.quality_status='PASS'
), imputed AS (
    SELECT lower(i.market_key) AS instrument_key,
           x.instrument_id,x.time_utc,x.price_basis,
           x.open,x.high,x.low,x.close,x.volume,TRUE AS is_complete,
           x.source_kind,x.quality_status,
           ARRAY['C2_BOUNDED_IMPUTED_PREVIOUS_VALID']::TEXT[] AS warning_ids,
           x.reason AS imputation_reason,x.source_time_utc,
           x.consecutive_gap_index,x.consecutive_gap_count,
           x.candidate_data_version,x.source_data_version,
           x.source_ingestion_run_id,x.source_payload_sha256,
           x.source_artifact_relative_path,x.policy_id AS imputation_policy_id,
           x.official_close_claim,x.total_return_claim,x.execution_price_claim
    FROM ranked_imputation x
    JOIN catalog.instrument i USING (instrument_id)
    WHERE x.selected_number=1
      AND NOT EXISTS (
          SELECT 1 FROM curated.market_bar b
          WHERE b.instrument_id=x.instrument_id AND b.horizon_minutes=60
            AND b.time_utc=x.time_utc AND b.price_basis=x.price_basis
            AND b.is_complete AND b.quality_status='PASS'
      )
)
SELECT * FROM accepted
UNION ALL
SELECT * FROM imputed;

CREATE OR REPLACE VIEW analytics.v_c2_daily_close_with_imputation
WITH (security_barrier = true)
AS
WITH sessions AS (
    SELECT i.instrument_id,lower(i.market_key) AS instrument_key,
           i.session_calendar_id,si.session_date,
           si.open_time_utc,si.close_time_utc,
           c.metadata_json->>'verification_status' AS calendar_verification_status,
           CEIL(EXTRACT(EPOCH FROM (si.close_time_utc-si.open_time_utc))/3600)::INTEGER
               AS expected_slots,
           si.open_time_utc + make_interval(
               secs => FLOOR((EXTRACT(EPOCH FROM (si.close_time_utc-si.open_time_utc))-1)/3600)::INTEGER*3600
           ) AS terminal_time_utc
    FROM catalog.instrument i
    JOIN catalog.session_calendar c USING (session_calendar_id)
    JOIN catalog.session_interval si USING (session_calendar_id)
    WHERE lower(i.market_key)=ANY(
        ARRAY['spy','iwm','efa','eem','vnq','shy','ief','tlt','tip','lqd','gld']::TEXT[]
    )
      AND i.active_to_utc IS NULL AND si.session_status <> 'HOLIDAY'
), grouped AS (
    SELECT s.instrument_id,s.instrument_key,s.session_date,
           'native_ohlc'::TEXT AS price_basis,s.expected_slots,
           COUNT(DISTINCT o.time_utc)::INTEGER AS observed_slots,
           COUNT(*) FILTER (WHERE o.source_kind='IMPUTED_PREVIOUS_VALID')::INTEGER
               AS imputed_bar_count,
           BOOL_OR(
               o.time_utc=s.terminal_time_utc AND o.source_kind='SAXO_ACCEPTED'
           ) AS actual_terminal_close_present,
           (ARRAY_AGG(o.close ORDER BY o.time_utc DESC)
               FILTER (WHERE o.time_utc=s.terminal_time_utc AND o.source_kind='SAXO_ACCEPTED'))[1]
               AS close,
           MAX(o.source_ingestion_run_id) AS source_last_ingestion_run_id,
           s.calendar_verification_status
    FROM sessions s
    LEFT JOIN analytics.v_c2_market_bar_1h_overlay o
      ON o.instrument_id=s.instrument_id
     AND o.time_utc>=s.open_time_utc AND o.time_utc<s.close_time_utc
    GROUP BY s.instrument_id,s.instrument_key,s.session_date,s.expected_slots,
             s.terminal_time_utc,s.calendar_verification_status
)
SELECT instrument_id,instrument_key,session_date,price_basis,close,
       expected_slots,observed_slots,imputed_bar_count,actual_terminal_close_present,
       source_last_ingestion_run_id,
       CASE
           WHEN calendar_verification_status <> 'VERIFIED' THEN 'BLOCKED_CALENDAR_NOT_VERIFIED'
           WHEN NOT actual_terminal_close_present THEN 'BLOCKED_DAILY_CLOSE_SOURCE_MISSING'
           WHEN observed_slots <> expected_slots THEN 'BLOCKED_SESSION_COVERAGE'
           WHEN imputed_bar_count > 0 THEN 'PASS_WITH_IMPUTATION_WARNING'
           ELSE 'PASS'
       END AS derivation_status,
       CASE WHEN imputed_bar_count > 0
            THEN ARRAY['C2_BOUNDED_IMPUTED_PREVIOUS_VALID']::TEXT[]
            ELSE ARRAY[]::TEXT[] END AS warning_ids,
       FALSE AS official_close_claim,
       FALSE AS total_return_claim,
       FALSE AS execution_price_claim
FROM grouped;

REVOKE ALL ON derived.c2_market_bar_1h_imputation FROM PUBLIC;
REVOKE ALL ON analytics.v_c2_market_bar_1h_overlay FROM PUBLIC;
REVOKE ALL ON analytics.v_c2_daily_close_with_imputation FROM PUBLIC;
GRANT INSERT ON derived.c2_market_bar_1h_imputation TO saxo_ingest;
GRANT USAGE,SELECT ON SEQUENCE derived.c2_market_bar_1h_imputation_imputation_id_seq
TO saxo_ingest;
GRANT SELECT ON analytics.v_c2_market_bar_1h_overlay,
                analytics.v_c2_daily_close_with_imputation
TO saxo_app_reader,saxo_analyst_reader;
