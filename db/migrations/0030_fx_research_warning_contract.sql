SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0030_fx_research_warning_contract.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

ALTER TABLE catalog.series_publication_state
    ADD COLUMN consumer_availability_status TEXT NOT NULL DEFAULT 'BLOCKED'
        CHECK (consumer_availability_status IN (
            'BLOCKED','AVAILABLE','AVAILABLE_WITH_WARNINGS'
        )),
    ADD COLUMN research_policy_id TEXT NULL,
    ADD COLUMN provider_advertised_start_utc TIMESTAMPTZ NULL,
    ADD COLUMN effective_coverage_start_utc TIMESTAMPTZ NULL,
    ADD COLUMN coverage_limitation TEXT NULL,
    ADD COLUMN warning_metadata_json JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(warning_metadata_json)='object'),
    ADD COLUMN policy_approved_at_utc TIMESTAMPTZ NULL,
    ADD COLUMN policy_approved_by TEXT NULL;

ALTER TABLE catalog.series_publication_state
    DROP CONSTRAINT series_publication_state_check,
    ADD CONSTRAINT series_publication_state_publication_gate_v2 CHECK (
        publication_status NOT IN ('STAGING','PUBLISHED') OR (
            quality_status IN ('PASS','WARN')
            AND coverage_status IN ('PASS','WARN')
            AND freshness_status='PASS'
            AND blocker_code IS NULL
            AND last_evaluated_run_id IS NOT NULL
            AND evidence_manifest_relative_path IS NOT NULL
            AND evidence_manifest_sha256 IS NOT NULL
            AND last_accepted_complete_time_utc IS NOT NULL
            AND effective_coverage_start_utc IS NOT NULL
            AND provider_advertised_start_utc IS NOT NULL
            AND consumer_availability_status IN (
                'AVAILABLE','AVAILABLE_WITH_WARNINGS'
            )
            AND (
                quality_status <> 'WARN' OR (
                    consumer_availability_status='AVAILABLE_WITH_WARNINGS'
                    AND research_policy_id IS NOT NULL
                    AND policy_approved_at_utc IS NOT NULL
                    AND length(btrim(policy_approved_by)) BETWEEN 1 AND 128
                    AND warning_metadata_json <> '{}'::JSONB
                )
            )
        )
    ),
    ADD CONSTRAINT series_publication_state_consumer_availability_v1 CHECK (
        (
            publication_status IN ('CANDIDATE','BLOCKED')
            AND consumer_availability_status='BLOCKED'
        ) OR (
            publication_status IN ('STAGING','PUBLISHED')
            AND consumer_availability_status IN (
                'AVAILABLE','AVAILABLE_WITH_WARNINGS'
            )
        )
    ),
    ADD CONSTRAINT series_publication_state_warning_contract_v1 CHECK (
        consumer_availability_status <> 'AVAILABLE_WITH_WARNINGS' OR (
            research_policy_id IS NOT NULL
            AND provider_advertised_start_utc IS NOT NULL
            AND effective_coverage_start_utc IS NOT NULL
            AND effective_coverage_start_utc >= provider_advertised_start_utc
            AND length(btrim(coverage_limitation)) BETWEEN 1 AND 2000
            AND warning_metadata_json <> '{}'::JSONB
            AND policy_approved_at_utc IS NOT NULL
            AND length(btrim(policy_approved_by)) BETWEEN 1 AND 128
        )
    );

UPDATE catalog.series_publication_state p SET
    consumer_availability_status='BLOCKED',
    research_policy_id='fx_research_candidate_user_approved_warnings_v1',
    provider_advertised_start_utc='2002-09-25T02:40:00Z',
    effective_coverage_start_utc=CASE i.market_key
        WHEN 'audusd' THEN '2003-05-12T00:00:00Z'::TIMESTAMPTZ
        WHEN 'usdcad' THEN '2010-06-18T00:00:00Z'::TIMESTAMPTZ
        WHEN 'usdchf' THEN '2010-06-18T00:00:00Z'::TIMESTAMPTZ
    END,
    coverage_limitation=CASE i.market_key
        WHEN 'audusd' THEN
            'Saxo Chart advertises 2002-09-25T02:40:00Z; stable paged 1H retrieval currently begins at 2003-05-12T00:00:00Z. No earlier prices are synthesized.'
        ELSE
            'Research coverage starts at the first stable paged 1H sample. The provider-advertised 2002-2010 interval is not filled, estimated, or represented as observed data.'
    END,
    warning_metadata_json=CASE i.market_key
        WHEN 'audusd' THEN jsonb_build_object(
            'values_modified',FALSE,
            'interpolation_performed',FALSE,
            'raw_deleted',FALSE,
            'known_provider_anomaly',jsonb_build_object(
                'policy_id','audusd_known_provider_extrema_14_v1',
                'rule_id','db3_fx_crossed_extrema_quarantine',
                'approved_unique_rows',14,
                'affected_from_utc','2013-09-15T21:00:00Z',
                'affected_to_utc','2020-04-19T21:00:00Z',
                'allowed_fields',jsonb_build_array('High','Low'),
                'content_sha256','c4039ebdef6caadad6f70cdce3d5c909ed88cbc042e362ecd4e58ad42337196e',
                'baseline_acquisition_run_id','20260728T000915Z-809e12ac'
            )
        )
        ELSE jsonb_build_object(
            'values_modified',FALSE,
            'interpolation_performed',FALSE,
            'provider_advertised_interval_before_effective_start_included',FALSE
        )
    END,
    policy_approved_at_utc='2026-07-28T01:47:09Z',
    policy_approved_by='project_owner',
    updated_at_utc=clock_timestamp()
FROM catalog.instrument i
WHERE i.instrument_id=p.instrument_id
  AND i.provider='Saxo OpenAPI' AND i.environment='SIM'
  AND i.market_key IN ('audusd','usdcad','usdchf')
  AND p.horizon_minutes=60 AND p.price_basis='bid_ask_mid';

UPDATE catalog.source_dataset SET
    source_manifest_relative_path='specs/source_collection/fx_research_candidates_v1.json',
    metadata_json=metadata_json || jsonb_build_object(
        'research_warning_policy_id','fx_research_candidate_user_approved_warnings_v1',
        'consumer_availability_status','AVAILABLE_WITH_WARNINGS',
        'value_repair',FALSE,
        'interpolation',FALSE
    )
WHERE source_dataset_id='saxo_sim_fx_research_candidates_60m_v1';

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
        p.consumer_availability_status,
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
    r.evidence_sample_count,
    p.publication_status,
    p.research_policy_id AS publication_policy_id
FROM catalog.instrument i
JOIN ops.watermark w USING (instrument_id)
LEFT JOIN ops.v_data_version_revision_state r
  ON r.instrument_id=i.instrument_id
 AND r.horizon_minutes=w.horizon_minutes
 AND r.price_basis=w.price_basis
LEFT JOIN catalog.series_publication_state p
  ON p.instrument_id=i.instrument_id
 AND p.horizon_minutes=w.horizon_minutes
 AND p.price_basis=w.price_basis
WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
  AND i.active_to_utc IS NULL;

GRANT SELECT ON catalog.series_publication_state
    TO saxo_app_reader,saxo_analyst_reader,saxo_ops_operator;
GRANT SELECT ON ops.v_series_revision_availability
    TO saxo_app_reader,saxo_analyst_reader,saxo_ops_operator;
