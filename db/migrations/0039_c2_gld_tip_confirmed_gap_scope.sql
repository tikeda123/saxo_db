SET LOCAL ROLE saxo_db_owner;

-- Freeze the only user-approved forward-fill scope.  The provider raw archive,
-- accepted curated bars, canonical watermarks and canonical derived bars remain
-- untouched.  This migration appends four C2-only overlay rows plus WARN events.
CREATE TABLE derived.c2_confirmed_imputation_scope (
    policy_id TEXT NOT NULL CHECK (policy_id='c2_etf_bounded_previous_valid_v1'),
    instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(instrument_id),
    session_date DATE NOT NULL CHECK (session_date=DATE '2026-07-29'),
    time_utc TIMESTAMPTZ NOT NULL CHECK (
        time_utc IN (
            TIMESTAMPTZ '2026-07-29 13:30:00+00',
            TIMESTAMPTZ '2026-07-29 14:30:00+00'
        )
    ),
    source_time_utc TIMESTAMPTZ NOT NULL
        CHECK (source_time_utc=TIMESTAMPTZ '2026-07-28 19:30:00+00'),
    candidate_data_version BIGINT NOT NULL,
    source_data_version BIGINT NOT NULL CHECK (source_data_version=candidate_data_version),
    source_ingestion_run_id BIGINT NOT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    source_payload_sha256 CHAR(64) NOT NULL
        CHECK (source_payload_sha256 ~ '^[0-9a-f]{64}$'),
    source_artifact_relative_path TEXT NOT NULL CHECK (
        source_artifact_relative_path !~ '^/' AND
        source_artifact_relative_path !~ '^[A-Za-z]:' AND
        source_artifact_relative_path !~ '(^|/)\.\.(/|$)'
    ),
    consecutive_gap_index SMALLINT NOT NULL CHECK (consecutive_gap_index IN (1,2)),
    consecutive_gap_count SMALLINT NOT NULL CHECK (consecutive_gap_count=2),
    imputation_method TEXT NOT NULL
        CHECK (imputation_method='FORWARD_FILL_PREVIOUS_ACTUAL_CLOSE'),
    warning_id TEXT NOT NULL
        CHECK (warning_id='C2_BOUNDED_IMPUTED_PREVIOUS_VALID'),
    review_id TEXT NOT NULL
        CHECK (review_id='c2_gld_tip_live_confirmed_gap_20260807'),
    approved_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (policy_id,instrument_id,time_utc,candidate_data_version),
    UNIQUE (
        policy_id,instrument_id,time_utc,candidate_data_version,
        source_time_utc,source_data_version,source_ingestion_run_id,
        source_payload_sha256,source_artifact_relative_path,review_id
    )
);

CREATE TRIGGER c2_confirmed_imputation_scope_immutable
BEFORE UPDATE OR DELETE ON derived.c2_confirmed_imputation_scope
FOR EACH ROW EXECUTE FUNCTION derived.reject_c2_imputation_mutation();

WITH approved (
    instrument_key,session_date,time_utc,source_time_utc,
    candidate_data_version,consecutive_gap_index
) AS (
    VALUES
      ('tip'::TEXT,DATE '2026-07-29',TIMESTAMPTZ '2026-07-29 13:30:00+00',
       TIMESTAMPTZ '2026-07-28 19:30:00+00',29759068::BIGINT,1::SMALLINT),
      ('tip',DATE '2026-07-29',TIMESTAMPTZ '2026-07-29 14:30:00+00',
       TIMESTAMPTZ '2026-07-28 19:30:00+00',29759068::BIGINT,2::SMALLINT),
      ('gld',DATE '2026-07-29',TIMESTAMPTZ '2026-07-29 13:30:00+00',
       TIMESTAMPTZ '2026-07-28 19:30:00+00',29749768::BIGINT,1::SMALLINT),
      ('gld',DATE '2026-07-29',TIMESTAMPTZ '2026-07-29 14:30:00+00',
       TIMESTAMPTZ '2026-07-28 19:30:00+00',29749768::BIGINT,2::SMALLINT)
)
INSERT INTO derived.c2_confirmed_imputation_scope (
    policy_id,instrument_id,session_date,time_utc,source_time_utc,
    candidate_data_version,source_data_version,source_ingestion_run_id,
    source_payload_sha256,source_artifact_relative_path,
    consecutive_gap_index,consecutive_gap_count,imputation_method,
    warning_id,review_id
)
SELECT
    'c2_etf_bounded_previous_valid_v1',i.instrument_id,a.session_date,a.time_utc,
    a.source_time_utc,a.candidate_data_version,a.candidate_data_version,
    b.latest_ingestion_run_id,r.payload_sha256,s.relative_path,
    a.consecutive_gap_index,2,'FORWARD_FILL_PREVIOUS_ACTUAL_CLOSE',
    'C2_BOUNDED_IMPUTED_PREVIOUS_VALID',
    'c2_gld_tip_live_confirmed_gap_20260807'
