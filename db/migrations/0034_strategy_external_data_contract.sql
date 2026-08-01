SET LOCAL ROLE saxo_db_owner;

CREATE TABLE IF NOT EXISTS catalog.strategy_external_data_contract (
    contract_id TEXT PRIMARY KEY,
    edc_id TEXT NOT NULL UNIQUE CHECK (edc_id ~ '^EDC-[0-9]{2}$'),
    dataset_role TEXT NOT NULL UNIQUE,
    contract_revision TEXT NOT NULL,
    contract_state TEXT NOT NULL CHECK (
        contract_state IN (
            'ACTIVE', 'READY_FOR_READ_ONLY_VALIDATION',
            'CLOSED_SPEC', 'BLOCKED_EXTERNAL_CONTRACT'
        )
    ),
    availability_state TEXT NOT NULL CHECK (
        availability_state IN ('AVAILABLE', 'NOT_EVALUATED', 'BLOCKED_EXTERNAL_CONTRACT')
    ),
    provider_id TEXT NULL,
    dataset_id TEXT NULL,
    price_basis TEXT NULL,
    horizon_minutes SMALLINT NULL CHECK (horizon_minutes IS NULL OR horizon_minutes > 0),
    target_read_endpoint TEXT NULL,
    blocker_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    decision_required_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    required_receipt_fields TEXT[] NOT NULL,
    manifest_relative_path TEXT NOT NULL CHECK (
        manifest_relative_path !~ '^/' AND
        manifest_relative_path !~ '^[A-Za-z]:' AND
        manifest_relative_path !~ '(^|/)\.\.(/|$)'
    ),
    manifest_sha256 CHAR(64) NOT NULL CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (
        availability_state <> 'BLOCKED_EXTERNAL_CONTRACT'
        OR (cardinality(blocker_ids) > 0 AND dataset_id IS NULL)
    )
);

CREATE TABLE IF NOT EXISTS ops.strategy_external_data_receipt (
    receipt_id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL REFERENCES catalog.strategy_external_data_contract(contract_id),
    availability_state TEXT NOT NULL CHECK (
        availability_state IN (
            'AVAILABLE', 'AVAILABLE_WITH_WARNINGS', 'DATA_NOT_READY',
            'BLOCKED_EXTERNAL_CONTRACT', 'BLOCKED_INTERFACE_OPERATIONAL',
            'FAIL_DATA_QUALITY'
        )
    ),
    dataset_id TEXT NULL,
    provider_id TEXT NULL,
    provider_data_version TEXT NULL,
    lineage_id TEXT NULL,
    manifest_sha256 CHAR(64) NULL CHECK (
        manifest_sha256 IS NULL OR manifest_sha256 ~ '^[0-9a-f]{64}$'
    ),
    ordered_content_sha256 CHAR(64) NULL CHECK (
        ordered_content_sha256 IS NULL OR ordered_content_sha256 ~ '^[0-9a-f]{64}$'
    ),
    calendar_id TEXT NULL,
    source_as_of TEXT NULL,
    source_observed_at_utc TIMESTAMPTZ NOT NULL,
    available_at_utc TIMESTAMPTZ NOT NULL,
    accepted_at_utc TIMESTAMPTZ NULL,
    expected_by_utc TIMESTAMPTZ NULL,
    published_at_utc TIMESTAMPTZ NULL,
    freshness_state TEXT NOT NULL CHECK (
        freshness_state IN (
            'CURRENT', 'DELAYED', 'DATA_NOT_READY', 'STALE',
            'NOT_EVALUATED_SLA', 'BLOCKED_INTERFACE_OPERATIONAL'
        )
    ),
    quality_state TEXT NOT NULL CHECK (
        quality_state IN ('PASS', 'PASS_WITH_WARNINGS', 'FAIL_DATA_QUALITY', 'NOT_EVALUATED')
    ),
    revision_state TEXT NOT NULL CHECK (
        revision_state IN (
            'CURRENT_ACCEPTED', 'REVISION_DETECTED', 'REVISION_REVIEW_PENDING',
            'REVISION_ACCEPTED_NEXT_DECISION', 'SUPERSEDED', 'NOT_EVALUATED'
        )
    ),
    cost_confidence TEXT NOT NULL CHECK (
        cost_confidence IN (
            'ACTUAL_BOOKED', 'PUBLISHED_SCHEDULE_ESTIMATE',
            'RESEARCH_MODEL_ONLY', 'UNKNOWN', 'NOT_APPLICABLE'
        )
    ),
    warning_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    blocker_ids TEXT[] NOT NULL DEFAULT ARRAY[]::TEXT[],
    account_fingerprint TEXT NULL,
    values_modified BOOLEAN NOT NULL DEFAULT FALSE CHECK (values_modified IS FALSE),
    interpolation_performed BOOLEAN NOT NULL DEFAULT FALSE CHECK (interpolation_performed IS FALSE),
    receipt_json JSONB NOT NULL CHECK (
        receipt_json::TEXT !~* '"(access_token|refresh_token|authorization|accountkey|clientkey|account_key|client_key)"[[:space:]]*:'
    ),
    receipt_sha256 CHAR(64) NOT NULL CHECK (receipt_sha256 ~ '^[0-9a-f]{64}$'),
    supersedes_receipt_id TEXT NULL REFERENCES ops.strategy_external_data_receipt(receipt_id),
    created_at_utc TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CHECK (available_at_utc >= source_observed_at_utc),
    CHECK (accepted_at_utc IS NULL OR accepted_at_utc >= source_observed_at_utc),
    CHECK (
        availability_state NOT IN ('AVAILABLE', 'AVAILABLE_WITH_WARNINGS')
        OR (
            accepted_at_utc IS NOT NULL
            AND quality_state IN ('PASS', 'PASS_WITH_WARNINGS')
            AND freshness_state = 'CURRENT'
            AND provider_id IS NOT NULL
            AND lineage_id IS NOT NULL
            AND ordered_content_sha256 IS NOT NULL
        )
    ),
    CHECK (
        availability_state <> 'AVAILABLE_WITH_WARNINGS'
        OR (quality_state = 'PASS_WITH_WARNINGS' AND cardinality(warning_ids) > 0)
    )
);

