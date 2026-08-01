SET LOCAL ROLE saxo_db_owner;

-- PostgreSQL requires SELECT privilege for columns named by an INSERT ... ON
-- CONFLICT arbiter.  Keep the append-only evidence body hidden and expose only
-- the four immutable uniqueness-key columns to the trusted ingestion role.
GRANT SELECT (
    policy_id,instrument_id,time_utc,candidate_data_version
) ON derived.c2_market_bar_1h_imputation TO saxo_ingest;
