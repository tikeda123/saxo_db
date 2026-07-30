SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0024_fx_run_scope_and_event_supersession.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

-- Immutable selected-series evidence for every acquisition run.  The copied
-- identity prevents a later catalog edit from changing the historical scope.
CREATE TABLE ops.ingestion_run_instrument_scope (
    ingestion_run_id BIGINT NOT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(instrument_id),
    instrument_key TEXT NOT NULL,
    uic BIGINT NOT NULL,
    asset_type TEXT NOT NULL,
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes > 0),
    price_basis TEXT NOT NULL,
    recorded_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (ingestion_run_id, instrument_id, horizon_minutes, price_basis)
);

CREATE INDEX ingestion_run_instrument_scope_lookup_idx
    ON ops.ingestion_run_instrument_scope (instrument_key, horizon_minutes, price_basis, ingestion_run_id);

REVOKE ALL ON ops.ingestion_run_instrument_scope FROM PUBLIC;
GRANT SELECT, INSERT ON ops.ingestion_run_instrument_scope TO saxo_ingest;
GRANT SELECT ON ops.ingestion_run_instrument_scope
    TO saxo_app_reader, saxo_analyst_reader, saxo_ops_operator;

-- Preserve old runs while recovering their intended series scope from the
-- immutable requested_series UIC/AssetType evidence.
INSERT INTO ops.ingestion_run_instrument_scope (
    ingestion_run_id,instrument_id,instrument_key,uic,asset_type,horizon_minutes,price_basis
)
SELECT DISTINCT
    r.ingestion_run_id,
    i.instrument_id,
    lower(i.market_key),
    i.uic,
    i.asset_type,
    COALESCE((requested.item->>'horizon_minutes')::SMALLINT,60),
    COALESCE(requested.item->>'price_basis',w.price_basis)
FROM ops.ingestion_run r
CROSS JOIN LATERAL jsonb_array_elements(r.requested_series) requested(item)
JOIN catalog.instrument i
  ON i.provider='Saxo OpenAPI'
 AND i.environment=r.environment
 AND i.uic=(requested.item->>'uic')::BIGINT
 AND i.asset_type=requested.item->>'asset_type'
LEFT JOIN ops.watermark w
  ON w.instrument_id=i.instrument_id
 AND w.horizon_minutes=COALESCE((requested.item->>'horizon_minutes')::SMALLINT,60)
WHERE jsonb_typeof(r.requested_series)='array'
  AND requested.item ? 'uic'
  AND requested.item ? 'asset_type'
  AND COALESCE(requested.item->>'price_basis',w.price_basis) IS NOT NULL
ON CONFLICT DO NOTHING;

-- Append corrected RUN scope for old instrument_id=NULL atomic events.  Base
-- quality.event rows and their original lifecycle rows stay immutable.
WITH selected_scope AS (
    SELECT
        s.ingestion_run_id,
        jsonb_agg(s.instrument_key ORDER BY s.instrument_key) AS instrument_keys,
        jsonb_agg(s.instrument_id ORDER BY s.instrument_key) AS instrument_ids,
        CASE WHEN COUNT(DISTINCT s.price_basis)=1 THEN MIN(s.price_basis) END AS price_basis
    FROM ops.ingestion_run_instrument_scope s
    GROUP BY s.ingestion_run_id
)
INSERT INTO quality.event_scope (
    quality_event_id,scope_kind,source_dataset_id,affected_layer,price_basis,
    scope_evidence,recorded_by
)
SELECT
    e.quality_event_id,
    'RUN',
    NULL,
    'curated',
    selected_scope.price_basis,
    jsonb_build_object(
        'policy','fx_run_scope_remediation_v1',
        'ingestion_run_id',e.ingestion_run_id,
        'selected_instrument_keys',selected_scope.instrument_keys,
        'selected_instrument_ids',selected_scope.instrument_ids,
        'original_event_preserved',TRUE
    ),
    'system:fx_run_scope_remediation_v1'
