SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0016_quality_event_lifecycle.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

CREATE TABLE quality.event_rule_policy (
    rule_id TEXT PRIMARY KEY,
    default_scope_kind TEXT NOT NULL CHECK (
        default_scope_kind IN ('INSTRUMENT', 'SERIES', 'DATASET', 'RUN', 'LAYER', 'GLOBAL', 'UNKNOWN')
    ),
    default_applicability TEXT NOT NULL CHECK (
        default_applicability IN ('CURRENT', 'HISTORICAL', 'UNKNOWN')
    ),
    affected_layer TEXT NULL CHECK (
        affected_layer IS NULL OR affected_layer IN ('raw', 'curated', 'derived', 'research_metadata')
    ),
    price_basis TEXT NULL,
    blocking_severities TEXT[] NOT NULL,
    supersession_condition TEXT NOT NULL,
    automatic_review_allowed BOOLEAN NOT NULL,
    operator_review_required BOOLEAN NOT NULL,
    CHECK (blocking_severities <@ ARRAY['INFO','WARN','ERROR','CRITICAL']::TEXT[])
);

INSERT INTO quality.event_rule_policy (
    rule_id, default_scope_kind, default_applicability, affected_layer, price_basis,
    blocking_severities, supersession_condition, automatic_review_allowed,
    operator_review_required
) VALUES
    (
        'source_series_quality_gate', 'SERIES', 'CURRENT', 'raw', 'native_ohlc',
        ARRAY['ERROR','CRITICAL']::TEXT[],
        'operator must review source archive evidence; canonical refresh alone does not supersede it',
        FALSE, TRUE
    ),
    (
        'db3_atomic_run_gate', 'RUN', 'CURRENT', 'curated', 'native_ohlc',
        ARRAY['ERROR','CRITICAL']::TEXT[],
        'same-instrument full-refetch PASS or all-13-series normal PASS after the failed run',
        TRUE, FALSE
    ),
    (
        'db3_fx_crossed_extrema_quarantine', 'SERIES', 'HISTORICAL', 'raw', 'native_ohlc',
        ARRAY[]::TEXT[], 'resolved audit event at creation', TRUE, FALSE
    ),
    (
        'db3_historical_revision', 'SERIES', 'HISTORICAL', 'curated', 'native_ohlc',
        ARRAY[]::TEXT[], 'resolved audit event at creation', TRUE, FALSE
    ),
    (
        'db3_full_refetch_removed_observations', 'SERIES', 'HISTORICAL', 'curated', 'native_ohlc',
        ARRAY[]::TEXT[], 'resolved audit event at creation', TRUE, FALSE
    );

REVOKE ALL ON quality.event_rule_policy FROM PUBLIC;

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

CREATE TRIGGER quality_event_lifecycle_defaults
AFTER INSERT ON quality.event
FOR EACH ROW EXECUTE FUNCTION quality.attach_event_lifecycle_defaults();

CREATE OR REPLACE FUNCTION quality.supersede_atomic_run_events_after_pass()
RETURNS TRIGGER
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    v_instrument_id BIGINT;
    v_instrument_count INTEGER;
BEGIN
    IF NEW.status <> 'PASS' OR OLD.status = 'PASS' THEN
        RETURN NEW;
    END IF;

    IF NEW.trigger = 'manual_db3_full_refetch' AND NEW.successful_series = 1 THEN
        SELECT MIN(r.instrument_id), COUNT(DISTINCT r.instrument_id)
          INTO v_instrument_id, v_instrument_count
        FROM raw.market_bar_revision r
        WHERE r.ingestion_run_id = NEW.ingestion_run_id;

        IF v_instrument_count = 1 THEN
            INSERT INTO quality.event_applicability_review (
                quality_event_id, applicability, reason,
                superseded_by_ingestion_run_id, reviewed_by
            )
            SELECT
                s.quality_event_id, 'HISTORICAL',
                'same-instrument full-refetch PASS superseded the earlier atomic run failure',
                NEW.ingestion_run_id, 'system:dmi1_atomic_supersession_v1'
            FROM quality.v_event_status s
            WHERE s.rule_id = 'db3_atomic_run_gate'
              AND s.status IN ('OPEN', 'ACKNOWLEDGED')
              AND s.applicability IN ('CURRENT', 'UNKNOWN')
              AND s.ingestion_run_id < NEW.ingestion_run_id
              AND s.instrument_id = v_instrument_id;
        END IF;
    ELSIF NEW.trigger = 'manual_db3' AND NEW.successful_series = 13 THEN
        INSERT INTO quality.event_applicability_review (
            quality_event_id, applicability, reason,
            superseded_by_ingestion_run_id, reviewed_by
        )
        SELECT
            s.quality_event_id, 'HISTORICAL',
            'all-13-series normal PASS superseded the earlier atomic run failure',
            NEW.ingestion_run_id, 'system:dmi1_atomic_supersession_v1'
        FROM quality.v_event_status s
        WHERE s.rule_id = 'db3_atomic_run_gate'
          AND s.status IN ('OPEN', 'ACKNOWLEDGED')
          AND s.applicability IN ('CURRENT', 'UNKNOWN')
          AND s.ingestion_run_id < NEW.ingestion_run_id;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER ingestion_run_quality_event_supersession
AFTER UPDATE OF status ON ops.ingestion_run
FOR EACH ROW EXECUTE FUNCTION quality.supersede_atomic_run_events_after_pass();

GRANT SELECT ON quality.event_rule_policy TO saxo_app_reader, saxo_analyst_reader;
