SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0017_quality_event_price_basis_derivation.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

UPDATE quality.event_rule_policy
SET price_basis = NULL
WHERE rule_id = 'db3_atomic_run_gate';

CREATE OR REPLACE FUNCTION quality.attach_event_lifecycle_defaults()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_policy quality.event_rule_policy%ROWTYPE;
    v_source_dataset_id TEXT;
    v_scope_kind TEXT := 'UNKNOWN';
    v_applicability TEXT := 'UNKNOWN';
    v_affected_layer TEXT;
    v_price_basis TEXT;
    v_policy_found BOOLEAN := FALSE;
BEGIN
    SELECT * INTO v_policy
    FROM quality.event_rule_policy p
    WHERE p.rule_id = NEW.rule_id;

    IF FOUND THEN
        v_policy_found := TRUE;
        v_scope_kind := v_policy.default_scope_kind;
        v_applicability := v_policy.default_applicability;
        v_affected_layer := v_policy.affected_layer;
        v_price_basis := v_policy.price_basis;
    END IF;

    IF NEW.ingestion_run_id IS NOT NULL THEN
        SELECT CASE WHEN COUNT(DISTINCT sf.source_dataset_id) = 1
                    THEN MIN(sf.source_dataset_id) END
          INTO v_source_dataset_id
        FROM ops.source_file sf
        JOIN catalog.source_dataset d
          ON d.source_dataset_id = sf.source_dataset_id
        WHERE sf.ingestion_run_id = NEW.ingestion_run_id
          AND (v_affected_layer IS NULL OR d.authoritative_layer = v_affected_layer);
    END IF;

    IF v_price_basis IS NULL AND NEW.instrument_id IS NOT NULL THEN
        SELECT CASE WHEN COUNT(DISTINCT w.price_basis) = 1
                    THEN MIN(w.price_basis) END
          INTO v_price_basis
        FROM ops.watermark w
        WHERE w.instrument_id = NEW.instrument_id
          AND (NEW.horizon_minutes IS NULL OR w.horizon_minutes = NEW.horizon_minutes);
    END IF;

    INSERT INTO quality.event_scope (
        quality_event_id, scope_kind, source_dataset_id, affected_layer,
        price_basis, scope_evidence, recorded_by
    ) VALUES (
        NEW.quality_event_id, v_scope_kind, v_source_dataset_id, v_affected_layer,
        v_price_basis,
        jsonb_build_object(
            'policy', 'quality.event_rule_policy',
            'rule_id', NEW.rule_id,
            'instrument_id', NEW.instrument_id,
            'ingestion_run_id', NEW.ingestion_run_id,
            'horizon_minutes', NEW.horizon_minutes
        ),
        'system:dmi1_event_default_v1'
    );

    INSERT INTO quality.event_applicability_review (
        quality_event_id, applicability, reason, superseded_by_ingestion_run_id, reviewed_by
    ) VALUES (
        NEW.quality_event_id,
        v_applicability,
        CASE
            WHEN v_policy_found THEN 'initial applicability from quality.event_rule_policy'
            ELSE 'unrecognized rule; fail-closed UNKNOWN pending operator review'
        END,
        NULL,
        'system:dmi1_event_default_v1'
    );
    RETURN NEW;
END
$$;
