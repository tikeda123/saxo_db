SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_forward_v13' THEN
        RAISE EXCEPTION '0004_forward_schema.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

CREATE SCHEMA IF NOT EXISTS catalog AUTHORIZATION saxo_db_owner;
CREATE SCHEMA IF NOT EXISTS ops AUTHORIZATION saxo_db_owner;
CREATE SCHEMA IF NOT EXISTS raw AUTHORIZATION saxo_db_owner;
CREATE SCHEMA IF NOT EXISTS quality AUTHORIZATION saxo_db_owner;

CREATE TABLE IF NOT EXISTS ops.schema_migration (
    target_database TEXT NOT NULL,
    migration_number TEXT NOT NULL,
    filename TEXT NOT NULL,
    sha256 CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    applied_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (target_database, migration_number)
);

CREATE TABLE IF NOT EXISTS catalog.source_dataset (
    source_dataset_id TEXT PRIMARY KEY,
    dataset_name TEXT NOT NULL,
    provider TEXT NOT NULL,
    environment TEXT NOT NULL,
    dataset_kind TEXT NOT NULL,
    price_basis TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS catalog.instrument (
    instrument_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider TEXT NOT NULL,
    environment TEXT NOT NULL,
    market_key TEXT NOT NULL,
    symbol TEXT NOT NULL,
    uic BIGINT NOT NULL,
    asset_type TEXT NOT NULL,
    category TEXT NOT NULL,
    currency CHAR(3) NOT NULL,
    exchange_id TEXT NULL,
    active_from_utc TIMESTAMPTZ NOT NULL,
    active_to_utc TIMESTAMPTZ NULL,
    UNIQUE (provider, environment, uic, asset_type)
);

CREATE TABLE IF NOT EXISTS ops.ingestion_run (
    ingestion_run_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at_utc TIMESTAMPTZ NULL,
    trigger TEXT NOT NULL,
    environment TEXT NOT NULL,
    status TEXT NOT NULL,
    requested_series JSONB NOT NULL DEFAULT '[]'::jsonb,
    successful_series INTEGER NOT NULL DEFAULT 0,
    inserted_rows BIGINT NOT NULL DEFAULT 0,
    updated_rows BIGINT NOT NULL DEFAULT 0,
    revision_rows BIGINT NOT NULL DEFAULT 0,
    rejected_rows BIGINT NOT NULL DEFAULT 0,
    source_manifest_sha256 CHAR(64) NULL,
    error_code TEXT NULL
);

CREATE TABLE IF NOT EXISTS ops.source_file (
    source_file_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingestion_run_id BIGINT NOT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    relative_path TEXT NOT NULL,
    sha256 CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes BIGINT NOT NULL,
    row_count BIGINT NOT NULL,
    source_dataset_id TEXT NOT NULL REFERENCES catalog.source_dataset(source_dataset_id),
    registered_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (relative_path, sha256)
);

CREATE TABLE IF NOT EXISTS raw.market_bar_revision (
    ingestion_run_id BIGINT NOT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    source_file_id BIGINT NOT NULL REFERENCES ops.source_file(source_file_id),
    instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(instrument_id),
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes = 60),
    time_utc TIMESTAMPTZ NOT NULL,
    open NUMERIC(24,12) NOT NULL,
    high NUMERIC(24,12) NOT NULL,
    low NUMERIC(24,12) NOT NULL,
    close NUMERIC(24,12) NOT NULL,
    open_bid NUMERIC(24,12) NULL,
    high_bid NUMERIC(24,12) NULL,
    low_bid NUMERIC(24,12) NULL,
    close_bid NUMERIC(24,12) NULL,
    open_ask NUMERIC(24,12) NULL,
    high_ask NUMERIC(24,12) NULL,
    low_ask NUMERIC(24,12) NULL,
    close_ask NUMERIC(24,12) NULL,
    volume NUMERIC(30,8) NULL,
    market_trading_state TEXT NULL,
    price_basis TEXT NOT NULL,
    is_complete BOOLEAN NOT NULL,
    data_version BIGINT NULL,
    delayed_by_minutes INTEGER NULL,
    retrieved_at_utc TIMESTAMPTZ NOT NULL,
    payload_sha256 CHAR(64) NOT NULL CHECK (payload_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (ingestion_run_id, instrument_id, horizon_minutes, time_utc, price_basis)
);

CREATE OR REPLACE PROCEDURE raw.append_forward_market_bar(
    p_ingestion_run_id BIGINT,
    p_source_file_id BIGINT,
    p_instrument_id BIGINT,
    p_horizon_minutes SMALLINT,
    p_time_utc TIMESTAMPTZ,
    p_price_basis TEXT,
    p_payload JSONB
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
BEGIN
    IF p_horizon_minutes <> 60 THEN
        RAISE EXCEPTION 'forward append accepts completed canonical 60m bars only';
    END IF;
    IF COALESCE((p_payload ->> 'is_complete')::BOOLEAN, FALSE) IS NOT TRUE THEN
        RAISE EXCEPTION 'forward append requires is_complete=true';
    END IF;

    INSERT INTO raw.market_bar_revision (
        ingestion_run_id, source_file_id, instrument_id, horizon_minutes, time_utc,
        open, high, low, close,
        open_bid, high_bid, low_bid, close_bid,
        open_ask, high_ask, low_ask, close_ask,
        volume, market_trading_state, price_basis, is_complete, data_version,
        delayed_by_minutes, retrieved_at_utc, payload_sha256
    ) VALUES (
        p_ingestion_run_id, p_source_file_id, p_instrument_id, p_horizon_minutes, p_time_utc,
        (p_payload ->> 'open')::NUMERIC, (p_payload ->> 'high')::NUMERIC,
        (p_payload ->> 'low')::NUMERIC, (p_payload ->> 'close')::NUMERIC,
        NULLIF(p_payload ->> 'open_bid', '')::NUMERIC,
        NULLIF(p_payload ->> 'high_bid', '')::NUMERIC,
        NULLIF(p_payload ->> 'low_bid', '')::NUMERIC,
        NULLIF(p_payload ->> 'close_bid', '')::NUMERIC,
        NULLIF(p_payload ->> 'open_ask', '')::NUMERIC,
        NULLIF(p_payload ->> 'high_ask', '')::NUMERIC,
        NULLIF(p_payload ->> 'low_ask', '')::NUMERIC,
        NULLIF(p_payload ->> 'close_ask', '')::NUMERIC,
        NULLIF(p_payload ->> 'volume', '')::NUMERIC,
        p_payload ->> 'market_trading_state', p_price_basis, TRUE,
        NULLIF(p_payload ->> 'data_version', '')::BIGINT,
        NULLIF(p_payload ->> 'delayed_by_minutes', '')::INTEGER,
        (p_payload ->> 'retrieved_at_utc')::TIMESTAMPTZ,
        p_payload ->> 'payload_sha256'
    );
END
$$;

REVOKE ALL ON PROCEDURE raw.append_forward_market_bar(
    BIGINT, BIGINT, BIGINT, SMALLINT, TIMESTAMPTZ, TEXT, JSONB
) FROM PUBLIC;
