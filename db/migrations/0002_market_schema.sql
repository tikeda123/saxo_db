SET LOCAL ROLE saxo_db_owner;

CREATE SCHEMA IF NOT EXISTS catalog AUTHORIZATION saxo_db_owner;
CREATE SCHEMA IF NOT EXISTS ops AUTHORIZATION saxo_db_owner;
CREATE SCHEMA IF NOT EXISTS raw AUTHORIZATION saxo_db_owner;
CREATE SCHEMA IF NOT EXISTS staging AUTHORIZATION saxo_db_owner;
CREATE SCHEMA IF NOT EXISTS curated AUTHORIZATION saxo_db_owner;
CREATE SCHEMA IF NOT EXISTS derived AUTHORIZATION saxo_db_owner;
CREATE SCHEMA IF NOT EXISTS quality AUTHORIZATION saxo_db_owner;
CREATE SCHEMA IF NOT EXISTS analytics AUTHORIZATION saxo_db_owner;

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
    dataset_kind TEXT NOT NULL CHECK (
        dataset_kind IN ('raw_market', 'external_reference', 'total_return', 'analysis_baseline')
    ),
    price_basis TEXT NOT NULL,
    canonical_horizon_minutes SMALLINT NULL CHECK (canonical_horizon_minutes IS NULL OR canonical_horizon_minutes > 0),
    expected_update_interval_seconds BIGINT NULL CHECK (expected_update_interval_seconds IS NULL OR expected_update_interval_seconds > 0),
    freshness_grace_seconds BIGINT NULL CHECK (freshness_grace_seconds IS NULL OR freshness_grace_seconds >= 0),
    authoritative_layer TEXT NOT NULL CHECK (authoritative_layer IN ('raw', 'curated', 'derived', 'research_metadata')),
    research_eligibility TEXT NOT NULL,
    active BOOLEAN NOT NULL DEFAULT TRUE,
    source_manifest_relative_path TEXT NULL CHECK (
        source_manifest_relative_path IS NULL OR (
            source_manifest_relative_path !~ '^/' AND
            source_manifest_relative_path !~ '^[A-Za-z]:' AND
            source_manifest_relative_path !~ '(^|/)\.\.(/|$)'
        )
    ),
    source_manifest_sha256 CHAR(64) NULL CHECK (
        source_manifest_sha256 IS NULL OR source_manifest_sha256 ~ '^[0-9a-f]{64}$'
    ),
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS catalog.session_calendar (
    session_calendar_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    exchange_id TEXT NULL,
    asset_type TEXT NOT NULL,
    timezone_name TEXT NOT NULL,
    schedule_version TEXT NOT NULL,
    effective_from DATE NOT NULL,
    effective_to DATE NULL CHECK (effective_to IS NULL OR effective_to >= effective_from),
    metadata_json JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS catalog.session_interval (
    session_calendar_id TEXT NOT NULL REFERENCES catalog.session_calendar(session_calendar_id),
    session_date DATE NOT NULL,
    interval_sequence SMALLINT NOT NULL CHECK (interval_sequence >= 0),
    open_time_utc TIMESTAMPTZ NULL,
    close_time_utc TIMESTAMPTZ NULL,
    session_status TEXT NOT NULL CHECK (session_status IN ('OPEN', 'SHORT_SESSION', 'HOLIDAY')),
    source_sha256 CHAR(64) NULL CHECK (source_sha256 IS NULL OR source_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (session_calendar_id, session_date, interval_sequence),
    CHECK (
        (session_status = 'HOLIDAY' AND open_time_utc IS NULL AND close_time_utc IS NULL) OR
        (session_status IN ('OPEN', 'SHORT_SESSION') AND open_time_utc IS NOT NULL AND close_time_utc > open_time_utc)
    )
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
    session_calendar_id TEXT NULL REFERENCES catalog.session_calendar(session_calendar_id),
    active_from_utc TIMESTAMPTZ NOT NULL,
    active_to_utc TIMESTAMPTZ NULL CHECK (active_to_utc IS NULL OR active_to_utc > active_from_utc),
    UNIQUE (provider, environment, uic, asset_type)
);

CREATE TABLE IF NOT EXISTS ops.ingestion_run (
    ingestion_run_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    started_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at_utc TIMESTAMPTZ NULL,
    trigger TEXT NOT NULL,
    environment TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'PASS', 'FAILED', 'BLOCKED')),
    requested_series JSONB NOT NULL DEFAULT '[]'::jsonb,
    successful_series INTEGER NOT NULL DEFAULT 0 CHECK (successful_series >= 0),
    inserted_rows BIGINT NOT NULL DEFAULT 0 CHECK (inserted_rows >= 0),
    updated_rows BIGINT NOT NULL DEFAULT 0 CHECK (updated_rows >= 0),
    revision_rows BIGINT NOT NULL DEFAULT 0 CHECK (revision_rows >= 0),
    rejected_rows BIGINT NOT NULL DEFAULT 0 CHECK (rejected_rows >= 0),
    source_manifest_sha256 CHAR(64) NULL CHECK (source_manifest_sha256 IS NULL OR source_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    error_code TEXT NULL,
    CHECK (finished_at_utc IS NULL OR finished_at_utc >= started_at_utc)
);

CREATE TABLE IF NOT EXISTS ops.source_file (
    source_file_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingestion_run_id BIGINT NOT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    relative_path TEXT NOT NULL CHECK (
        relative_path !~ '^/' AND relative_path !~ '^[A-Za-z]:' AND relative_path !~ '(^|/)\.\.(/|$)'
    ),
    sha256 CHAR(64) NOT NULL CHECK (sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes BIGINT NOT NULL CHECK (size_bytes >= 0),
    row_count BIGINT NOT NULL CHECK (row_count >= 0),
    source_dataset_id TEXT NOT NULL REFERENCES catalog.source_dataset(source_dataset_id),
    registered_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (relative_path, sha256)
);

CREATE TABLE IF NOT EXISTS ops.watermark (
    instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(instrument_id),
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes > 0),
    price_basis TEXT NOT NULL,
    latest_seen_time_utc TIMESTAMPTZ NOT NULL,
    latest_complete_time_utc TIMESTAMPTZ NULL,
    data_version BIGINT NULL,
    updated_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (instrument_id, horizon_minutes, price_basis),
    CHECK (latest_complete_time_utc IS NULL OR latest_complete_time_utc <= latest_seen_time_utc)
);

CREATE TABLE IF NOT EXISTS ops.research_snapshot (
    snapshot_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    plan_id TEXT NOT NULL,
    research_line_id TEXT NOT NULL,
    cutoff_utc TIMESTAMPTZ NOT NULL,
    source_database TEXT NOT NULL,
    source_manifest_sha256 CHAR(64) NOT NULL CHECK (source_manifest_sha256 ~ '^[0-9a-f]{64}$'),
    row_counts_json JSONB NOT NULL,
    snapshot_sha256 CHAR(64) NOT NULL CHECK (snapshot_sha256 ~ '^[0-9a-f]{64}$'),
    frozen_at_utc TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ops.backup_run (
    backup_run_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    database_name TEXT NOT NULL CHECK (database_name IN ('saxo_market', 'saxo_research_v13', 'saxo_forward_v13')),
    started_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    finished_at_utc TIMESTAMPTZ NULL,
    status TEXT NOT NULL CHECK (status IN ('RUNNING', 'PASS', 'FAILED', 'BLOCKED')),
    relative_path TEXT NOT NULL CHECK (
        relative_path !~ '^/' AND relative_path !~ '^[A-Za-z]:' AND relative_path !~ '(^|/)\.\.(/|$)'
    ),
    sha256 CHAR(64) NULL CHECK (sha256 IS NULL OR sha256 ~ '^[0-9a-f]{64}$'),
    size_bytes BIGINT NULL CHECK (size_bytes IS NULL OR size_bytes >= 0),
    pg_restore_list_pass BOOLEAN NULL,
    restore_smoke_tested_at_utc TIMESTAMPTZ NULL,
    restore_smoke_test_status TEXT NULL,
    error_code TEXT NULL,
    CHECK (finished_at_utc IS NULL OR finished_at_utc >= started_at_utc),
    CHECK (status <> 'PASS' OR (sha256 IS NOT NULL AND size_bytes IS NOT NULL AND pg_restore_list_pass IS TRUE))
);

CREATE TABLE IF NOT EXISTS raw.market_bar_revision (
    ingestion_run_id BIGINT NOT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    source_file_id BIGINT NOT NULL REFERENCES ops.source_file(source_file_id),
    instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(instrument_id),
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes > 0),
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

CREATE TABLE IF NOT EXISTS curated.market_bar (
    instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(instrument_id),
    horizon_minutes SMALLINT NOT NULL CHECK (horizon_minutes = 60),
    time_utc TIMESTAMPTZ NOT NULL,
    open NUMERIC(24,12) NOT NULL CHECK (open > 0),
    high NUMERIC(24,12) NOT NULL CHECK (high > 0),
    low NUMERIC(24,12) NOT NULL CHECK (low > 0),
    close NUMERIC(24,12) NOT NULL CHECK (close > 0),
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
    latest_ingestion_run_id BIGINT NOT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    retrieved_at_utc TIMESTAMPTZ NOT NULL,
    quality_status TEXT NOT NULL CHECK (quality_status IN ('PASS', 'WARN', 'FAIL', 'NOT_EVALUATED')),
    PRIMARY KEY (instrument_id, horizon_minutes, time_utc, price_basis),
    CHECK (high >= GREATEST(open, low, close)),
    CHECK (low <= LEAST(open, high, close))
);

CREATE TABLE IF NOT EXISTS curated.etf_total_return_daily (
    source_dataset_id TEXT NOT NULL REFERENCES catalog.source_dataset(source_dataset_id),
    ticker TEXT NOT NULL,
    date DATE NOT NULL,
    currency CHAR(3) NOT NULL,
    open_unadjusted NUMERIC(24,12) NULL,
    high_unadjusted NUMERIC(24,12) NULL,
    low_unadjusted NUMERIC(24,12) NULL,
    close_unadjusted NUMERIC(24,12) NULL,
    adjusted_close NUMERIC(24,12) NOT NULL,
    total_return_index NUMERIC(24,12) NOT NULL,
    volume NUMERIC(30,8) NULL,
    dividend_cash NUMERIC(24,12) NULL,
    split_factor NUMERIC(24,12) NULL,
    source TEXT NOT NULL,
    quality_status TEXT NOT NULL CHECK (quality_status IN ('PASS', 'WARN', 'FAIL', 'NOT_EVALUATED')),
    PRIMARY KEY (source_dataset_id, ticker, date)
);

CREATE TABLE IF NOT EXISTS derived.market_bar_4h (
    derivation_version TEXT NOT NULL,
    instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(instrument_id),
    time_utc TIMESTAMPTZ NOT NULL,
    price_basis TEXT NOT NULL,
    open NUMERIC(24,12) NOT NULL,
    high NUMERIC(24,12) NOT NULL,
    low NUMERIC(24,12) NOT NULL,
    close NUMERIC(24,12) NOT NULL,
    volume NUMERIC(30,8) NULL,
    is_complete BOOLEAN NOT NULL,
    source_first_ingestion_run_id BIGINT NOT NULL,
    source_last_ingestion_run_id BIGINT NOT NULL,
    quality_status TEXT NOT NULL CHECK (quality_status IN ('PASS', 'WARN', 'FAIL', 'NOT_EVALUATED')),
    PRIMARY KEY (derivation_version, instrument_id, time_utc, price_basis)
);

CREATE TABLE IF NOT EXISTS derived.market_bar_1d_risk (
    derivation_version TEXT NOT NULL,
    instrument_id BIGINT NOT NULL REFERENCES catalog.instrument(instrument_id),
    session_date DATE NOT NULL,
    price_basis TEXT NOT NULL,
    open NUMERIC(24,12) NOT NULL,
    high NUMERIC(24,12) NOT NULL,
    low NUMERIC(24,12) NOT NULL,
    close NUMERIC(24,12) NOT NULL,
    volume NUMERIC(30,8) NULL,
    is_complete BOOLEAN NOT NULL,
    source_first_ingestion_run_id BIGINT NOT NULL,
    source_last_ingestion_run_id BIGINT NOT NULL,
    quality_status TEXT NOT NULL CHECK (quality_status IN ('PASS', 'WARN', 'FAIL', 'NOT_EVALUATED')),
    PRIMARY KEY (derivation_version, instrument_id, session_date, price_basis)
);

CREATE TABLE IF NOT EXISTS quality.event (
    quality_event_id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    ingestion_run_id BIGINT NULL REFERENCES ops.ingestion_run(ingestion_run_id),
    instrument_id BIGINT NULL REFERENCES catalog.instrument(instrument_id),
    horizon_minutes SMALLINT NULL,
    time_utc TIMESTAMPTZ NULL,
    rule_id TEXT NOT NULL,
    severity TEXT NOT NULL CHECK (severity IN ('INFO', 'WARN', 'ERROR', 'CRITICAL')),
    observed_value JSONB NOT NULL DEFAULT '{}'::jsonb,
    action TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'OPEN' CHECK (status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVED')),
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    resolved_at_utc TIMESTAMPTZ NULL,
    resolved_by TEXT NULL,
    resolution_note TEXT NULL,
    CHECK (
        (status <> 'RESOLVED' AND resolved_at_utc IS NULL) OR
        (status = 'RESOLVED' AND resolved_at_utc IS NOT NULL AND resolved_by IS NOT NULL AND resolution_note IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS market_bar_revision_lookup_idx
    ON raw.market_bar_revision (instrument_id, horizon_minutes, time_utc, price_basis);
CREATE INDEX IF NOT EXISTS market_bar_time_idx
    ON curated.market_bar (instrument_id, horizon_minutes, time_utc);
CREATE INDEX IF NOT EXISTS quality_event_open_idx
    ON quality.event (status, severity, created_at_utc) WHERE status <> 'RESOLVED';