CREATE OR REPLACE FUNCTION ops.reject_strategy_external_receipt_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION 'strategy external data receipts are immutable';
END;
$$;

DROP TRIGGER IF EXISTS strategy_external_receipt_immutable
ON ops.strategy_external_data_receipt;
CREATE TRIGGER strategy_external_receipt_immutable
BEFORE UPDATE OR DELETE ON ops.strategy_external_data_receipt
FOR EACH ROW EXECUTE FUNCTION ops.reject_strategy_external_receipt_mutation();

INSERT INTO catalog.strategy_external_data_contract (
    contract_id, edc_id, dataset_role, contract_revision, contract_state,
    availability_state, provider_id, dataset_id, price_basis, horizon_minutes,
    target_read_endpoint, blocker_ids, decision_required_ids,
    required_receipt_fields, manifest_relative_path, manifest_sha256
)
VALUES
    ('c2_edc00_current_native_market_bar_v1','EDC-00','CURRENT_NATIVE_MARKET_BAR','1.0','READY_FOR_READ_ONLY_VALIDATION','NOT_EVALUATED','SAXO_OPENAPI_SIM',NULL,'native_ohlc',60,'/api/v1/bars',ARRAY['NOT_EVALUATED_CURRENT_RECEIPT'],ARRAY[]::TEXT[],ARRAY['contract_id','dataset_id','provider_id','provider_data_version','lineage_id','manifest_sha256','ordered_content_sha256','available_at_utc','quality_state'],'manifests/strategy_external_data_contract_manifest_v1.json','9e127e9fbed32b226b6a048c21868587e897c26bf437addaaa0ad9f214e25c97'),
    ('c2_edc01_signal_total_return_daily_v1','EDC-01','SIGNAL_TOTAL_RETURN_DAILY','1.0','BLOCKED_EXTERNAL_CONTRACT','BLOCKED_EXTERNAL_CONTRACT',NULL,NULL,'adjusted_total_return_index',1440,'/api/v1/total-return',ARRAY['BLOCKED_EXTERNAL_CONTRACT_SIGNAL_CURRENT'],ARRAY['EDR-01'],ARRAY['contract_id','dataset_id','provider_id','return_definition_id','provider_data_version','lineage_id','manifest_sha256','ordered_content_sha256','calendar_id','available_at_utc','quality_state','warning_ids','revision_id'],'manifests/strategy_external_data_contract_manifest_v1.json','9e127e9fbed32b226b6a048c21868587e897c26bf437addaaa0ad9f214e25c97'),
    ('c2_edc02_valuation_price_daily_v1','EDC-02','VALUATION_PRICE_DAILY','1.0','BLOCKED_EXTERNAL_CONTRACT','BLOCKED_EXTERNAL_CONTRACT',NULL,NULL,'UNADJUSTED_PRIMARY_EXCHANGE_OFFICIAL_CLOSE',1440,NULL,ARRAY['BLOCKED_EXTERNAL_CONTRACT_VALUATION_CLOSE'],ARRAY['EDR-02'],ARRAY['contract_id','dataset_id','ticker','uic','asset_type','session_date','mark_price','price_basis','primary_exchange','currency','source_observed_at_utc','available_at_utc','provider_data_version','is_complete','quality_state','receipt_sha256'],'manifests/strategy_external_data_contract_manifest_v1.json','9e127e9fbed32b226b6a048c21868587e897c26bf437addaaa0ad9f214e25c97'),
    ('c2_edc03_common_regular_session_calendar_v1','EDC-03','COMMON_REGULAR_SESSION_CALENDAR','1.0','BLOCKED_EXTERNAL_CONTRACT','BLOCKED_EXTERNAL_CONTRACT',NULL,NULL,NULL,NULL,'/api/v1/strategy-data/calendars/{calendar_id}',ARRAY['BLOCKED_EXTERNAL_CONTRACT_CALENDAR_PUBLICATION'],ARRAY['EDR-03'],ARRAY['calendar_id','calendar_version','tzdb_version','published_at_utc','valid_from','valid_to','source_urls','normalized_sha256','sessions'],'manifests/strategy_external_data_contract_manifest_v1.json','9e127e9fbed32b226b6a048c21868587e897c26bf437addaaa0ad9f214e25c97'),
    ('c2_edc04_distribution_declaration_v1','EDC-04','DISTRIBUTION_DECLARATION','1.0','READY_FOR_READ_ONLY_VALIDATION','BLOCKED_EXTERNAL_CONTRACT',NULL,NULL,NULL,NULL,'/api/v1/strategy-data/receipts?dataset_role=DISTRIBUTION_DECLARATION',ARRAY['BLOCKED_EXTERNAL_CONTRACT_DISTRIBUTION_DECLARATION_SOURCE'],ARRAY['EDR-05'],ARRAY['distribution_id','ticker','uic','distribution_type','gross_amount_per_share','currency','ex_date','record_date','payable_date','declared_at_utc','source_as_of_utc','source_revision_id','receipt_sha256'],'manifests/strategy_external_data_contract_manifest_v1.json','9e127e9fbed32b226b6a048c21868587e897c26bf437addaaa0ad9f214e25c97'),
    ('c2_edc05_distribution_cash_transaction_v1','EDC-05','DISTRIBUTION_CASH_TRANSACTION','1.0','BLOCKED_EXTERNAL_CONTRACT','BLOCKED_EXTERNAL_CONTRACT','SAXO_OPENAPI_ACCOUNT',NULL,NULL,NULL,'/api/v1/strategy-data/receipts?dataset_role=DISTRIBUTION_CASH_TRANSACTION',ARRAY['BLOCKED_EXTERNAL_CONTRACT_DISTRIBUTION_TRANSACTION'],ARRAY['EDR-05'],ARRAY['transaction_id','corporate_action_id','booking_ids','ticker','uic','transaction_type','event','currency','currency_decimals','booked_amount','posting_date','record_date','value_date','correction_reason','is_reversal','source_observed_at_utc','receipt_sha256'],'manifests/strategy_external_data_contract_manifest_v1.json','9e127e9fbed32b226b6a048c21868587e897c26bf437addaaa0ad9f214e25c97'),
    ('c2_edc06_instrument_reference_v1','EDC-06','INSTRUMENT_REFERENCE','1.0','READY_FOR_READ_ONLY_VALIDATION','BLOCKED_EXTERNAL_CONTRACT','SAXO_OPENAPI_ACCOUNT',NULL,NULL,NULL,'/api/v1/strategy-data/receipts?dataset_role=INSTRUMENT_REFERENCE',ARRAY['BLOCKED_EXTERNAL_CONTRACT_INSTRUMENT_ACCOUNT_CONTEXT'],ARRAY['EDR-06'],ARRAY['receipt_id','observed_at_utc','environment','account_fingerprint','source_endpoint_revision','normalized_sha256','instruments'],'manifests/strategy_external_data_contract_manifest_v1.json','9e127e9fbed32b226b6a048c21868587e897c26bf437addaaa0ad9f214e25c97'),
    ('c2_edc07_proposal_price_snapshot_v1','EDC-07','PROPOSAL_PRICE_SNAPSHOT','1.0','BLOCKED_EXTERNAL_CONTRACT','BLOCKED_EXTERNAL_CONTRACT','SAXO_OPENAPI_ACCOUNT',NULL,'account_context_bid_ask_quote',NULL,'/api/v1/strategy-data/receipts?dataset_role=PROPOSAL_PRICE_SNAPSHOT',ARRAY['BLOCKED_EXTERNAL_CONTRACT_PROPOSAL_QUOTE'],ARRAY['EDR-04'],ARRAY['snapshot_id','observed_at_utc','account_fingerprint','uic','asset_type','last_updated','price_source','amount','bid','ask','mid','bid_size','ask_size','delayed_by_minutes','error_code','market_state','price_type_bid','price_type_ask','is_market_open','receipt_sha256'],'manifests/strategy_external_data_contract_manifest_v1.json','9e127e9fbed32b226b6a048c21868587e897c26bf437addaaa0ad9f214e25c97'),
    ('c2_edc08_fee_estimate_and_actual_v1','EDC-08','FEE_ESTIMATE_AND_ACTUAL','1.0','BLOCKED_EXTERNAL_CONTRACT','BLOCKED_EXTERNAL_CONTRACT',NULL,NULL,NULL,NULL,'/api/v1/strategy-data/receipts?dataset_role=FEE_ESTIMATE_AND_ACTUAL',ARRAY['BLOCKED_EXTERNAL_CONTRACT_FEE_ESTIMATE'],ARRAY['EDR-07'],ARRAY['receipt_id','fee_kind','account_fingerprint','ticker','side','quantity','currency','amount','confidence','source_observed_at_utc','source_revision_id','receipt_sha256'],'manifests/strategy_external_data_contract_manifest_v1.json','9e127e9fbed32b226b6a048c21868587e897c26bf437addaaa0ad9f214e25c97'),
    ('c2_edc09_currency_and_amount_unit_v1','EDC-09','CURRENCY_AND_AMOUNT_UNIT','1.0','CLOSED_SPEC','BLOCKED_EXTERNAL_CONTRACT','SAXO_OPENAPI_ACCOUNT',NULL,NULL,NULL,'/api/v1/strategy-data/receipts?dataset_role=CURRENCY_AND_AMOUNT_UNIT',ARRAY['BLOCKED_EXTERNAL_CONTRACT_USD_ACCOUNT_QUANTUM'],ARRAY['EDR-09'],ARRAY['receipt_id','account_fingerprint','account_currency','currency_decimals','strategy_quantity_rule','minimum_trade_size','minimum_trade_value','amount_decimals','source_observed_at_utc','receipt_sha256'],'manifests/strategy_external_data_contract_manifest_v1.json','9e127e9fbed32b226b6a048c21868587e897c26bf437addaaa0ad9f214e25c97'),
    ('c2_edc10_revision_and_latency_state_v1','EDC-10','REVISION_AND_LATENCY_STATE','1.0','CLOSED_SPEC','BLOCKED_EXTERNAL_CONTRACT',NULL,NULL,NULL,NULL,'/api/v1/strategy-data/status',ARRAY['BLOCKED_EXTERNAL_CONTRACT_SOURCE_SLA'],ARRAY['EDR-10'],ARRAY['source_as_of','source_observed_at_utc','available_at_utc','accepted_at_utc','expected_by_utc','published_at_utc','freshness_state','quality_state','revision_state'],'manifests/strategy_external_data_contract_manifest_v1.json','9e127e9fbed32b226b6a048c21868587e897c26bf437addaaa0ad9f214e25c97');