FROM quality.event e
JOIN selected_scope USING (ingestion_run_id)
WHERE e.rule_id='db3_atomic_run_gate'
  AND e.instrument_id IS NULL;

-- A successful atomic run supersedes only earlier atomic failures applicable
-- to the same selected instruments.  This covers scheduled subsets, guarded
-- single-instrument refetches, and the full canonical universe uniformly.
CREATE OR REPLACE FUNCTION quality.supersede_atomic_run_events_after_pass()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF NEW.status <> 'PASS' OR OLD.status = 'PASS' THEN
        RETURN NEW;
    END IF;

    INSERT INTO quality.event_applicability_review (
        quality_event_id,applicability,reason,
        superseded_by_ingestion_run_id,reviewed_by
    )
    SELECT DISTINCT
        event_status.quality_event_id,
        'HISTORICAL',
        'later PASS for the same immutable run instrument scope superseded the earlier atomic failure',
        NEW.ingestion_run_id,
        'system:fx_run_scope_supersession_v1'
    FROM quality.v_event_status event_status
    JOIN ops.ingestion_run_instrument_scope selected
      ON selected.ingestion_run_id=NEW.ingestion_run_id
     AND (
          event_status.instrument_id=selected.instrument_id
          OR (
              event_status.instrument_id IS NULL
              AND event_status.scope_kind='RUN'
              AND event_status.scope_evidence ? 'selected_instrument_keys'
              AND (event_status.scope_evidence->'selected_instrument_keys') ? selected.instrument_key
          )
     )
    WHERE event_status.rule_id='db3_atomic_run_gate'
      AND event_status.status IN ('OPEN','ACKNOWLEDGED')
      AND event_status.applicability IN ('CURRENT','UNKNOWN')
      AND event_status.ingestion_run_id < NEW.ingestion_run_id;

    RETURN NEW;
END
$$;

-- Ensure the latest lifecycle scope of new atomic events is instrument-bound
-- when instrument_id exists, otherwise RUN-bound to the immutable run scope.
CREATE OR REPLACE FUNCTION quality.attach_fx_run_scope_after_event()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    selected_keys JSONB;
    selected_ids JSONB;
    selected_basis TEXT;
BEGIN
    IF NEW.rule_id <> 'db3_atomic_run_gate' OR NEW.ingestion_run_id IS NULL THEN
        RETURN NEW;
    END IF;

    SELECT
        jsonb_agg(s.instrument_key ORDER BY s.instrument_key),
        jsonb_agg(s.instrument_id ORDER BY s.instrument_key),
        CASE WHEN COUNT(DISTINCT s.price_basis)=1 THEN MIN(s.price_basis) END
      INTO selected_keys,selected_ids,selected_basis
    FROM ops.ingestion_run_instrument_scope s
    WHERE s.ingestion_run_id=NEW.ingestion_run_id;

    INSERT INTO quality.event_scope (
        quality_event_id,scope_kind,source_dataset_id,affected_layer,price_basis,
        scope_evidence,recorded_by
    ) VALUES (
        NEW.quality_event_id,
        CASE WHEN NEW.instrument_id IS NULL THEN 'RUN' ELSE 'INSTRUMENT' END,
        NULL,
        'curated',
        selected_basis,
        jsonb_build_object(
            'policy','fx_run_scope_v1',
            'ingestion_run_id',NEW.ingestion_run_id,
            'instrument_id',NEW.instrument_id,
            'selected_instrument_keys',COALESCE(selected_keys,'[]'::jsonb),
            'selected_instrument_ids',COALESCE(selected_ids,'[]'::jsonb)
        ),
        'system:fx_run_scope_v1'
    );
    RETURN NEW;
END
$$;

CREATE TRIGGER zz_quality_event_fx_run_scope
AFTER INSERT ON quality.event
FOR EACH ROW EXECUTE FUNCTION quality.attach_fx_run_scope_after_event();
