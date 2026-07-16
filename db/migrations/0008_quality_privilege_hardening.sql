SET LOCAL ROLE saxo_db_owner;

DO $$
BEGIN
    IF current_database() <> 'saxo_market' THEN
        RAISE EXCEPTION '0008_quality_privilege_hardening.sql applied to unexpected database %', current_database();
    END IF;
END
$$;

REVOKE UPDATE, DELETE, TRUNCATE, REFERENCES, TRIGGER
    ON quality.event FROM saxo_ingest;
GRANT SELECT, INSERT ON quality.event TO saxo_ingest;
