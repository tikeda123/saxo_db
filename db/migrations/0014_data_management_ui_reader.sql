SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0014_data_management_ui_reader.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

GRANT SELECT ON curated.etf_total_return_daily TO saxo_app_reader;