FROM approved a
JOIN catalog.instrument i
  ON lower(i.market_key)=a.instrument_key
 AND i.provider='Saxo OpenAPI' AND i.environment='SIM'
 AND i.asset_type='Etf' AND i.active_to_utc IS NULL
JOIN catalog.session_calendar c USING (session_calendar_id)
JOIN catalog.session_interval si
  ON si.session_calendar_id=i.session_calendar_id
 AND si.session_date=a.session_date AND si.session_status<>'HOLIDAY'
 AND si.open_time_utc=TIMESTAMPTZ '2026-07-29 13:30:00+00'
 AND si.close_time_utc=TIMESTAMPTZ '2026-07-29 20:00:00+00'
JOIN curated.market_bar b
  ON b.instrument_id=i.instrument_id AND b.horizon_minutes=60
 AND b.price_basis='native_ohlc' AND b.time_utc=a.source_time_utc
 AND b.is_complete AND b.quality_status='PASS'
 AND b.data_version=a.candidate_data_version
JOIN raw.market_bar_revision r
  ON r.ingestion_run_id=b.latest_ingestion_run_id
 AND r.instrument_id=b.instrument_id AND r.horizon_minutes=b.horizon_minutes
 AND r.price_basis=b.price_basis AND r.time_utc=b.time_utc
JOIN ops.source_file s ON s.source_file_id=r.source_file_id
JOIN curated.market_bar right_anchor
  ON right_anchor.instrument_id=i.instrument_id AND right_anchor.horizon_minutes=60
 AND right_anchor.price_basis='native_ohlc'
 AND right_anchor.time_utc=TIMESTAMPTZ '2026-07-29 15:30:00+00'
 AND right_anchor.is_complete AND right_anchor.quality_status='PASS'
 AND right_anchor.data_version=a.candidate_data_version
JOIN curated.market_bar terminal_actual
  ON terminal_actual.instrument_id=i.instrument_id AND terminal_actual.horizon_minutes=60
 AND terminal_actual.price_basis='native_ohlc'
 AND terminal_actual.time_utc=TIMESTAMPTZ '2026-07-29 19:30:00+00'
 AND terminal_actual.is_complete AND terminal_actual.quality_status='PASS'
 AND terminal_actual.data_version=a.candidate_data_version
WHERE c.metadata_json->>'verification_status'='VERIFIED'
  AND NOT EXISTS (
      SELECT 1 FROM curated.market_bar missing_actual
      WHERE missing_actual.instrument_id=i.instrument_id
        AND missing_actual.horizon_minutes=60
        AND missing_actual.price_basis='native_ohlc'
        AND missing_actual.time_utc=a.time_utc
  );

