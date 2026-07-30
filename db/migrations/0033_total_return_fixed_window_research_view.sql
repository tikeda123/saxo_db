SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0033_total_return_fixed_window_research_view.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

CREATE OR REPLACE VIEW analytics.v_total_return_research_series
WITH (security_barrier = true)
AS
SELECT
    m.source_dataset_id,
    m.external_series_key,
    m.instrument_id,
    m.mapping_kind,
    m.mapping_reason,
    m.approved_at_utc,
    m.approved_by,
    i.market_key AS instrument_key,
    i.symbol,
    i.category,
    ds.dataset_name,
    ds.provider,
    ds.dataset_kind,
    ds.price_basis,
    ds.canonical_horizon_minutes,
    ds.research_eligibility,
    ds.source_manifest_relative_path,
    ds.source_manifest_sha256,
    COALESCE((ds.metadata_json->>'current')::BOOLEAN,FALSE) AS is_current,
    1::BIGINT AS mapping_count,
    stats.row_count,
    stats.min_session_date,
    stats.max_session_date,
    stats.duplicate_count,
    stats.null_or_nonpositive_count,
    stats.quality_fail_count,
    stats.quality_not_evaluated_count,
    stats.quality_warn_count,
    stats.source_file_count,
    stats.missing_source_file_count,
    stats.source_dataset_lineage_mismatch_count,
    stats.source_file_sha256_values
FROM catalog.series_instrument_mapping m
JOIN catalog.instrument i ON i.instrument_id=m.instrument_id
JOIN catalog.source_dataset ds ON ds.source_dataset_id=m.source_dataset_id
LEFT JOIN LATERAL (
    SELECT
        COUNT(*)::BIGINT AS row_count,
        MIN(d.date) AS min_session_date,
        MAX(d.date) AS max_session_date,
        (COUNT(*)-COUNT(DISTINCT d.date))::BIGINT AS duplicate_count,
        COUNT(*) FILTER (
            WHERE d.adjusted_close IS NULL OR d.adjusted_close <= 0
               OR d.total_return_index IS NULL OR d.total_return_index <= 0
        )::BIGINT AS null_or_nonpositive_count,
        COUNT(*) FILTER (WHERE d.quality_status='FAIL')::BIGINT AS quality_fail_count,
        COUNT(*) FILTER (WHERE d.quality_status='NOT_EVALUATED')::BIGINT AS quality_not_evaluated_count,
        COUNT(*) FILTER (WHERE d.quality_status='WARN')::BIGINT AS quality_warn_count,
        COUNT(DISTINCT d.source_file_id)::BIGINT AS source_file_count,
        COUNT(*) FILTER (WHERE sf.source_file_id IS NULL)::BIGINT AS missing_source_file_count,
        COUNT(*) FILTER (
            WHERE sf.source_file_id IS NOT NULL
              AND sf.source_dataset_id<>d.source_dataset_id
        )::BIGINT AS source_dataset_lineage_mismatch_count,
        COALESCE(
            ARRAY_AGG(DISTINCT sf.sha256::TEXT) FILTER (WHERE sf.sha256 IS NOT NULL),
            ARRAY[]::TEXT[]
        ) AS source_file_sha256_values
    FROM curated.etf_total_return_daily d
    LEFT JOIN ops.source_file sf ON sf.source_file_id=d.source_file_id
    WHERE d.source_dataset_id=m.source_dataset_id
      AND d.ticker=m.external_series_key
) stats ON TRUE
WHERE m.active
  AND m.approved_at_utc IS NOT NULL
  AND i.active_to_utc IS NULL
  AND ds.dataset_kind='total_return'
  AND ds.price_basis='etf_total_return';

GRANT USAGE ON SCHEMA analytics TO saxo_app_reader, saxo_analyst_reader;
GRANT SELECT ON analytics.v_total_return_research_series TO saxo_app_reader, saxo_analyst_reader;
