SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0015_read_api_contract_hardening.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

CREATE TABLE quality.event_scope (
    event_scope_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    quality_event_id BIGINT NOT NULL REFERENCES quality.event(quality_event_id),
    scope_kind TEXT NOT NULL CHECK (
        scope_kind IN ('INSTRUMENT', 'SERIES', 'DATASET', 'RUN', 'LAYER', 'GLOBAL', 'UNKNOWN')
    ),
    source_dataset_id TEXT NULL REFERENCES catalog.source_dataset(source_dataset_id),
    affected_layer TEXT NULL CHECK (
        affected_layer IS NULL OR affected_layer IN ('raw', 'curated', 'derived', 'research_metadata')
    ),
    price_basis TEXT NULL,
    scope_evidence JSONB NOT NULL DEFAULT '{}'::jsonb,
    recorded_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    recorded_by TEXT NOT NULL CHECK (btrim(recorded_by) <> '')
);

CREATE TABLE quality.event_applicability_review (
    event_applicability_review_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    quality_event_id BIGINT NOT NULL REFERENCES quality.event(quality_event_id),
    applicability TEXT NOT NULL CHECK (applicability IN ('CURRENT', 'HISTORICAL', 'UNKNOWN')),
    reason TEXT NOT NULL CHECK (btrim(reason) <> ''),
    superseded_by_ingestion_run_id BIGINT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    reviewed_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    reviewed_by TEXT NOT NULL CHECK (btrim(reviewed_by) <> '')
);

CREATE INDEX event_scope_latest_idx
    ON quality.event_scope (quality_event_id, recorded_at_utc DESC, event_scope_id DESC);
CREATE INDEX event_applicability_review_latest_idx
    ON quality.event_applicability_review (
        quality_event_id, reviewed_at_utc DESC, event_applicability_review_id DESC
    );

REVOKE ALL ON quality.event_scope, quality.event_applicability_review FROM PUBLIC;
REVOKE ALL ON SEQUENCE quality.event_scope_event_scope_id_seq,
                       quality.event_applicability_review_event_applicability_review_id_seq
    FROM PUBLIC;

CREATE OR REPLACE PROCEDURE quality.record_event_scope(
    p_quality_event_id BIGINT,
    p_scope_kind TEXT,
    p_source_dataset_id TEXT,
    p_affected_layer TEXT,
    p_price_basis TEXT,
    p_scope_evidence JSONB,
    p_recorded_by TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF p_scope_kind NOT IN ('INSTRUMENT', 'SERIES', 'DATASET', 'RUN', 'LAYER', 'GLOBAL', 'UNKNOWN') THEN
        RAISE EXCEPTION 'invalid scope kind';
    END IF;
    IF p_affected_layer IS NOT NULL
       AND p_affected_layer NOT IN ('raw', 'curated', 'derived', 'research_metadata') THEN
        RAISE EXCEPTION 'invalid affected layer';
    END IF;
    IF btrim(COALESCE(p_recorded_by, '')) = '' THEN
        RAISE EXCEPTION 'recorded_by is required';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM quality.event e WHERE e.quality_event_id = p_quality_event_id
    ) THEN
        RAISE EXCEPTION 'quality event does not exist';
    END IF;

    INSERT INTO quality.event_scope (
        quality_event_id, scope_kind, source_dataset_id, affected_layer,
        price_basis, scope_evidence, recorded_by
    ) VALUES (
        p_quality_event_id, p_scope_kind, p_source_dataset_id, p_affected_layer,
        p_price_basis, COALESCE(p_scope_evidence, '{}'::jsonb), p_recorded_by
    );
END
$$;