DO $$
DECLARE
    v_scope_count INTEGER;
    v_missing_count INTEGER;
    v_unscoped_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_scope_count
    FROM derived.c2_confirmed_imputation_scope;
    IF v_scope_count <> 4 THEN
        RAISE EXCEPTION 'BLOCKED_CONFIRMED_IMPUTATION_SCOPE_EXPECTED_4_FOUND_%', v_scope_count;
    END IF;

    WITH bounds AS (
        SELECT i.instrument_id,i.session_calendar_id,
               MIN(b.time_utc) AS min_time_utc,MAX(b.time_utc) AS max_time_utc
        FROM catalog.instrument i
        JOIN curated.market_bar b USING (instrument_id)
        WHERE lower(i.market_key) IN ('tip','gld')
          AND b.horizon_minutes=60 AND b.price_basis='native_ohlc'
        GROUP BY i.instrument_id,i.session_calendar_id
    ), expected AS (
        SELECT x.instrument_id,slot.time_utc
        FROM bounds x
        JOIN catalog.session_interval si
          ON si.session_calendar_id=x.session_calendar_id
         AND si.session_status<>'HOLIDAY'
        CROSS JOIN LATERAL generate_series(
            si.open_time_utc,si.close_time_utc-INTERVAL '1 hour',INTERVAL '1 hour'
        ) slot(time_utc)
        WHERE slot.time_utc BETWEEN x.min_time_utc AND x.max_time_utc
    ), missing AS (
        SELECT e.instrument_id,e.time_utc
        FROM expected e
        LEFT JOIN curated.market_bar b
          ON b.instrument_id=e.instrument_id AND b.horizon_minutes=60
         AND b.price_basis='native_ohlc' AND b.time_utc=e.time_utc
         AND b.is_complete
        WHERE b.instrument_id IS NULL
    )
    SELECT COUNT(*),COUNT(*) FILTER (WHERE s.instrument_id IS NULL)
      INTO v_missing_count,v_unscoped_count
    FROM missing m
    LEFT JOIN derived.c2_confirmed_imputation_scope s
      ON s.instrument_id=m.instrument_id AND s.time_utc=m.time_utc;
    IF v_missing_count <> 4 OR v_unscoped_count <> 0 THEN
        RAISE EXCEPTION 'BLOCKED_PROVIDER_GAPS_NOT_EXACTLY_CONFIRMED_SCOPE';
    END IF;
END
$$;

ALTER TABLE derived.c2_market_bar_1h_imputation
    ALTER COLUMN source_ingestion_run_id SET NOT NULL;

ALTER TABLE derived.c2_market_bar_1h_imputation
    ADD CONSTRAINT c2_imputation_confirmed_scope_fk
    FOREIGN KEY (
        policy_id,instrument_id,time_utc,candidate_data_version,
        source_time_utc,source_data_version,source_ingestion_run_id,
        source_payload_sha256,source_artifact_relative_path,review_id
    ) REFERENCES derived.c2_confirmed_imputation_scope (
        policy_id,instrument_id,time_utc,candidate_data_version,
        source_time_utc,source_data_version,source_ingestion_run_id,
        source_payload_sha256,source_artifact_relative_path,review_id
    );

INSERT INTO derived.c2_market_bar_1h_imputation (
    policy_id,instrument_id,session_calendar_id,session_date,time_utc,
    horizon_minutes,price_basis,open,high,low,close,volume,
    source_kind,reason,source_time_utc,consecutive_gap_index,
    consecutive_gap_count,candidate_data_version,source_data_version,
    source_ingestion_run_id,source_payload_sha256,
    source_artifact_relative_path,quality_status,
    official_close_claim,total_return_claim,execution_price_claim,review_id
)
SELECT s.policy_id,s.instrument_id,i.session_calendar_id,s.session_date,s.time_utc,
       60,'native_ohlc',b.close,b.close,b.close,b.close,NULL,
       'IMPUTED_PREVIOUS_VALID','PROVIDER_SESSION_OPEN_ROWS_MISSING',
       s.source_time_utc,s.consecutive_gap_index,s.consecutive_gap_count,
       s.candidate_data_version,s.source_data_version,s.source_ingestion_run_id,
       s.source_payload_sha256,s.source_artifact_relative_path,'WARN',
       FALSE,FALSE,FALSE,s.review_id
FROM derived.c2_confirmed_imputation_scope s
JOIN catalog.instrument i USING (instrument_id)
JOIN curated.market_bar b
  ON b.instrument_id=s.instrument_id AND b.horizon_minutes=60
 AND b.price_basis='native_ohlc' AND b.time_utc=s.source_time_utc
 AND b.is_complete AND b.quality_status='PASS'
 AND b.data_version=s.source_data_version
ON CONFLICT (policy_id,instrument_id,time_utc,candidate_data_version) DO NOTHING;

