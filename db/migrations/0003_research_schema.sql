SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_research_v13' THEN
        RAISE EXCEPTION '0003_research_schema.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

COMMENT ON DATABASE saxo_research_v13 IS
    'v13 research snapshot database; data loading and read-only freeze occur in DB2';
