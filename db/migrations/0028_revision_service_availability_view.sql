SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0028_revision_service_availability_view.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

CREATE VIEW ops.v_series_revision_availability
WITH (security_barrier = true)
AS
SELECT
    i.market_key AS instrument_key,
    w.horizon_minutes,
    w.price_basis,
    w.data_status,
    COALESCE(
        r.availability_status,
        CASE WHEN w.data_status='ACTIVE' THEN 'AVAILABLE' ELSE 'BLOCKED' END
    )::TEXT AS availability_status,
    r.reconciliation_status,
    r.reason_code,
    r.old_data_version,
    r.new_data_version,
    r.revision_event_id
FROM catalog.instrument i
JOIN ops.watermark w USING (instrument_id)
LEFT JOIN ops.v_data_version_revision_state r
  ON r.instrument_id=i.instrument_id
 AND r.horizon_minutes=w.horizon_minutes
 AND r.price_basis=w.price_basis
WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
  AND i.active_to_utc IS NULL;

REVOKE ALL ON ops.v_series_revision_availability FROM PUBLIC;
GRANT SELECT ON ops.v_series_revision_availability TO saxo_app_reader,saxo_analyst_reader;
