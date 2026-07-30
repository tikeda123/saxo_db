SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0025_fx_gap_evidence_lookup.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

-- R5 joins expected slots back to immutable raw revisions and quarantine
-- evidence. These indexes alter no observation or lifecycle state.
CREATE INDEX raw_market_bar_revision_instrument_time_evidence_idx
    ON raw.market_bar_revision (instrument_id,horizon_minutes,time_utc,price_basis,ingestion_run_id);

CREATE INDEX quality_event_instrument_time_rule_evidence_idx
    ON quality.event (instrument_id,horizon_minutes,time_utc,rule_id,quality_event_id)
    WHERE time_utc IS NOT NULL;