DO $$
BEGIN
    IF (SELECT COUNT(*) FROM derived.c2_market_bar_1h_imputation) <> 4 THEN
        RAISE EXCEPTION 'BLOCKED_IMPUTATION_OVERLAY_NOT_EXACTLY_4';
    END IF;
END
$$;

INSERT INTO quality.event_rule_policy (
    rule_id,default_scope_kind,default_applicability,affected_layer,price_basis,
    blocking_severities,supersession_condition,automatic_review_allowed,
    operator_review_required
) VALUES (
    'c2_confirmed_provider_gap_forward_fill','SERIES','CURRENT','derived','native_ohlc',
    ARRAY[]::TEXT[],
    'warning remains auditable; an arriving actual row supersedes overlay selection but does not delete evidence',
    FALSE,FALSE
);

INSERT INTO quality.event (
    ingestion_run_id,instrument_id,horizon_minutes,time_utc,rule_id,severity,
    observed_value,action,status
)
SELECT s.source_ingestion_run_id,s.instrument_id,60,s.time_utc,
       'c2_confirmed_provider_gap_forward_fill','WARN',
       jsonb_build_object(
           'provider_observation_present',FALSE,
           'source_kind','IMPUTED_PREVIOUS_VALID',
           'imputation_method',s.imputation_method,
           'source_time_utc',s.source_time_utc,
           'source_data_version',s.source_data_version,
           'source_ingestion_run_id',s.source_ingestion_run_id,
           'source_payload_sha256',s.source_payload_sha256,
           'source_artifact_relative_path',s.source_artifact_relative_path,
           'review_id',s.review_id,
           'raw_or_curated_modified',FALSE,
           'official_close_claim',FALSE,
           'total_return_claim',FALSE,
           'execution_price_claim',FALSE
       ),
       'retain provider gap as OPEN WARN and use only the explicit C2 derived overlay',
       'OPEN'
FROM derived.c2_confirmed_imputation_scope s;

CREATE UNIQUE INDEX quality_c2_confirmed_provider_gap_warning_idx
ON quality.event (rule_id,instrument_id,time_utc)
WHERE rule_id='c2_confirmed_provider_gap_forward_fill';

CREATE VIEW analytics.v_c2_confirmed_provider_gap_warning
WITH (security_barrier=true)
AS
SELECT lower(i.market_key) AS instrument_key,s.session_date,
       s.time_utc AS missing_time_utc,x.imputation_id,x.source_kind,
       s.imputation_method,x.reason AS imputation_reason,
       s.source_time_utc,s.source_data_version,s.source_ingestion_run_id,
       s.source_payload_sha256,s.source_artifact_relative_path,
       s.review_id,s.warning_id,x.quality_status,
       q.quality_event_id,q.severity AS quality_event_severity,
       q.status AS quality_event_status,
       NOT EXISTS (
           SELECT 1 FROM curated.market_bar b
           WHERE b.instrument_id=s.instrument_id AND b.horizon_minutes=60
             AND b.price_basis='native_ohlc' AND b.time_utc=s.time_utc
             AND b.is_complete AND b.quality_status='PASS'
       ) AS active_in_overlay,
       x.official_close_claim,x.total_return_claim,x.execution_price_claim
FROM derived.c2_confirmed_imputation_scope s
JOIN catalog.instrument i USING (instrument_id)
JOIN derived.c2_market_bar_1h_imputation x
  ON x.policy_id=s.policy_id AND x.instrument_id=s.instrument_id
 AND x.time_utc=s.time_utc AND x.candidate_data_version=s.candidate_data_version
JOIN quality.event q
  ON q.rule_id='c2_confirmed_provider_gap_forward_fill'
 AND q.instrument_id=s.instrument_id AND q.time_utc=s.time_utc;

REVOKE ALL ON derived.c2_confirmed_imputation_scope FROM PUBLIC;
REVOKE ALL ON analytics.v_c2_confirmed_provider_gap_warning FROM PUBLIC;
GRANT SELECT ON analytics.v_c2_confirmed_provider_gap_warning
TO saxo_app_reader,saxo_analyst_reader;
