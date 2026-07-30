SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0029_data_version_warning_review_policy.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

ALTER TABLE ops.data_version_revision_event
    ADD COLUMN policy_id TEXT NOT NULL
        DEFAULT 'bounded_data_version_reconciliation_v1',
    ADD COLUMN review_status TEXT NOT NULL DEFAULT 'LEGACY_STATE',
    ADD COLUMN reviewed_at_utc TIMESTAMPTZ NULL,
    ADD COLUMN reviewed_by TEXT NULL,
    ADD COLUMN review_note TEXT NULL;

ALTER TABLE ops.data_version_revision_event
    ADD CONSTRAINT data_version_revision_event_review_status_check CHECK (
        review_status IN (
            'LEGACY_STATE','PENDING_REVIEW','REVIEWED_KEEP_CURRENT',
            'APPLY_APPROVED','APPLIED'
        )
    ),
    ADD CONSTRAINT data_version_revision_event_review_audit_check CHECK (
        review_status IN ('LEGACY_STATE','PENDING_REVIEW') OR (
            reviewed_at_utc IS NOT NULL
            AND length(btrim(reviewed_by)) BETWEEN 1 AND 128
            AND length(btrim(review_note)) BETWEEN 1 AND 2000
        )
    );

ALTER TABLE ops.data_version_revision_event
    DROP CONSTRAINT data_version_revision_event_reconciliation_status_check,
    ADD CONSTRAINT data_version_revision_event_reconciliation_status_check CHECK (
        reconciliation_status IN (
            'DETECTED','DISCOVERING','REVIEW_PENDING','READY_TO_APPLY',
            'APPLIED','BLOCKED_FULL_REFETCH','FAILED'
        )
    );

ALTER TABLE ops.data_version_revision_step
    DROP CONSTRAINT data_version_revision_step_decision_check,
    ADD CONSTRAINT data_version_revision_step_decision_check CHECK (
        decision IN (
            'WARNING_RECORDED','EXPAND','READY_TO_APPLY','BLOCKED_FULL_REFETCH'
        )
    );

DROP INDEX ops.data_version_revision_open_version_idx;
CREATE UNIQUE INDEX data_version_revision_open_version_idx
    ON ops.data_version_revision_event (
        instrument_id,horizon_minutes,price_basis,new_data_version
    )
    WHERE reconciliation_status IN (
        'DETECTED','DISCOVERING','REVIEW_PENDING','READY_TO_APPLY'
    );

CREATE OR REPLACE VIEW ops.v_data_version_revision_state
WITH (security_barrier = true)
AS
SELECT
    i.market_key AS instrument_key,
    e.revision_event_id,
    e.instrument_id,
    e.horizon_minutes,
    e.price_basis,
    e.old_data_version,
    e.new_data_version,
    e.reconciliation_status,
    CASE
        WHEN e.policy_id='data_version_revision_warning_v2'
             AND e.reconciliation_status='REVIEW_PENDING'
            THEN 'AVAILABLE_WITH_REVISION_WARNING'
        WHEN e.reconciliation_status='APPLIED' THEN 'AVAILABLE'
        WHEN e.reconciliation_status IN ('DETECTED','DISCOVERING','READY_TO_APPLY')
            THEN 'RECONCILING'
        ELSE 'BLOCKED'
    END::TEXT AS availability_status,
    e.comparison_from_utc,
    e.comparison_to_utc,
    e.compared_rows,
    e.content_difference_rows,
    e.version_only_rows,
    e.new_rows,
    e.removed_rows,
    e.affected_from_utc,
    e.affected_to_utc,
    e.stable_anchor_rows,
    e.reason_code,
    e.detected_ingestion_run_id,
    e.applied_ingestion_run_id,
    e.discovery_manifest_relative_path,
    e.discovery_manifest_sha256,
    e.replacement_result,
    e.created_at_utc,
    e.updated_at_utc,
    e.policy_id,
    e.review_status,
    e.reviewed_at_utc,
    e.reviewed_by,
    e.review_note,
    latest_step.request_time_utc AS latest_evidence_at_utc,
    latest_step.compared_to_utc AS latest_provider_observed_time_utc,
    latest_step.step_number AS evidence_sample_count
FROM catalog.instrument i
JOIN LATERAL (
    SELECT selected.*
    FROM ops.data_version_revision_event selected
    WHERE selected.instrument_id=i.instrument_id
    ORDER BY selected.revision_event_id DESC
    LIMIT 1
) e ON TRUE
LEFT JOIN LATERAL (
    SELECT selected_step.*
    FROM ops.data_version_revision_step selected_step
    WHERE selected_step.revision_event_id=e.revision_event_id
    ORDER BY selected_step.step_number DESC
    LIMIT 1
) latest_step ON TRUE;

CREATE OR REPLACE VIEW ops.v_series_revision_availability
WITH (security_barrier = true)
AS
SELECT
    i.market_key AS instrument_key,
    w.horizon_minutes,
    w.price_basis,
    w.data_status,
    COALESCE(
        r.availability_status,
        CASE WHEN w.data_status='ACTIVE' THEN 'AVAILABLE' ELSE 'BLOCKED' END
    )::TEXT AS availability_status,
    r.reconciliation_status,
    r.reason_code,
    r.old_data_version,
    r.new_data_version,
    r.revision_event_id,
    w.data_version AS last_accepted_data_version,
    w.latest_seen_time_utc AS last_accepted_seen_time_utc,
    w.latest_complete_time_utc AS last_accepted_complete_time_utc,
    w.last_ingestion_run_id AS last_accepted_ingestion_run_id,
    r.policy_id,
    r.review_status,
    r.latest_evidence_at_utc,
    r.latest_provider_observed_time_utc,
    r.evidence_sample_count
