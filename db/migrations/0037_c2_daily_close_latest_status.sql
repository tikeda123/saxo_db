SET LOCAL ROLE saxo_db_owner;

-- Operational latest-state projection for C2.  The full historical session
-- view created by 0036 intentionally contains future calendar sessions and is
-- not suitable for a low-latency status endpoint.  This view reads the latest
-- already-derived provider close and overlays only an aggregate warning for
-- still-active bounded imputations.  It does not change source or derived data.
CREATE OR REPLACE VIEW analytics.v_c2_daily_close_status_latest
WITH (security_barrier = true)
AS
WITH latest_daily AS (
    SELECT ranked.*
    FROM (
        SELECT d.*,
               ROW_NUMBER() OVER (
                   PARTITION BY d.instrument_id,d.price_basis
                   ORDER BY d.session_date DESC,d.derivation_version DESC
               ) AS selected_number
        FROM derived.market_bar_1d_risk d
        JOIN catalog.instrument i USING (instrument_id)
        WHERE lower(i.market_key)=ANY(
            ARRAY['spy','iwm','efa','eem','vnq','shy','ief','tlt','tip','lqd','gld']::TEXT[]
        )
          AND d.price_basis='native_ohlc' AND d.is_complete
    ) ranked
    WHERE ranked.selected_number=1
), active_imputation AS (
    SELECT x.instrument_id,
           COUNT(*)::INTEGER AS imputed_bar_count,
           MAX(x.session_date) AS latest_imputed_session_date
    FROM derived.c2_market_bar_1h_imputation x
    WHERE NOT EXISTS (
        SELECT 1
        FROM curated.market_bar b
        WHERE b.instrument_id=x.instrument_id
          AND b.horizon_minutes=60
          AND b.time_utc=x.time_utc
          AND b.price_basis=x.price_basis
          AND b.is_complete AND b.quality_status='PASS'
    )
    GROUP BY x.instrument_id
)
SELECT d.instrument_id,lower(i.market_key) AS instrument_key,
       d.session_date,d.price_basis,d.close,
       d.source_last_ingestion_run_id,
       COALESCE(x.imputed_bar_count,0) AS imputed_bar_count,
       x.latest_imputed_session_date,
       TRUE AS actual_terminal_close_present,
       CASE
           WHEN COALESCE(x.imputed_bar_count,0)>0 THEN 'PASS_WITH_IMPUTATION_WARNING'
           WHEN d.quality_status='PASS' THEN 'PASS'
           ELSE 'BLOCKED_DAILY_CLOSE_QUALITY'
       END AS derivation_status,
       CASE WHEN COALESCE(x.imputed_bar_count,0)>0
            THEN ARRAY['C2_BOUNDED_IMPUTED_PREVIOUS_VALID']::TEXT[]
            ELSE ARRAY[]::TEXT[] END AS warning_ids,
       FALSE AS official_close_claim,
       FALSE AS total_return_claim,
       FALSE AS execution_price_claim
FROM latest_daily d
JOIN catalog.instrument i USING (instrument_id)
LEFT JOIN active_imputation x USING (instrument_id);

REVOKE ALL ON analytics.v_c2_daily_close_status_latest FROM PUBLIC;
GRANT SELECT ON analytics.v_c2_daily_close_status_latest
TO saxo_app_reader,saxo_analyst_reader;
