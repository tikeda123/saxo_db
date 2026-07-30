SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0027_bounded_data_version_reconciliation.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

CREATE TABLE ops.data_version_revision_event (
    revision_event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(instrument_id),
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes = 60),
    price_basis TEXT NOT NULL,
    detected_ingestion_run_id BIGINT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    applied_ingestion_run_id BIGINT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    old_data_version BIGINT NOT NULL,
    new_data_version BIGINT NOT NULL,
    reconciliation_status TEXT NOT NULL CHECK (
        reconciliation_status IN (
            'DETECTED','DISCOVERING','READY_TO_APPLY','APPLIED',
            'BLOCKED_FULL_REFETCH','FAILED'
        )
    ),
    comparison_from_utc TIMESTAMPTZ NULL,
    comparison_to_utc TIMESTAMPTZ NULL,
    compared_rows BIGINT NOT NULL DEFAULT 0 CHECK (compared_rows >= 0),
    content_difference_rows BIGINT NOT NULL DEFAULT 0 CHECK (content_difference_rows >= 0),
    version_only_rows BIGINT NOT NULL DEFAULT 0 CHECK (version_only_rows >= 0),
    new_rows BIGINT NOT NULL DEFAULT 0 CHECK (new_rows >= 0),
    removed_rows BIGINT NOT NULL DEFAULT 0 CHECK (removed_rows >= 0),
    affected_from_utc TIMESTAMPTZ NULL,
    affected_to_utc TIMESTAMPTZ NULL,
    stable_anchor_rows INTEGER NOT NULL DEFAULT 0 CHECK (stable_anchor_rows >= 0),
    reason_code TEXT NULL,
    discovery_manifest_relative_path TEXT NULL CHECK (
        discovery_manifest_relative_path IS NULL OR (
            discovery_manifest_relative_path !~ '^/' AND
            discovery_manifest_relative_path !~ '^[A-Za-z]:' AND
            discovery_manifest_relative_path !~ '(^|/)\.\.(/|$)'
        )
    ),
    discovery_manifest_sha256 CHAR(64) NULL CHECK (
        discovery_manifest_sha256 IS NULL OR discovery_manifest_sha256 ~ '^[0-9a-f]{64}$'
    ),
    replacement_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (old_data_version <> new_data_version),
    CHECK (
        (comparison_from_utc IS NULL AND comparison_to_utc IS NULL) OR
        (comparison_from_utc IS NOT NULL AND comparison_to_utc IS NOT NULL
         AND comparison_from_utc <= comparison_to_utc)
    ),
    CHECK (
        (affected_from_utc IS NULL AND affected_to_utc IS NULL) OR
        (affected_from_utc IS NOT NULL AND affected_to_utc IS NOT NULL
         AND affected_from_utc <= affected_to_utc)
    ),
    CHECK (
        reconciliation_status <> 'APPLIED' OR (
            applied_ingestion_run_id IS NOT NULL
            AND discovery_manifest_relative_path IS NOT NULL
            AND discovery_manifest_sha256 IS NOT NULL
        )
    )
);

CREATE UNIQUE INDEX data_version_revision_open_version_idx
    ON ops.data_version_revision_event (
        instrument_id,horizon_minutes,price_basis,new_data_version
    )
    WHERE reconciliation_status IN ('DETECTED','DISCOVERING','READY_TO_APPLY');

CREATE INDEX data_version_revision_series_idx
    ON ops.data_version_revision_event (
        instrument_id,horizon_minutes,price_basis,revision_event_id DESC
    );