FROM catalog.instrument i
JOIN ops.watermark w USING (instrument_id)
LEFT JOIN ops.v_data_version_revision_state r
  ON r.instrument_id=i.instrument_id
 AND r.horizon_minutes=w.horizon_minutes
 AND r.price_basis=w.price_basis
WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
  AND i.active_to_utc IS NULL;

CREATE OR REPLACE PROCEDURE ops.review_data_version_revision(
    IN p_revision_event_id BIGINT,
    IN p_decision TEXT,
    IN p_reviewer TEXT,
    IN p_note TEXT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    selected_review_status TEXT;
BEGIN
    IF SESSION_USER <> 'saxo_ops_operator' THEN
        RAISE EXCEPTION 'revision review is restricted to saxo_ops_operator';
    END IF;
    IF p_decision NOT IN ('KEEP_CURRENT','APPROVE_APPLY')
       OR length(btrim(p_reviewer)) NOT BETWEEN 1 AND 128
       OR length(btrim(p_note)) NOT BETWEEN 1 AND 2000 THEN
        RAISE EXCEPTION 'invalid revision review decision or audit metadata';
    END IF;

    UPDATE ops.data_version_revision_event SET
        review_status=CASE
            WHEN p_decision='KEEP_CURRENT' THEN 'REVIEWED_KEEP_CURRENT'
            ELSE 'APPLY_APPROVED'
        END,
        reviewed_at_utc=clock_timestamp(),
        reviewed_by=p_reviewer,
        review_note=p_note,
        updated_at_utc=clock_timestamp()
    WHERE revision_event_id=p_revision_event_id
      AND policy_id='data_version_revision_warning_v2'
      AND reconciliation_status='REVIEW_PENDING'
      AND review_status IN ('PENDING_REVIEW','REVIEWED_KEEP_CURRENT')
    RETURNING review_status INTO selected_review_status;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'revision review guard condition failed';
    END IF;
END
$$;

CREATE OR REPLACE PROCEDURE curated.prepare_bounded_revision(
    IN p_ingestion_run_id BIGINT,
    IN p_revision_event_id BIGINT,
    IN p_instrument_id BIGINT,
    IN p_affected_from_utc TIMESTAMPTZ,
    IN p_affected_to_utc TIMESTAMPTZ
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    selected_trigger TEXT;
    selected_run_status TEXT;
    selected_data_status TEXT;
    selected_revision_status TEXT;
    selected_revision_instrument BIGINT;
    selected_from TIMESTAMPTZ;
    selected_to TIMESTAMPTZ;
    selected_policy_id TEXT;
    selected_review_status TEXT;
BEGIN
    IF SESSION_USER <> 'saxo_ingest' THEN
        RAISE EXCEPTION 'bounded revision procedure is restricted to saxo_ingest';
    END IF;

    SELECT r.trigger,r.status,w.data_status,e.reconciliation_status,
           e.instrument_id,e.affected_from_utc,e.affected_to_utc,
           e.policy_id,e.review_status
      INTO selected_trigger,selected_run_status,selected_data_status,
           selected_revision_status,selected_revision_instrument,
           selected_from,selected_to,selected_policy_id,selected_review_status
    FROM ops.ingestion_run r
    JOIN ops.data_version_revision_event e
      ON e.revision_event_id=p_revision_event_id
    JOIN ops.watermark w
      ON w.instrument_id=p_instrument_id
     AND w.horizon_minutes=60
     AND w.price_basis=e.price_basis
    WHERE r.ingestion_run_id=p_ingestion_run_id
    FOR UPDATE OF r,w,e;

    IF NOT FOUND
       OR selected_trigger NOT IN (
           'manual_db3_bounded_revision','manual_db3_approved_revision_apply'
       )
       OR selected_run_status <> 'RUNNING'
       OR selected_revision_status <> 'READY_TO_APPLY'
       OR selected_revision_instrument <> p_instrument_id
       OR selected_from IS DISTINCT FROM p_affected_from_utc
       OR selected_to IS DISTINCT FROM p_affected_to_utc
       OR NOT (
           (
               selected_policy_id='bounded_data_version_reconciliation_v1'
               AND selected_data_status='STALE_DATA_VERSION'
           ) OR (
               selected_policy_id='data_version_revision_warning_v2'
               AND selected_data_status='ACTIVE'
               AND selected_review_status='APPLY_APPROVED'
           )
       ) THEN
        RAISE EXCEPTION 'bounded revision guard condition failed';
    END IF;

    DELETE FROM curated.market_bar
    WHERE instrument_id=p_instrument_id
      AND horizon_minutes=60
      AND time_utc BETWEEN p_affected_from_utc AND p_affected_to_utc;
END
$$;

REVOKE ALL ON PROCEDURE ops.review_data_version_revision(BIGINT,TEXT,TEXT,TEXT)
    FROM PUBLIC;
GRANT EXECUTE ON PROCEDURE ops.review_data_version_revision(BIGINT,TEXT,TEXT,TEXT)
    TO saxo_ops_operator;
GRANT SELECT ON ops.v_data_version_revision_state,ops.v_series_revision_availability
    TO saxo_app_reader,saxo_analyst_reader,saxo_ops_operator;