CREATE OR REPLACE VIEW analytics.v_strategy_external_data_contract_status
WITH (security_barrier = true)
AS
WITH latest AS (
    SELECT r.*,
           ROW_NUMBER() OVER (
               PARTITION BY r.contract_id
               ORDER BY r.created_at_utc DESC, r.receipt_id DESC
           ) AS row_number
    FROM ops.strategy_external_data_receipt r
)
SELECT c.edc_id,
       c.dataset_role,
       c.contract_id,
       c.contract_state,
       CASE
           WHEN c.contract_state = 'BLOCKED_EXTERNAL_CONTRACT'
           THEN c.availability_state
           ELSE COALESCE(r.availability_state, c.availability_state)
       END AS availability_state,
       COALESCE(r.provider_id, c.provider_id) AS provider_id,
       COALESCE(r.dataset_id, c.dataset_id) AS dataset_id,
       c.price_basis,
       c.horizon_minutes,
       c.target_read_endpoint,
       r.receipt_id AS latest_receipt_id,
       CASE
           WHEN r.availability_state IN ('AVAILABLE','AVAILABLE_WITH_WARNINGS')
           THEN r.receipt_id
           ELSE NULL
       END AS last_good_receipt_id,
       r.source_as_of,
       r.source_observed_at_utc,
       r.available_at_utc,
       r.accepted_at_utc,
       r.expected_by_utc,
       r.published_at_utc,
       COALESCE(r.freshness_state, 'NOT_EVALUATED_SLA') AS freshness_state,
       COALESCE(r.quality_state, 'NOT_EVALUATED') AS quality_state,
       COALESCE(r.revision_state, 'NOT_EVALUATED') AS revision_state,
       COALESCE(r.cost_confidence, 'NOT_APPLICABLE') AS cost_confidence,
       COALESCE(r.warning_ids, ARRAY[]::TEXT[]) AS warning_ids,
       CASE
           WHEN c.contract_state = 'BLOCKED_EXTERNAL_CONTRACT'
           THEN c.blocker_ids
           ELSE COALESCE(r.blocker_ids, c.blocker_ids)
       END AS blocker_ids,
       c.decision_required_ids,
       r.provider_data_version,
       COALESCE(r.manifest_sha256, c.manifest_sha256) AS manifest_sha256,
       r.ordered_content_sha256,
       r.calendar_id