CREATE OR REPLACE PROCEDURE quality.review_event_applicability(
    p_quality_event_id BIGINT,
    p_applicability TEXT,
    p_reason TEXT,
    p_superseded_by_ingestion_run_id BIGINT,
    p_reviewed_by TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF p_applicability NOT IN ('CURRENT', 'HISTORICAL', 'UNKNOWN') THEN
        RAISE EXCEPTION 'invalid applicability';
    END IF;
    IF btrim(COALESCE(p_reason, '')) = '' OR btrim(COALESCE(p_reviewed_by, '')) = '' THEN
        RAISE EXCEPTION 'reason and reviewed_by are required';
    END IF;
    IF p_applicability = 'CURRENT' AND p_superseded_by_ingestion_run_id IS NOT NULL THEN
        RAISE EXCEPTION 'CURRENT review cannot name a superseding ingestion run';
    END IF;
    IF NOT EXISTS (
        SELECT 1 FROM quality.event e WHERE e.quality_event_id = p_quality_event_id
    ) THEN
        RAISE EXCEPTION 'quality event does not exist';
    END IF;

    INSERT INTO quality.event_applicability_review (
        quality_event_id, applicability, reason, superseded_by_ingestion_run_id, reviewed_by
    ) VALUES (
        p_quality_event_id, p_applicability, p_reason,
        p_superseded_by_ingestion_run_id, p_reviewed_by
    );
END
$$;

REVOKE ALL ON PROCEDURE quality.record_event_scope(BIGINT, TEXT, TEXT, TEXT, TEXT, JSONB, TEXT)
    FROM PUBLIC;
REVOKE ALL ON PROCEDURE quality.review_event_applicability(BIGINT, TEXT, TEXT, BIGINT, TEXT)
    FROM PUBLIC;
GRANT EXECUTE ON PROCEDURE quality.record_event_scope(BIGINT, TEXT, TEXT, TEXT, TEXT, JSONB, TEXT)
    TO saxo_ops_operator;
GRANT EXECUTE ON PROCEDURE quality.review_event_applicability(BIGINT, TEXT, TEXT, BIGINT, TEXT)
    TO saxo_ops_operator;

CREATE OR REPLACE VIEW quality.v_event_status
WITH (security_barrier = true)
AS
SELECT
    e.quality_event_id,
    e.status,
    e.severity,
    e.rule_id,
    e.ingestion_run_id,
    e.instrument_id,
    i.market_key AS instrument_key,
    i.symbol,
    i.category,
    e.horizon_minutes,
    e.time_utc,
    e.action,
    e.created_at_utc,
    COALESCE(s.scope_kind, 'UNKNOWN')::TEXT AS scope_kind,
    s.source_dataset_id,
    s.affected_layer,
    s.price_basis,
    COALESCE(s.scope_evidence, '{}'::jsonb) AS scope_evidence,
    s.recorded_at_utc AS scope_recorded_at_utc,
    s.recorded_by AS scope_recorded_by,
    COALESCE(r.applicability, 'UNKNOWN')::TEXT AS applicability,
    r.reason AS applicability_reason,
    r.superseded_by_ingestion_run_id,
    r.reviewed_at_utc,
    r.reviewed_by,
    (
        e.status IN ('OPEN', 'ACKNOWLEDGED')
        AND e.severity IN ('ERROR', 'CRITICAL')
        AND COALESCE(r.applicability, 'UNKNOWN') IN ('CURRENT', 'UNKNOWN')
    ) AS current_blocker
FROM quality.event e
LEFT JOIN catalog.instrument i ON i.instrument_id = e.instrument_id
LEFT JOIN LATERAL (
    SELECT es.scope_kind, es.source_dataset_id, es.affected_layer, es.price_basis,
           es.scope_evidence, es.recorded_at_utc, es.recorded_by
    FROM quality.event_scope es
    WHERE es.quality_event_id = e.quality_event_id
    ORDER BY es.recorded_at_utc DESC, es.event_scope_id DESC
    LIMIT 1
) s ON TRUE
LEFT JOIN LATERAL (
    SELECT er.applicability, er.reason, er.superseded_by_ingestion_run_id,
           er.reviewed_at_utc, er.reviewed_by
    FROM quality.event_applicability_review er
    WHERE er.quality_event_id = e.quality_event_id
    ORDER BY er.reviewed_at_utc DESC, er.event_applicability_review_id DESC
    LIMIT 1
) r ON TRUE;

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
    created_at_utc,
    ingestion_run_id,
    instrument_key,
    symbol,
    category,
    horizon_minutes,
    scope_kind,
    source_dataset_id,
    affected_layer,
    price_basis,
    scope_evidence,
    scope_recorded_at_utc,
    scope_recorded_by,
    applicability,
    applicability_reason,
    superseded_by_ingestion_run_id,
    reviewed_at_utc,
    reviewed_by,
    current_blocker
FROM quality.v_event_status
WHERE status IN ('OPEN', 'ACKNOWLEDGED');

ALTER VIEW analytics.v_data_inventory RENAME TO v_data_inventory_base;
REVOKE ALL ON analytics.v_data_inventory_base FROM saxo_app_reader, saxo_analyst_reader;
CREATE VIEW analytics.v_data_inventory
WITH (security_barrier = true)
AS
SELECT base.*, i.market_key AS instrument_key
FROM analytics.v_data_inventory_base base
LEFT JOIN catalog.instrument i ON i.instrument_id = base.instrument_id;

CREATE OR REPLACE VIEW analytics.v_data_coverage
WITH (security_barrier = true)
AS
WITH actual AS (
    SELECT
        b.instrument_id,
        i.market_key,
        i.symbol,
        i.category,
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
    GROUP BY b.instrument_id, i.market_key, i.symbol, i.category, i.session_calendar_id,
             b.horizon_minutes, b.price_basis
), expected AS (
    SELECT
        a.instrument_id,
        a.price_basis,
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
    GROUP BY a.instrument_id, a.price_basis
), aligned AS (
    SELECT b.instrument_id, b.price_basis, COUNT(*)::BIGINT AS aligned_rows
    FROM curated.market_bar b
    JOIN catalog.instrument i ON i.instrument_id = b.instrument_id
    JOIN catalog.session_interval si
      ON si.session_calendar_id = i.session_calendar_id
     AND si.session_status <> 'HOLIDAY'
     AND b.time_utc >= si.open_time_utc
     AND b.time_utc < si.close_time_utc
     AND MOD(EXTRACT(EPOCH FROM (b.time_utc - si.open_time_utc))::BIGINT, 3600) = 0
    WHERE b.horizon_minutes = 60
    GROUP BY b.instrument_id, b.price_basis
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
    COALESCE(c.metadata_json->>'verification_status', 'UNASSIGNED')::TEXT AS calendar_verification_status,
    a.market_key AS instrument_key,
    a.category,
    a.price_basis
FROM actual a
LEFT JOIN expected e ON e.instrument_id = a.instrument_id AND e.price_basis = a.price_basis
LEFT JOIN aligned x ON x.instrument_id = a.instrument_id AND x.price_basis = a.price_basis
LEFT JOIN catalog.session_calendar c ON c.session_calendar_id = a.session_calendar_id;

CREATE OR REPLACE VIEW analytics.v_data_freshness
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
    END::TEXT AS freshness_status,
    i.market_key AS instrument_key
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

GRANT SELECT ON analytics.v_data_inventory, analytics.v_data_coverage,
                analytics.v_data_freshness, quality.v_event_status,
                quality.v_open_event
    TO saxo_app_reader;
GRANT SELECT ON analytics.v_data_inventory, analytics.v_data_coverage,
                analytics.v_data_freshness, quality.v_event_status,
                quality.v_open_event
    TO saxo_analyst_reader;
