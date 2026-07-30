SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0026_fx_research_candidate_publication.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

INSERT INTO catalog.source_dataset (
    source_dataset_id,dataset_name,provider,environment,dataset_kind,price_basis,
    canonical_horizon_minutes,expected_update_interval_seconds,
    freshness_grace_seconds,authoritative_layer,research_eligibility,active,
    source_manifest_relative_path,metadata_json
) VALUES (
    'saxo_sim_fx_research_candidates_60m_v1',
    'Saxo SIM FX research candidates 60m chart',
    'Saxo OpenAPI','SIM','raw_market','bid_ask_mid',60,3600,7200,'raw',
    'SIM_RESEARCH_CANDIDATE',TRUE,
    'specs/source_collection/fx_research_candidates_v1.json',
    jsonb_build_object(
        'instrument_keys',jsonb_build_array('audusd','usdcad','usdchf'),
        'publication_gate','fail_closed',
        'orders_or_prechecks',0
    )
)
ON CONFLICT (source_dataset_id) DO UPDATE SET
    dataset_name=EXCLUDED.dataset_name,
    provider=EXCLUDED.provider,
    environment=EXCLUDED.environment,
    dataset_kind=EXCLUDED.dataset_kind,
    price_basis=EXCLUDED.price_basis,
    canonical_horizon_minutes=EXCLUDED.canonical_horizon_minutes,
    expected_update_interval_seconds=EXCLUDED.expected_update_interval_seconds,
    freshness_grace_seconds=EXCLUDED.freshness_grace_seconds,
    authoritative_layer=EXCLUDED.authoritative_layer,
    research_eligibility=EXCLUDED.research_eligibility,
    active=EXCLUDED.active,
    source_manifest_relative_path=EXCLUDED.source_manifest_relative_path,
    metadata_json=EXCLUDED.metadata_json;

INSERT INTO catalog.instrument (
    provider,environment,market_key,symbol,uic,asset_type,category,currency,
    exchange_id,session_calendar_id,active_from_utc
)
VALUES
    ('Saxo OpenAPI','SIM','audusd','AUDUSD',4,'FxSpot','fx','USD',NULL,NULL,'2002-09-25T02:40:00Z'),
    ('Saxo OpenAPI','SIM','usdcad','USDCAD',38,'FxSpot','fx','CAD',NULL,NULL,'2002-09-25T02:40:00Z'),
    ('Saxo OpenAPI','SIM','usdchf','USDCHF',39,'FxSpot','fx','CHF',NULL,NULL,'2002-09-25T02:40:00Z')
ON CONFLICT (provider,environment,uic,asset_type) DO NOTHING;

DO $$
DECLARE
    valid_rows INTEGER;
    keyed_rows INTEGER;
BEGIN
    SELECT COUNT(*) INTO valid_rows
    FROM catalog.instrument
    WHERE provider='Saxo OpenAPI' AND environment='SIM' AND active_to_utc IS NULL
      AND (
        (market_key='audusd' AND symbol='AUDUSD' AND uic=4 AND asset_type='FxSpot' AND currency='USD') OR
        (market_key='usdcad' AND symbol='USDCAD' AND uic=38 AND asset_type='FxSpot' AND currency='CAD') OR
        (market_key='usdchf' AND symbol='USDCHF' AND uic=39 AND asset_type='FxSpot' AND currency='CHF')
      );
    SELECT COUNT(*) INTO keyed_rows
    FROM catalog.instrument
    WHERE provider='Saxo OpenAPI' AND environment='SIM'
      AND market_key IN ('audusd','usdcad','usdchf') AND active_to_utc IS NULL;
    IF valid_rows <> 3 OR keyed_rows <> 3 THEN
        RAISE EXCEPTION 'FX_RESEARCH_CANDIDATE_IDENTITY_MISMATCH';
    END IF;
END
$$;

CREATE TABLE catalog.series_publication_state (
    instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(instrument_id),
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes = 60),
    price_basis TEXT NOT NULL,
    publication_status TEXT NOT NULL CHECK (
        publication_status IN ('CANDIDATE','STAGING','PUBLISHED','BLOCKED')
    ),
    quality_status TEXT NOT NULL CHECK (
        quality_status IN ('NOT_EVALUATED','PASS','WARN','FAIL')
    ),
    coverage_status TEXT NOT NULL CHECK (
        coverage_status IN ('NOT_EVALUATED','PASS','WARN','FAIL','UNKNOWN')
    ),
    freshness_status TEXT NOT NULL CHECK (
        freshness_status IN ('NOT_EVALUATED','PASS','STALE','FAIL','UNKNOWN')
    ),
    blocker_code TEXT NULL,
    last_evaluated_run_id BIGINT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    evidence_manifest_relative_path TEXT NULL CHECK (
        evidence_manifest_relative_path IS NULL OR (
            evidence_manifest_relative_path !~ '^/' AND
            evidence_manifest_relative_path !~ '^[A-Za-z]:' AND
            evidence_manifest_relative_path !~ '(^|/)\.\.(/|$)'
        )
    ),
    evidence_manifest_sha256 CHAR(64) NULL CHECK (
        evidence_manifest_sha256 IS NULL OR evidence_manifest_sha256 ~ '^[0-9a-f]{64}$'
    ),
    last_accepted_complete_time_utc TIMESTAMPTZ NULL,
    consecutive_normal_passes SMALLINT NOT NULL DEFAULT 0 CHECK (
        consecutive_normal_passes BETWEEN 0 AND 2
    ),
    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (instrument_id,horizon_minutes,price_basis),
    CHECK (
        publication_status NOT IN ('STAGING','PUBLISHED') OR (
            quality_status='PASS'
            AND coverage_status IN ('PASS','WARN')
            AND freshness_status='PASS'
            AND blocker_code IS NULL
            AND last_evaluated_run_id IS NOT NULL
            AND evidence_manifest_relative_path IS NOT NULL
            AND evidence_manifest_sha256 IS NOT NULL
            AND last_accepted_complete_time_utc IS NOT NULL
        )
    ),
    CHECK (
        publication_status <> 'PUBLISHED' OR consecutive_normal_passes=2
    )
);

INSERT INTO catalog.series_publication_state (
    instrument_id,horizon_minutes,price_basis,publication_status,
    quality_status,coverage_status,freshness_status,blocker_code
)
SELECT instrument_id,60,'bid_ask_mid','CANDIDATE',
       'NOT_EVALUATED','NOT_EVALUATED','NOT_EVALUATED',
       'BLOCKED_CANDIDATE_ONBOARDING_REQUIRED'
FROM catalog.instrument
WHERE provider='Saxo OpenAPI' AND environment='SIM'
  AND market_key IN ('audusd','usdcad','usdchf') AND active_to_utc IS NULL
ON CONFLICT (instrument_id,horizon_minutes,price_basis) DO NOTHING;

CREATE INDEX series_publication_status_idx
    ON catalog.series_publication_state (publication_status,instrument_id);

REVOKE ALL ON catalog.series_publication_state FROM PUBLIC;
GRANT SELECT,INSERT,UPDATE ON catalog.series_publication_state TO saxo_ingest;
GRANT SELECT ON catalog.series_publication_state TO saxo_app_reader;