FROM catalog.strategy_external_data_contract c
LEFT JOIN latest r ON r.contract_id=c.contract_id AND r.row_number=1;

CREATE OR REPLACE VIEW analytics.v_strategy_external_data_receipt
WITH (security_barrier = true)
AS
SELECT r.receipt_id,
       c.edc_id,
       c.dataset_role,
       r.contract_id,
       r.availability_state,
       r.dataset_id,
       r.provider_id,
       r.provider_data_version,
       r.lineage_id,
       r.manifest_sha256,
       r.ordered_content_sha256,
       r.calendar_id,
       r.source_as_of,
       r.source_observed_at_utc,
       r.available_at_utc,
       r.accepted_at_utc,
       r.expected_by_utc,
       r.published_at_utc,
       r.freshness_state,
       r.quality_state,
       r.revision_state,
       r.cost_confidence,
       r.warning_ids,
       r.blocker_ids,
       r.values_modified,
       r.interpolation_performed,
       r.receipt_json AS payload,
       r.receipt_sha256,
       r.supersedes_receipt_id,
       r.created_at_utc
FROM ops.strategy_external_data_receipt r
JOIN catalog.strategy_external_data_contract c USING (contract_id);

REVOKE ALL ON catalog.strategy_external_data_contract FROM PUBLIC;
REVOKE ALL ON ops.strategy_external_data_receipt FROM PUBLIC;
REVOKE ALL ON analytics.v_strategy_external_data_contract_status FROM PUBLIC;
REVOKE ALL ON analytics.v_strategy_external_data_receipt FROM PUBLIC;
GRANT SELECT ON catalog.strategy_external_data_contract TO saxo_app_reader, saxo_analyst_reader;
GRANT SELECT ON analytics.v_strategy_external_data_contract_status TO saxo_app_reader, saxo_analyst_reader;
GRANT SELECT ON analytics.v_strategy_external_data_receipt TO saxo_app_reader, saxo_analyst_reader;
GRANT INSERT ON ops.strategy_external_data_receipt TO saxo_ingest;
