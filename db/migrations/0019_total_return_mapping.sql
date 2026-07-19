SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0019_total_return_mapping.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

CREATE TABLE IF NOT EXISTS catalog.series_instrument_mapping (
    source_dataset_id TEXT NOT NULL REFERENCES catalog.source_dataset(source_dataset_id),
    external_series_key TEXT NOT NULL CHECK (length(external_series_key) > 0),
    instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(instrument_id),
    mapping_kind TEXT NOT NULL CHECK (mapping_kind IN ('TICKER_EXACT', 'EXPLICIT_REVIEW')),
    mapping_reason TEXT NOT NULL CHECK (length(mapping_reason) > 0),
    approved_at_utc TIMESTAMPTZ NOT NULL,
    approved_by TEXT NOT NULL CHECK (length(approved_by) > 0),
    active BOOLEAN NOT NULL DEFAULT TRUE,
    PRIMARY KEY (source_dataset_id, external_series_key),
    UNIQUE (source_dataset_id, instrument_id)
);

GRANT USAGE ON SCHEMA catalog TO saxo_app_reader, saxo_analyst_reader;
GRANT SELECT ON catalog.series_instrument_mapping TO saxo_app_reader, saxo_analyst_reader;

INSERT INTO catalog.series_instrument_mapping (
    source_dataset_id, external_series_key, instrument_id,
    mapping_kind, mapping_reason, approved_at_utc, approved_by, active
)
SELECT
    '20260712T135236Z', v.external_series_key, i.instrument_id,
    'TICKER_EXACT',
    'ETF11 curated total-return ticker explicitly reviewed against catalog.instrument.market_key',
    '2026-07-20T00:00:00Z'::timestamptz,
    'codex-dmi3-20260720', TRUE
FROM (VALUES
    ('EEM', 'eem'), ('EFA', 'efa'), ('GLD', 'gld'), ('IEF', 'ief'),
    ('IWM', 'iwm'), ('LQD', 'lqd'), ('SHY', 'shy'), ('SPY', 'spy'),
    ('TIP', 'tip'), ('TLT', 'tlt'), ('VNQ', 'vnq')
) AS v(external_series_key, market_key)
JOIN catalog.instrument i ON i.market_key = v.market_key AND i.active_to_utc IS NULL
WHERE EXISTS (
    SELECT 1 FROM catalog.source_dataset d
    WHERE d.source_dataset_id = '20260712T135236Z'
      AND d.dataset_kind = 'total_return'
      AND d.price_basis = 'etf_total_return'
)
ON CONFLICT (source_dataset_id, external_series_key) DO NOTHING;

DO $$
DECLARE
    mapping_count INTEGER;
BEGIN
    SELECT COUNT(*) INTO mapping_count
    FROM catalog.series_instrument_mapping
    WHERE source_dataset_id = '20260712T135236Z' AND active;
    IF mapping_count <> 11 THEN
        RAISE EXCEPTION '0019 expected 11 approved total-return mappings, found %', mapping_count;
    END IF;
END
$$;
