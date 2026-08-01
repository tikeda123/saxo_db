SET LOCAL ROLE saxo_db_owner;

CREATE OR REPLACE VIEW analytics.v_strategy_external_data_contract_status
WITH (security_barrier = true)
AS
WITH ranked AS (
    SELECT r.*,
           ROW_NUMBER() OVER (
               PARTITION BY r.contract_id
               ORDER BY r.created_at_utc DESC, r.receipt_id DESC
           ) AS latest_number
    FROM ops.strategy_external_data_receipt r
), last_good AS (
    SELECT DISTINCT ON (contract_id) contract_id,receipt_id
    FROM ops.strategy_external_data_receipt
    WHERE availability_state IN ('AVAILABLE','AVAILABLE_WITH_WARNINGS')
      AND accepted_at_utc IS NOT NULL
    ORDER BY contract_id,created_at_utc DESC,receipt_id DESC
)
SELECT c.edc_id,
       c.dataset_role,
       c.contract_id,
       c.contract_state,
       COALESCE(r.availability_state,c.availability_state) AS availability_state,
       COALESCE(r.provider_id,c.provider_id) AS provider_id,
       COALESCE(r.dataset_id,c.dataset_id) AS dataset_id,
       c.price_basis,
       c.horizon_minutes,
       c.target_read_endpoint,
       r.receipt_id AS latest_receipt_id,
       g.receipt_id AS last_good_receipt_id,
       r.source_as_of,
       r.source_observed_at_utc,
       r.available_at_utc,
       r.accepted_at_utc,
       r.expected_by_utc,
       r.published_at_utc,
       COALESCE(r.freshness_state,'NOT_EVALUATED_SLA') AS freshness_state,
       COALESCE(r.quality_state,'NOT_EVALUATED') AS quality_state,
       COALESCE(r.revision_state,'NOT_EVALUATED') AS revision_state,
       COALESCE(r.cost_confidence,'NOT_APPLICABLE') AS cost_confidence,
       COALESCE(r.warning_ids,ARRAY[]::TEXT[]) AS warning_ids,
       COALESCE(r.blocker_ids,c.blocker_ids) AS blocker_ids,
       c.decision_required_ids,
       r.provider_data_version,
       COALESCE(r.manifest_sha256,c.manifest_sha256) AS manifest_sha256,
       r.ordered_content_sha256,
       r.calendar_id
FROM catalog.strategy_external_data_contract c
LEFT JOIN ranked r ON r.contract_id=c.contract_id AND r.latest_number=1
LEFT JOIN last_good g ON g.contract_id=c.contract_id;

REVOKE ALL ON analytics.v_strategy_external_data_contract_status FROM PUBLIC;
GRANT SELECT ON analytics.v_strategy_external_data_contract_status
TO saxo_app_reader,saxo_analyst_reader;
