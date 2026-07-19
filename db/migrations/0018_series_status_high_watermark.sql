SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0018_series_status_high_watermark.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

CREATE VIEW quality.v_event_high_watermark
WITH (security_barrier = true)
AS
SELECT COALESCE(MAX(quality_event_id), 0)::BIGINT AS quality_event_high_watermark
FROM quality.event;

GRANT SELECT ON quality.v_event_high_watermark TO saxo_app_reader, saxo_analyst_reader;
