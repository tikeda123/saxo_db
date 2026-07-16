SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0012_db3_full_refetch_guard.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

CREATE OR REPLACE PROCEDURE curated.prepare_full_refetch(
    IN p_ingestion_run_id BIGINT,
    IN p_instrument_id BIGINT
)
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog
AS $$
DECLARE
    selected_trigger TEXT;
    selected_run_status TEXT;
    selected_data_status TEXT;
BEGIN
    IF SESSION_USER <> 'saxo_ingest' THEN
        RAISE EXCEPTION 'full refetch procedure is restricted to saxo_ingest';
    END IF;

    SELECT r.trigger, r.status, w.data_status
      INTO selected_trigger, selected_run_status, selected_data_status
    FROM ops.ingestion_run r
    JOIN ops.watermark w
      ON w.instrument_id = p_instrument_id
     AND w.horizon_minutes = 60
    WHERE r.ingestion_run_id = p_ingestion_run_id
    FOR UPDATE OF r, w;

    IF NOT FOUND
       OR selected_trigger <> 'manual_db3_full_refetch'
       OR selected_run_status <> 'RUNNING'
       OR selected_data_status <> 'STALE_DATA_VERSION' THEN
        RAISE EXCEPTION 'full refetch guard condition failed';
    END IF;

    DELETE FROM curated.market_bar
    WHERE instrument_id = p_instrument_id AND horizon_minutes = 60;
END
$$;

REVOKE ALL ON PROCEDURE curated.prepare_full_refetch(BIGINT, BIGINT) FROM PUBLIC;
GRANT EXECUTE ON PROCEDURE curated.prepare_full_refetch(BIGINT, BIGINT) TO saxo_ingest;