CREATE TABLE ops.data_version_revision_step (
    revision_event_id BIGINT NOT NULL
        REFERENCES ops.data_version_revision_event(revision_event_id),
    step_number SMALLINT NOT NULL CHECK (step_number > 0),
    requested_count INTEGER NOT NULL CHECK (requested_count BETWEEN 1 AND 1200),
    request_mode TEXT NOT NULL CHECK (request_mode IN ('From','UpTo')),
    request_time_utc TIMESTAMPTZ NOT NULL,
    compared_from_utc TIMESTAMPTZ NOT NULL,
    compared_to_utc TIMESTAMPTZ NOT NULL,
    provider_rows INTEGER NOT NULL CHECK (provider_rows >= 0),
    matched_rows INTEGER NOT NULL CHECK (matched_rows >= 0),
    content_difference_rows INTEGER NOT NULL CHECK (content_difference_rows >= 0),
    version_only_rows INTEGER NOT NULL CHECK (version_only_rows >= 0),
    new_rows INTEGER NOT NULL CHECK (new_rows >= 0),
    removed_rows INTEGER NOT NULL CHECK (removed_rows >= 0),
    stable_anchor_rows INTEGER NOT NULL CHECK (stable_anchor_rows >= 0),
    decision TEXT NOT NULL CHECK (
        decision IN ('EXPAND','READY_TO_APPLY','BLOCKED_FULL_REFETCH')
    ),
    reason_code TEXT NOT NULL,
    artifact_relative_path TEXT NOT NULL CHECK (
        artifact_relative_path !~ '^/' AND
        artifact_relative_path !~ '^[A-Za-z]:' AND
        artifact_relative_path !~ '(^|/)\.\.(/|$)'
    ),
    artifact_sha256 CHAR(64) NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (revision_event_id,step_number),
    CHECK (compared_from_utc <= compared_to_utc)
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
    e.updated_at_utc
FROM catalog.instrument i
JOIN LATERAL (
    SELECT selected.*
    FROM ops.data_version_revision_event selected
    WHERE selected.instrument_id=i.instrument_id
    ORDER BY selected.revision_event_id DESC
    LIMIT 1
) e ON TRUE;

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
BEGIN
    IF SESSION_USER <> 'saxo_ingest' THEN
        RAISE EXCEPTION 'bounded revision procedure is restricted to saxo_ingest';
    END IF;

    SELECT r.trigger,r.status,w.data_status,e.reconciliation_status,
           e.instrument_id,e.affected_from_utc,e.affected_to_utc
      INTO selected_trigger,selected_run_status,selected_data_status,
           selected_revision_status,selected_revision_instrument,
           selected_from,selected_to
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
           'manual_db3_bounded_revision','scheduled_db3_bounded_revision'
       )
       OR selected_run_status <> 'RUNNING'
       OR selected_data_status <> 'STALE_DATA_VERSION'
       OR selected_revision_status <> 'READY_TO_APPLY'
       OR selected_revision_instrument <> p_instrument_id
       OR selected_from IS DISTINCT FROM p_affected_from_utc
       OR selected_to IS DISTINCT FROM p_affected_to_utc THEN
        RAISE EXCEPTION 'bounded revision guard condition failed';
    END IF;

    DELETE FROM curated.market_bar
    WHERE instrument_id=p_instrument_id
      AND horizon_minutes=60
      AND time_utc BETWEEN p_affected_from_utc AND p_affected_to_utc;
END
$$;

REVOKE ALL ON ops.data_version_revision_event FROM PUBLIC;
REVOKE ALL ON ops.data_version_revision_step FROM PUBLIC;
REVOKE ALL ON PROCEDURE curated.prepare_bounded_revision(
    BIGINT,BIGINT,BIGINT,TIMESTAMPTZ,TIMESTAMPTZ
) FROM PUBLIC;

GRANT SELECT,INSERT,UPDATE ON ops.data_version_revision_event TO saxo_ingest;
GRANT SELECT,INSERT ON ops.data_version_revision_step TO saxo_ingest;
GRANT USAGE,SELECT ON SEQUENCE ops.data_version_revision_event_revision_event_id_seq
    TO saxo_ingest;
GRANT EXECUTE ON PROCEDURE curated.prepare_bounded_revision(
    BIGINT,BIGINT,BIGINT,TIMESTAMPTZ,TIMESTAMPTZ
) TO saxo_ingest;
GRANT SELECT ON ops.v_data_version_revision_state TO saxo_app_reader,saxo_analyst_reader;
