"""Fail-closed onboarding and acceptance for reviewed FX research candidates."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable

from psycopg import Error as PsycopgError
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .acquire_pages import ChartPage, fetch_chart_pages
from .connection import MARKET_DB, connect, project_root
from .fx_gap_report import generate_report
from .incremental_update import (
    CANDIDATE_DATASET_ID,
    CANDIDATE_INSTRUMENT_KEYS,
    CANDIDATE_SPEC_RELATIVE_PATH,
    AcquiredInstrument,
    InstrumentState,
    _commit_acquired,
    _create_run,
    _error_code,
    _record_failure,
    _validate_full_refetch_quarantine,
    _write_run_manifest,
    run_incremental,
)
from .instrument_registry import (
    CanonicalInstrument,
    InstrumentDriftError,
    load_research_candidate_instruments,
    validate_detail,
)
from .normalize_bars import (
    BarQualityError,
    NormalizedBar,
    RejectedBar,
    merge_pages,
    normalize_chart_page_quarantining_fx_extrema,
    parse_utc,
)
from .raw_artifacts import ArtifactRecord, RunArtifacts, canonical_json_bytes, utc_run_id
from .saxo_auth import DEFAULT_CALLBACK_PORT, OAuthConfig, SaxoAuthError, SaxoOAuthManager
from .saxo_client import SaxoClient


PUBLICATION_STATES = frozenset({"CANDIDATE", "STAGING", "PUBLISHED", "BLOCKED"})
RESEARCH_WARNING_AVAILABILITY = "AVAILABLE_WITH_WARNINGS"


def candidate_research_contract(instrument_key: str) -> dict[str, Any]:
    """Load the reviewed per-instrument research warning contract."""

    key = instrument_key.strip().lower()
    path = project_root() / CANDIDATE_SPEC_RELATIVE_PATH
    payload = json.loads(path.read_text(encoding="utf-8"))
    policy = payload.get("research_warning_policy") or {}
    matches = [item for item in payload.get("instruments", ()) if item.get("key") == key]
    if (
        payload.get("schema_version") != 2
        or policy.get("policy_id") != "fx_research_candidate_user_approved_warnings_v1"
        or policy.get("availability_status") != RESEARCH_WARNING_AVAILABILITY
        or policy.get("general_quality_rules_unchanged") is not True
        or len(matches) != 1
    ):
        raise BarQualityError("BLOCKED_CANDIDATE_RESEARCH_WARNING_CONTRACT_INVALID")
    selected = dict(matches[0])
    coverage = selected.get("coverage_contract") or {}
    if not all(
        isinstance(coverage.get(name), str) and coverage.get(name)
        for name in (
            "provider_advertised_start_utc",
            "effective_coverage_start_utc",
            "limitation",
        )
    ):
        raise BarQualityError("BLOCKED_CANDIDATE_COVERAGE_CONTRACT_INVALID")
    return {**selected, "research_warning_policy": dict(policy)}


def _validate_candidate_history_boundary(
    instrument_key: str,
    *,
    provider_advertised_start_utc: datetime,
    observed_start_utc: datetime,
) -> dict[str, Any]:
    contract = candidate_research_contract(instrument_key)
    coverage = contract["coverage_contract"]
    expected_advertised = parse_utc(coverage["provider_advertised_start_utc"])
    effective_start = parse_utc(coverage["effective_coverage_start_utc"])
    if provider_advertised_start_utc != expected_advertised:
        raise BarQualityError("BLOCKED_CANDIDATE_PROVIDER_ADVERTISED_START_DRIFT")
    if observed_start_utc > effective_start:
        raise BarQualityError("BLOCKED_CANDIDATE_EFFECTIVE_HISTORY_TRUNCATED")
    return {
        "provider_advertised_start_utc": expected_advertised,
        "effective_coverage_start_utc": effective_start,
        "observed_start_utc": observed_start_utc,
        "limitation": coverage["limitation"],
        "pre_effective_history_synthesized": False,
    }


def _warning_metadata(
    instrument_key: str,
    quarantined_rows: Iterable[RejectedBar] = (),
) -> dict[str, Any]:
    contract = candidate_research_contract(instrument_key)
    rows = tuple(sorted(quarantined_rows, key=lambda item: item.time_utc))
    result: dict[str, Any] = {
        "policy_id": contract["research_warning_policy"]["policy_id"],
        "scope": contract["research_warning_policy"]["scope"],
        "values_modified": False,
        "interpolation_performed": False,
        "raw_deleted": False,
        "coverage_contract": dict(contract["coverage_contract"]),
    }
    approved = contract.get("approved_provider_anomaly")
    if approved is not None:
        result["known_provider_anomaly"] = dict(approved)
    if rows:
        result["observed_quarantined_extrema"] = {
            "unique_rows": len(rows),
            "affected_from_utc": rows[0].time_utc.isoformat().replace("+00:00", "Z"),
            "affected_to_utc": rows[-1].time_utc.isoformat().replace("+00:00", "Z"),
            "fields": sorted(
                {
                    violation.field
                    for row in rows
                    for violation in row.violations
                }
            ),
            "values_modified": False,
            "curated_action": "excluded_without_synthesis",
        }
    return result


def candidate_instrument(instrument_key: str) -> CanonicalInstrument:
    key = instrument_key.strip().lower()
    matches = [item for item in load_research_candidate_instruments() if item.key == key]
    if len(matches) != 1:
        raise ValueError("instrument key must identify one reviewed FX research candidate")
    return matches[0]


def _ceil_hour(value: datetime) -> datetime:
    selected = value.astimezone(timezone.utc)
    rounded = selected.replace(minute=0, second=0, microsecond=0)
    return rounded if rounded == selected else rounded + timedelta(hours=1)


def _candidate_catalog_state(instrument: CanonicalInstrument) -> tuple[int, str]:
    """Bind a verified FX calendar without creating a bar or watermark."""

    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_fx_candidate_catalog") as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT metadata_json->>'verification_status'
                    FROM catalog.session_calendar
                    WHERE session_calendar_id='SBFX_24X5'
                    """
                )
                calendar = cursor.fetchone()
                if calendar is None or calendar[0] != "VERIFIED":
                    raise BarQualityError("BLOCKED_FX_CALENDAR_NOT_VERIFIED")
                cursor.execute(
                    """
                    SELECT instrument_id,market_key,symbol,uic,asset_type,currency,
                           session_calendar_id
                    FROM catalog.instrument
                    WHERE provider='Saxo OpenAPI' AND environment='SIM'
                      AND uic=%s AND asset_type=%s AND active_to_utc IS NULL
                    FOR UPDATE
                    """,
                    (instrument.uic, instrument.asset_type),
                )
                row = cursor.fetchone()
                if row is None or (
                    str(row[1]).lower(), str(row[2]).upper(), int(row[3]),
                    str(row[4]), str(row[5]),
                ) != (
                    instrument.key, instrument.symbol.upper(), instrument.uic,
                    instrument.asset_type, instrument.currency,
                ):
                    raise BarQualityError("BLOCKED_CANDIDATE_CATALOG_IDENTITY_MISMATCH")
                instrument_id = int(row[0])
                if row[6] not in (None, "SBFX_24X5"):
                    raise BarQualityError("BLOCKED_CANDIDATE_CALENDAR_DRIFT")
                cursor.execute(
                    """
                    SELECT publication_status
                    FROM catalog.series_publication_state
                    WHERE instrument_id=%s AND horizon_minutes=60
                      AND price_basis='bid_ask_mid'
                    FOR UPDATE
                    """,
                    (instrument_id,),
                )
                publication = cursor.fetchone()
                if publication is None or str(publication[0]) not in PUBLICATION_STATES:
                    raise BarQualityError("BLOCKED_CANDIDATE_PUBLICATION_STATE_MISSING")
                cursor.execute(
                    """
                    SELECT EXISTS (
                        SELECT 1 FROM ops.watermark
                        WHERE instrument_id=%s AND horizon_minutes=60
                          AND price_basis='bid_ask_mid'
                    )
                    """,
                    (instrument_id,),
                )
                if bool(cursor.fetchone()[0]):
                    raise BarQualityError("BLOCKED_CANDIDATE_ALREADY_ONBOARDED")
                cursor.execute(
                    """
                    UPDATE catalog.instrument SET session_calendar_id='SBFX_24X5'
                    WHERE instrument_id=%s
                    """,
                    (instrument_id,),
                )
    return instrument_id, str(publication[0])


def _update_publication(
    instrument_key: str,
    *,
    publication_status: str,
    quality_status: str,
    coverage_status: str,
    freshness_status: str,
    blocker_code: str | None,
    run_id: int | None,
    evidence_relative_path: str | None,
    evidence_sha256: str | None,
    consecutive_normal_passes: int | None = None,
    last_accepted_complete_time_utc: datetime | None = None,
    warning_metadata: dict[str, Any] | None = None,
) -> None:
    if publication_status not in PUBLICATION_STATES:
        raise ValueError("invalid publication status")
    contract = candidate_research_contract(instrument_key)
    policy = contract["research_warning_policy"]
    coverage = contract["coverage_contract"]
    consumer_availability = (
        RESEARCH_WARNING_AVAILABILITY
        if publication_status in {"STAGING", "PUBLISHED"}
        else "BLOCKED"
    )
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_fx_candidate_publish") as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE catalog.series_publication_state p SET
                        publication_status=%s,quality_status=%s,
                        coverage_status=%s,freshness_status=%s,blocker_code=%s,
                        last_evaluated_run_id=%s,
                        evidence_manifest_relative_path=%s,
                        evidence_manifest_sha256=%s,
                        consecutive_normal_passes=COALESCE(%s,p.consecutive_normal_passes),
                        last_accepted_complete_time_utc=COALESCE(
                            %s,p.last_accepted_complete_time_utc
                        ),
                        consumer_availability_status=%s,
                        research_policy_id=%s,
                        provider_advertised_start_utc=%s,
                        effective_coverage_start_utc=%s,
                        coverage_limitation=%s,
                        warning_metadata_json=COALESCE(%s,p.warning_metadata_json),
                        policy_approved_at_utc=%s,
                        policy_approved_by=%s,
                        updated_at_utc=clock_timestamp()
                    FROM catalog.instrument i
                    WHERE i.instrument_id=p.instrument_id AND i.market_key=%s
                      AND p.horizon_minutes=60 AND p.price_basis='bid_ask_mid'
                    """,
                    (
                        publication_status,
                        quality_status,
                        coverage_status,
                        freshness_status,
                        blocker_code,
                        run_id,
                        evidence_relative_path,
                        evidence_sha256,
                        consecutive_normal_passes,
                        last_accepted_complete_time_utc,
                        consumer_availability,
                        policy["policy_id"],
                        parse_utc(coverage["provider_advertised_start_utc"]),
                        parse_utc(coverage["effective_coverage_start_utc"]),
                        coverage["limitation"],
                        None if warning_metadata is None else Jsonb(warning_metadata),
                        parse_utc(policy["approved_at_utc"]),
                        policy["approved_by"],
                        instrument_key,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("FAILED_CANDIDATE_PUBLICATION_UPDATE")


def _publication_snapshot(instrument_key: str) -> dict[str, Any]:
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_fx_candidate_status") as conn:
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT i.instrument_id,i.market_key AS instrument_key,i.symbol,i.uic,
                       i.asset_type,i.session_calendar_id,p.publication_status,
                       p.quality_status,p.coverage_status,p.freshness_status,
                       p.blocker_code,p.last_evaluated_run_id,
                       p.evidence_manifest_relative_path,p.evidence_manifest_sha256,
                       p.last_accepted_complete_time_utc,
                       p.consecutive_normal_passes,p.updated_at_utc,
                       p.consumer_availability_status,p.research_policy_id,
                       p.provider_advertised_start_utc,p.effective_coverage_start_utc,
                       p.coverage_limitation,p.warning_metadata_json,
                       p.policy_approved_at_utc,p.policy_approved_by
                FROM catalog.instrument i
                JOIN catalog.series_publication_state p USING (instrument_id)
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                  AND i.market_key=%s AND p.horizon_minutes=60
                  AND p.price_basis='bid_ask_mid'
                """,
                (instrument_key,),
            )
            row = cursor.fetchone()
    if row is None:
        raise BarQualityError("BLOCKED_CANDIDATE_PUBLICATION_STATE_MISSING")
    return dict(row)


def candidate_status(instrument_keys: Iterable[str] = CANDIDATE_INSTRUMENT_KEYS) -> dict[str, Any]:
    selected = tuple(candidate_instrument(key).key for key in instrument_keys)
    states = [_publication_snapshot(key) for key in selected]
    return {
        "status": "PASS",
        "candidates": states,
        "orders_or_prechecks_sent": 0,
    }


def _retained_duplicate_audit(
    instrument_key: str,
    acquisition_run_id: str,
    *,
    source_database_ingestion_run_id: int,
) -> dict[str, int]:
    manifest_path = (
        project_root() / "data" / "acquisition" / "runs" / acquisition_run_id
        / "run_manifest.json"
    )
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if int(payload.get("database_ingestion_run_id", -1)) != source_database_ingestion_run_id:
        raise BarQualityError("BLOCKED_CANDIDATE_RETAINED_RUN_ID_MISMATCH")
    prefix = f"data/acquisition/runs/{acquisition_run_id}/instruments/{instrument_key}/chart_"
    records = sorted(
        (
            item for item in payload.get("artifacts", ())
            if str(item.get("relative_path", "")).startswith(prefix)
        ),
        key=lambda item: item["relative_path"],
    )
    if not records:
        raise BarQualityError("BLOCKED_CANDIDATE_RETAINED_CHART_ARTIFACTS_MISSING")
    raw_time_payloads: dict[datetime, bytes] = {}
    raw_occurrences = 0
    conflicting_boundaries = 0
    for record in records:
        path = project_root() / str(record["relative_path"])
        content = path.read_bytes()
        if (
            path.is_symlink()
            or len(content) != int(record["size_bytes"])
            or hashlib.sha256(content).hexdigest() != record["sha256"]
        ):
            raise BarQualityError("BLOCKED_CANDIDATE_RETAINED_ARTIFACT_INTEGRITY")
        page = json.loads(content)
        for sample in page.get("Data") or ():
            timestamp = parse_utc(str(sample.get("Time", "")))
            encoded = canonical_json_bytes(sample)
            raw_occurrences += 1
            previous = raw_time_payloads.setdefault(timestamp, encoded)
            if previous != encoded:
                conflicting_boundaries += 1
    return {
        "raw_occurrences": raw_occurrences,
        "unique_timestamps": len(raw_time_payloads),
        "inclusive_boundary_duplicates": raw_occurrences - len(raw_time_payloads),
        "conflicting_boundary_rows": conflicting_boundaries,
    }


def _retained_gap_result(
    instrument_key: str, acquisition_run_id: str
) -> tuple[dict[str, Any], ArtifactRecord]:
    path = (
        project_root() / "manifests" / "fx_research_candidates" / instrument_key
        / acquisition_run_id / "fx_gap_classification_manifest.json"
    )
    content = path.read_bytes()
    payload = json.loads(content)
    if (
        payload.get("status") not in {"PASS", "PASS_ACCOUNTED"}
        or instrument_key not in (payload.get("per_instrument") or {})
        or payload.get("interpolation_performed") is not False
        or int(payload.get("orders_or_prechecks_sent", -1)) != 0
    ):
        raise BarQualityError("BLOCKED_CANDIDATE_RETAINED_GAP_EVIDENCE_INVALID")
    for name, record in (payload.get("artifacts") or {}).items():
        artifact_path = path.parent / name
        artifact_content = artifact_path.read_bytes()
        if (
            artifact_path.is_symlink()
            or len(artifact_content) != int(record["size_bytes"])
            or hashlib.sha256(artifact_content).hexdigest() != record["sha256"]
        ):
            raise BarQualityError("BLOCKED_CANDIDATE_RETAINED_GAP_EVIDENCE_INTEGRITY")
    relative_path = str(path.relative_to(project_root()))
    return (
        {
            "status": "PASS",
            "summary": {"per_instrument": payload["per_instrument"]},
            "artifacts": {"manifest_relative_path": relative_path},
        },
        ArtifactRecord(
            relative_path=relative_path,
            sha256=hashlib.sha256(content).hexdigest(),
            size_bytes=len(content),
            row_count=1,
        ),
    )


def _finalization_state(instrument_key: str, database_ingestion_run_id: int) -> dict[str, Any]:
    with connect(
        "saxo_ingest", MARKET_DB, application_name="saxo_db_fx_candidate_finalize_state"
    ) as conn:
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT r.status,r.metadata_json->>'acquisition_run_id' AS acquisition_run_id,
                       w.latest_seen_time_utc,w.latest_complete_time_utc,w.data_version,
                       w.data_status,w.last_ingestion_run_id,
                       COUNT(*) OVER ()::BIGINT AS selected_rows,
                       (SELECT COUNT(*) FROM ops.ingestion_run_instrument_scope all_scope
                        WHERE all_scope.ingestion_run_id=r.ingestion_run_id)::BIGINT AS scope_rows
                FROM ops.ingestion_run r
                JOIN ops.ingestion_run_instrument_scope s
                  ON s.ingestion_run_id=r.ingestion_run_id
                JOIN catalog.instrument i ON i.instrument_id=s.instrument_id
                JOIN ops.watermark w ON w.instrument_id=i.instrument_id
                  AND w.horizon_minutes=60 AND w.price_basis='bid_ask_mid'
                WHERE r.ingestion_run_id=%s AND i.market_key=%s
                """,
                (database_ingestion_run_id, instrument_key),
            )
            row = cursor.fetchone()
    if (
        row is None
        or row["status"] != "PASS"
        or int(row["selected_rows"]) != 1
        or int(row["scope_rows"]) != 1
        or int(row["last_ingestion_run_id"]) != database_ingestion_run_id
        or row["data_status"] != "ACTIVE"
    ):
        raise BarQualityError("BLOCKED_CANDIDATE_FINALIZATION_STATE_MISMATCH")
    return dict(row)


def _observed_quarantine_metadata(
    instrument_key: str, database_ingestion_run_id: int
) -> dict[str, Any]:
    with connect(
        "saxo_ingest", MARKET_DB, application_name="saxo_db_fx_candidate_finalize_quarantine"
    ) as conn:
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT e.time_utc,e.observed_value
                FROM quality.event e
                JOIN catalog.instrument i USING (instrument_id)
                WHERE e.ingestion_run_id=%s AND i.market_key=%s
                  AND e.rule_id='db3_fx_crossed_extrema_quarantine'
                ORDER BY e.time_utc
                """,
                (database_ingestion_run_id, instrument_key),
            )
            rows = cursor.fetchall()
    result = _warning_metadata(instrument_key)
    if rows:
        result["observed_quarantined_extrema"] = {
            "unique_rows": len(rows),
            "affected_from_utc": rows[0]["time_utc"].isoformat().replace(
                "+00:00", "Z"
            ),
            "affected_to_utc": rows[-1]["time_utc"].isoformat().replace(
                "+00:00", "Z"
            ),
            "fields": sorted(
                {
                    str(item.get("field"))
                    for row in rows
                    for item in (row["observed_value"].get("violations") or ())
                }
            ),
            "values_modified": False,
            "curated_action": "excluded_without_synthesis",
        }
    approved = candidate_research_contract(instrument_key).get("approved_provider_anomaly")
    if approved is not None and (
        not rows
        or len(rows) != int(approved["unique_rows"])
        or rows[0]["time_utc"] != parse_utc(approved["affected_from_utc"])
        or rows[-1]["time_utc"] != parse_utc(approved["affected_to_utc"])
    ):
        raise BarQualityError("FX_EXTREMA_APPROVED_EXCEPTION_MISMATCH")
    return result


def finalize_candidate_onboarding(
    instrument_key: str,
    *,
    acquisition_run_id: str,
    database_ingestion_run_id: int,
    source_database_ingestion_run_id: int | None = None,
) -> dict[str, Any]:
    instrument = candidate_instrument(instrument_key)
    current = _publication_snapshot(instrument.key)
    if current["publication_status"] in {"STAGING", "PUBLISHED"}:
        return {
            "status": "PASS",
            "instrument_key": instrument.key,
            "publication_status": current["publication_status"],
            "idempotent": True,
            "orders_or_prechecks_sent": 0,
        }
    state = _finalization_state(instrument.key, database_ingestion_run_id)
    source_run_id = source_database_ingestion_run_id or database_ingestion_run_id
    duplicate_audit = _retained_duplicate_audit(
        instrument.key,
        acquisition_run_id,
        source_database_ingestion_run_id=source_run_id,
    )
    gap_result, gap_record = _retained_gap_result(instrument.key, acquisition_run_id)
    warning_metadata = _observed_quarantine_metadata(
        instrument.key, source_run_id
    )
    gate = _candidate_quality_gate(
        instrument.key,
        database_ingestion_run_id,
        gap_result,
        duplicate_audit,
        (),
    )
    gate["warning_metadata"] = warning_metadata
    if gate["status"] != "PASS":
        return {
            "status": "BLOCKED",
            "error_code": gate["error_code"],
            "instrument_key": instrument.key,
            "publication_status": "BLOCKED",
            "quality_gate": gate,
            "orders_or_prechecks_sent": 0,
        }
    artifacts = RunArtifacts(acquisition_run_id)
    evidence_record = artifacts.write_json(
        "candidate_quality_finalization.json",
        {
            "schema_version": 1,
            "status": "PASS",
            "instrument_key": instrument.key,
            "database_ingestion_run_id": database_ingestion_run_id,
            "source_acquisition_run_id": acquisition_run_id,
            "source_database_ingestion_run_id": source_run_id,
            "freshness_database_ingestion_run_id": database_ingestion_run_id,
            "quality_gate": gate,
            "gap_manifest_relative_path": gap_record.relative_path,
            "gap_manifest_sha256": gap_record.sha256,
            "post_commit_operational_recovery": True,
            "data_reacquired": False,
            "orders_or_prechecks_sent": 0,
        },
        row_count=1,
    )
    _update_publication(
        instrument.key,
        publication_status="STAGING",
        quality_status=str(gate["quality_status"]),
        coverage_status=str(gate["coverage_status"]),
        freshness_status=str(gate["freshness_status"]),
        blocker_code=None,
        run_id=database_ingestion_run_id,
        evidence_relative_path=evidence_record.relative_path,
        evidence_sha256=evidence_record.sha256,
        consecutive_normal_passes=0,
        last_accepted_complete_time_utc=state["latest_complete_time_utc"],
        warning_metadata=warning_metadata,
    )
    return {
        "status": "PASS",
        "instrument_key": instrument.key,
        "publication_status": "STAGING",
        "database_ingestion_run_id": database_ingestion_run_id,
        "acquisition_run_id": acquisition_run_id,
        "quality_gate": gate,
        "evidence_relative_path": evidence_record.relative_path,
        "orders_or_prechecks_sent": 0,
    }


def _publication_watermark(instrument_key: str) -> dict[str, Any]:
    with connect(
        "saxo_ingest", MARKET_DB, application_name="saxo_db_fx_candidate_watermark"
    ) as conn:
        with conn.cursor(row_factory=dict_row) as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT w.latest_seen_time_utc,w.latest_complete_time_utc,
                       w.data_version,w.data_status,w.last_ingestion_run_id
                FROM ops.watermark w
                JOIN catalog.instrument i USING (instrument_id)
                WHERE i.market_key=%s AND w.horizon_minutes=60
                  AND w.price_basis='bid_ask_mid'
                """,
                (instrument_key,),
            )
            row = cursor.fetchone()
    if row is None:
        raise BarQualityError("BLOCKED_CANDIDATE_WATERMARK_MISSING")
    return dict(row)


def _candidate_quality_gate(
    instrument_key: str,
    run_id: int,
    gap_result: dict[str, Any],
    duplicate_audit: dict[str, int],
    quarantined_rows: tuple[RejectedBar, ...],
) -> dict[str, Any]:
    contract = candidate_research_contract(instrument_key)
    effective_start = parse_utc(
        contract["coverage_contract"]["effective_coverage_start_utc"]
    )
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_fx_candidate_gate") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute(
                    """
                    SELECT i.instrument_id,w.data_status,w.data_version,
                           w.latest_seen_time_utc,w.latest_complete_time_utc,
                           w.last_ingestion_run_id,c.coverage_status,f.freshness_status,
                           r.status AS run_status,
                           MIN(b.time_utc) AS first_observed_time_utc,
                           COUNT(b.*)::BIGINT AS actual_rows,
                           COUNT(b.*) FILTER (WHERE b.is_complete)::BIGINT AS complete_rows,
                           COUNT(b.*)-COUNT(DISTINCT b.time_utc)::BIGINT AS duplicate_rows,
                           COUNT(DISTINCT b.data_version)::BIGINT AS data_version_count,
                           COUNT(b.*) FILTER (WHERE b.data_version IS NULL)::BIGINT
                               AS null_data_version_rows,
                           COUNT(b.*) FILTER (WHERE
                               b.open_bid IS NULL OR b.high_bid IS NULL OR b.low_bid IS NULL OR b.close_bid IS NULL OR
                               b.open_ask IS NULL OR b.high_ask IS NULL OR b.low_ask IS NULL OR b.close_ask IS NULL
                           )::BIGINT AS null_bid_ask_rows,
                           COUNT(b.*) FILTER (WHERE LEAST(
                               b.open_bid,b.high_bid,b.low_bid,b.close_bid,
                               b.open_ask,b.high_ask,b.low_ask,b.close_ask
                           ) <= 0)::BIGINT AS nonpositive_bid_ask_rows,
                           COUNT(b.*) FILTER (WHERE
                               b.high_bid < GREATEST(b.open_bid,b.low_bid,b.close_bid) OR
                               b.low_bid > LEAST(b.open_bid,b.high_bid,b.close_bid) OR
                               b.high_ask < GREATEST(b.open_ask,b.low_ask,b.close_ask) OR
                               b.low_ask > LEAST(b.open_ask,b.high_ask,b.close_ask)
                           )::BIGINT AS side_ohlc_violation_rows,
                           COUNT(b.*) FILTER (WHERE
                               b.open_bid>b.open_ask OR b.high_bid>b.high_ask OR
                               b.low_bid>b.low_ask OR b.close_bid>b.close_ask
                           )::BIGINT AS crossed_bid_ask_rows,
                           COUNT(b.*) FILTER (WHERE
                               b.high < GREATEST(b.open,b.low,b.close) OR
                               b.low > LEAST(b.open,b.high,b.close)
                           )::BIGINT AS midpoint_ohlc_violation_rows,
                           COUNT(b.*) FILTER (WHERE
                               b.is_complete AND b.time_utc>clock_timestamp()
                           )::BIGINT AS future_complete_rows
                    FROM catalog.instrument i
                    JOIN ops.watermark w ON w.instrument_id=i.instrument_id
                      AND w.horizon_minutes=60 AND w.price_basis='bid_ask_mid'
                    JOIN analytics.v_data_coverage c ON c.instrument_id=i.instrument_id
                      AND c.horizon_minutes=60 AND c.price_basis='bid_ask_mid'
                    JOIN analytics.v_data_freshness f ON f.instrument_id=i.instrument_id
                      AND f.horizon_minutes=60 AND f.price_basis='bid_ask_mid'
                    JOIN ops.ingestion_run r ON r.ingestion_run_id=w.last_ingestion_run_id
                    JOIN curated.market_bar b ON b.instrument_id=i.instrument_id
                      AND b.horizon_minutes=60 AND b.price_basis='bid_ask_mid'
                    WHERE i.market_key=%s
                    GROUP BY i.instrument_id,w.data_status,w.data_version,
                             w.latest_seen_time_utc,w.latest_complete_time_utc,
                             w.last_ingestion_run_id,c.coverage_status,
                             f.freshness_status,r.status
                    """,
                    (instrument_key,),
                )
                profile = cursor.fetchone()
                cursor.execute(
                    """
                    SELECT COUNT(*)::BIGINT AS current_blockers,
                           COUNT(*) FILTER (WHERE applicability='UNKNOWN')::BIGINT AS unknown_blockers
                    FROM quality.v_open_event e
                    WHERE e.severity IN ('ERROR','CRITICAL') AND e.current_blocker
                      AND (
                        e.instrument_key=%s
                        OR e.scope_kind IN ('GLOBAL','UNKNOWN','DATASET','LAYER')
                        OR (
                          e.instrument_id IS NULL AND e.scope_kind='RUN'
                          AND (
                            NOT (e.scope_evidence ? 'selected_instrument_keys')
                            OR (e.scope_evidence->'selected_instrument_keys') ? %s
                          )
                        )
                      )
                      AND (
                        e.scope_kind='UNKNOWN'
                        OR (
                          (e.affected_layer IS NULL OR e.affected_layer='curated')
                          AND (e.horizon_minutes IS NULL OR e.horizon_minutes=60)
                          AND (e.price_basis IS NULL OR e.price_basis='bid_ask_mid')
                        )
                      )
                    """,
                    (instrument_key, instrument_key),
                )
                blockers = cursor.fetchone()
    if profile is None or blockers is None:
        return {
            "status": "BLOCKED",
            "error_code": "BLOCKED_CANDIDATE_GATE_COMPONENT_MISSING",
            "quality_status": "NOT_EVALUATED",
            "coverage_status": "NOT_EVALUATED",
            "freshness_status": "NOT_EVALUATED",
        }
    selected = dict(profile)
    selected.update(dict(blockers))
    gap_summary = gap_result.get("summary", {}).get("per_instrument", {}).get(instrument_key, {})
    checks = {
        "run_pass": selected.get("run_status") == "PASS" and selected.get("last_ingestion_run_id") == run_id,
        "watermark_active": selected.get("data_status") == "ACTIVE",
        "data_version_single": (
            selected.get("data_version") is not None
            and selected.get("data_version_count") == 1
            and int(selected.get("null_data_version_rows", -1)) == 0
        ),
        "coverage_accounted": (
            selected.get("coverage_status") in {"PASS", "WARN"}
            and gap_result.get("status") == "PASS"
            and int(gap_summary.get("blocking_rows", -1)) == 0
            and int(gap_summary.get("unclassified_rows", -1)) == 0
        ),
        "effective_coverage_start_reached": (
            selected.get("first_observed_time_utc") is not None
            and selected.get("first_observed_time_utc") <= effective_start
        ),
        "freshness_pass": selected.get("freshness_status") == "PASS",
        "curated_duplicate_zero": int(selected.get("duplicate_rows", -1)) == 0,
        "page_boundary_duplicates_audited": (
            int(duplicate_audit.get("inclusive_boundary_duplicates", -1)) >= 0
            and int(duplicate_audit.get("conflicting_boundary_rows", -1)) >= 0
            and int(duplicate_audit.get("conflicting_boundary_rows", -1))
            <= int(duplicate_audit.get("inclusive_boundary_duplicates", -1))
        ),
        "null_zero": int(selected.get("null_bid_ask_rows", -1)) == 0,
        "nonpositive_zero": int(selected.get("nonpositive_bid_ask_rows", -1)) == 0,
        "side_ohlc_valid": int(selected.get("side_ohlc_violation_rows", -1)) == 0,
        "midpoint_ohlc_valid": int(selected.get("midpoint_ohlc_violation_rows", -1)) == 0,
        "bid_ask_valid": int(selected.get("crossed_bid_ask_rows", -1)) == 0,
        "future_complete_zero": int(selected.get("future_complete_rows", -1)) == 0,
        "current_blockers_zero": int(selected.get("current_blockers", -1)) == 0,
        "unknown_blockers_zero": int(selected.get("unknown_blockers", -1)) == 0,
        "latest_sample_forming": selected.get("latest_complete_time_utc") < selected.get("latest_seen_time_utc"),
    }
    failed = [name for name, passed in checks.items() if not passed]
    warning_metadata = _warning_metadata(instrument_key, quarantined_rows)
    return {
        "status": "PASS" if not failed else "BLOCKED",
        "error_code": None if not failed else "BLOCKED_CANDIDATE_QUALITY_GATE",
        "failed_checks": failed,
        "checks": checks,
        "quality_status": "WARN" if not failed else "FAIL",
        "coverage_status": str(selected.get("coverage_status") or "NOT_EVALUATED"),
        "freshness_status": str(selected.get("freshness_status") or "NOT_EVALUATED"),
        "profile": selected,
        "gap_summary": gap_summary,
        "duplicate_audit": duplicate_audit,
        "consumer_availability_status": (
            RESEARCH_WARNING_AVAILABILITY if not failed else "BLOCKED"
        ),
        "research_policy_id": contract["research_warning_policy"]["policy_id"],
        "warning_metadata": warning_metadata,
        "interpolation_performed": False,
        "orders_or_prechecks_sent": 0,
    }


def run_candidate_onboarding(
    instrument_key: str,
    client: SaxoClient | None = None,
    *,
    client_factory: Callable[[], SaxoClient] | None = None,
) -> dict[str, Any]:
    instrument = candidate_instrument(instrument_key)
    contract = candidate_research_contract(instrument.key)
    approved_exception = contract.get("approved_provider_anomaly")
    run_id = utc_run_id(secrets.token_hex(4))
    artifacts = RunArtifacts(run_id)
    db_run_id = _create_run(run_id, (instrument,), trigger="manual_fx_candidate_onboarding")
    chart_artifacts: list[ArtifactRecord] = []
    all_artifacts: list[ArtifactRecord] = []
    selected_client = client
    smoke_result: dict[str, Any] | None = None
    acquisition_result: dict[str, Any] = {}
    raw_time_payloads: dict[datetime, bytes] = {}
    raw_occurrences = 0
    conflicting_boundaries = 0
    database_committed = False
    try:
        instrument_id, _ = _candidate_catalog_state(instrument)
        if selected_client is not None and client_factory is not None:
            raise ValueError("client and client_factory are mutually exclusive")
        selected_client = selected_client or (
            client_factory() if client_factory is not None else SaxoClient.from_environment()
        )
        smoke_result = selected_client.smoke_test()
        detail = selected_client.instrument_detail(instrument.uic, instrument.asset_type)
        validate_detail(instrument, detail)
        all_artifacts.append(
            artifacts.write_json(f"instruments/{instrument.key}/detail.json", detail, row_count=1)
        )
        schedule = selected_client.trading_schedule(instrument.uic, instrument.asset_type)
        if not isinstance(schedule.get("Sessions"), list):
            raise BarQualityError("INVALID_TRADING_SCHEDULE")
        all_artifacts.append(
            artifacts.write_json(
                f"instruments/{instrument.key}/trading_schedule.json",
                schedule,
                row_count=len(schedule["Sessions"]),
            )
        )
        normalized_pages: list[list[NormalizedBar]] = []
        rejected_pages: list[list[RejectedBar]] = []
        page_versions: set[int] = set()
        first_sample_times: set[datetime] = set()

        def save_page(page: ChartPage) -> None:
            nonlocal raw_occurrences, conflicting_boundaries
            record = artifacts.write_json(
                f"instruments/{instrument.key}/chart_{page.page_number:04d}.json",
                page.payload,
                row_count=len(page.payload.get("Data") or []),
            )
            chart_artifacts.append(record)
            all_artifacts.append(record)
            version = page.payload.get("DataVersion")
            if version is not None:
                page_versions.add(int(version))
            first_sample = (page.payload.get("ChartInfo") or {}).get("FirstSampleTime")
            if first_sample:
                first_sample_times.add(parse_utc(str(first_sample)))
            for sample in page.payload.get("Data") or []:
                timestamp = parse_utc(str(sample.get("Time", "")))
                encoded = canonical_json_bytes(sample)
                raw_occurrences += 1
                previous = raw_time_payloads.setdefault(timestamp, encoded)
                if previous != encoded:
                    conflicting_boundaries += 1
            normalized, rejected = normalize_chart_page_quarantining_fx_extrema(
                instrument,
                page.payload,
                retrieved_at_utc=datetime.now(timezone.utc),
                payload_sha256=record.sha256,
                artifact_relative_path=record.relative_path,
            )
            normalized_pages.append(normalized)
            rejected_pages.append(rejected)

        pages = fetch_chart_pages(
            selected_client,
            instrument,
            mode="UpTo",
            time_utc=datetime.now(timezone.utc),
            on_page=save_page,
        )
        if len(page_versions) != 1:
            raise BarQualityError("MULTIPLE_DATA_VERSIONS_IN_RUN")
        if len(first_sample_times) != 1:
            raise BarQualityError("BLOCKED_CANDIDATE_FIRST_SAMPLE_TIME_AMBIGUOUS")
        bars = tuple(merge_pages(normalized_pages))
        quarantined_rows = _validate_full_refetch_quarantine(
            (bar.time_utc for bar in bars),
            (rejected for page in rejected_pages for rejected in page),
            approved_exception=approved_exception,
        )
        observed_times = [
            *(bar.time_utc for bar in bars),
            *(rejected.time_utc for rejected in quarantined_rows),
        ]
        if len(bars) < 2 or not observed_times:
            raise BarQualityError("BLOCKED_CANDIDATE_HISTORY_EMPTY")
        first_sample_time = next(iter(first_sample_times))
        coverage_contract = _validate_candidate_history_boundary(
            instrument.key,
            provider_advertised_start_utc=first_sample_time,
            observed_start_utc=min(observed_times),
        )
        if len(pages[-1].payload.get("Data") or []) >= 1200:
            raise BarQualityError("BLOCKED_CANDIDATE_PROVIDER_BOUNDARY_NOT_REACHED")
        if any(bar.is_complete and bar.time_utc > datetime.now(timezone.utc) for bar in bars):
            raise BarQualityError("FUTURE_COMPLETED_BAR")
        duplicate_audit = {
            "raw_occurrences": raw_occurrences,
            "unique_timestamps": len(raw_time_payloads),
            "inclusive_boundary_duplicates": raw_occurrences - len(raw_time_payloads),
            "conflicting_boundary_rows": conflicting_boundaries,
        }
        # Saxo UpTo pages are inclusive at the page boundary.  Every duplicate
        # occurrence remains immutable in raw while merge_pages applies the
        # reviewed retain-first-seen rule.  The audit counts therefore remain
        # visible evidence; no value is swapped, clamped, interpolated or
        # rewritten merely because adjacent pages disagree at that boundary.
        state = InstrumentState(
            instrument_id=instrument_id,
            latest_complete_time_utc=max(bar.time_utc for bar in bars if bar.is_complete),
            data_version=None,
            data_status="CANDIDATE",
        )
        acquisition_result = _commit_acquired(
            db_run_id,
            [AcquiredInstrument(instrument, state, bars)],
            chart_artifacts,
            quarantined_rows=quarantined_rows,
            dataset_id=CANDIDATE_DATASET_ID,
            bootstrap_watermark=True,
            approved_quarantine_exception=approved_exception,
        )
        database_committed = True
        gap_output = (
            project_root()
            / "manifests"
            / "fx_research_candidates"
            / instrument.key
            / run_id
        )
        gap_result = generate_report(gap_output, (instrument.key,))
        gate = _candidate_quality_gate(
            instrument.key,
            db_run_id,
            gap_result,
            duplicate_audit,
            quarantined_rows,
        )
        evidence = {
            "schema_version": 2,
            "instrument_key": instrument.key,
            "uic": instrument.uic,
            "asset_type": instrument.asset_type,
            "horizon_minutes": 60,
            "price_basis": instrument.price_basis,
            "source_dataset_id": CANDIDATE_DATASET_ID,
            "data_version": next(iter(page_versions)),
            "first_sample_time_utc": first_sample_time,
            "coverage_contract": coverage_contract,
            "research_warning_policy": contract["research_warning_policy"],
            "approved_provider_anomaly": approved_exception,
            "page_count": len(pages),
            "quality_gate": gate,
            "gap_artifacts": gap_result.get("artifacts"),
            "raw_artifact_count": len(chart_artifacts),
            "write_requests": 0 if selected_client is None else selected_client.write_request_count,
            "prechecks": 0,
            "orders": 0,
        }
        evidence_record = artifacts.write_json(
            "candidate_quality_evidence.json", evidence, row_count=1
        )
        all_artifacts.append(evidence_record)
        manifest = _write_run_manifest(
            artifacts,
            db_run_id=db_run_id,
            status="PASS",
            error_code=None,
            smoke_result=smoke_result,
            successful_series=1,
            client=selected_client,
            all_artifacts=all_artifacts,
            result={**acquisition_result, "candidate_quality_gate": gate},
        )
        publishable = gate.get("status") == "PASS"
        _update_publication(
            instrument.key,
            publication_status="STAGING" if publishable else "BLOCKED",
            quality_status=str(gate.get("quality_status") or "NOT_EVALUATED"),
            coverage_status=str(gate.get("coverage_status") or "NOT_EVALUATED"),
            freshness_status=str(gate.get("freshness_status") or "NOT_EVALUATED"),
            blocker_code=None if publishable else str(gate.get("error_code")),
            run_id=db_run_id,
            evidence_relative_path=manifest.relative_path,
            evidence_sha256=manifest.sha256,
            consecutive_normal_passes=0,
            last_accepted_complete_time_utc=gate.get("profile", {}).get(
                "latest_complete_time_utc"
            ),
            warning_metadata=gate.get("warning_metadata"),
        )
        return {
            "status": "PASS" if publishable else "BLOCKED",
            "error_code": None if publishable else gate.get("error_code"),
            "instrument_key": instrument.key,
            "publication_status": "STAGING" if publishable else "BLOCKED",
            "acquisition_run_id": run_id,
            "database_ingestion_run_id": db_run_id,
            "manifest_relative_path": manifest.relative_path,
            "quality_gate": gate,
            "orders_or_prechecks_sent": 0,
            **acquisition_result,
        }
    except Exception as exc:
        code = _error_code(exc)
        content_quality_failure = isinstance(
            exc, (BarQualityError, InstrumentDriftError)
        )
        if not database_committed:
            _record_failure(
                db_run_id,
                code,
                chart_artifacts,
                (instrument.key,),
                dataset_id=CANDIDATE_DATASET_ID,
                spec_relative_path=CANDIDATE_SPEC_RELATIVE_PATH,
                dataset_name="Saxo SIM FX research candidates 60m chart",
                research_eligibility="SIM_RESEARCH_CANDIDATE",
            )
        manifest = _write_run_manifest(
            artifacts,
            db_run_id=db_run_id,
            status="BLOCKED" if code.startswith("BLOCKED") else "FAILED",
            error_code=code,
            smoke_result=smoke_result,
            successful_series=0,
            client=selected_client,
            all_artifacts=all_artifacts,
            result=acquisition_result,
            failed_instrument_key=instrument.key,
        )
        _update_publication(
            instrument.key,
            publication_status="BLOCKED",
            quality_status="FAIL" if content_quality_failure else "NOT_EVALUATED",
            coverage_status="NOT_EVALUATED",
            freshness_status="NOT_EVALUATED",
            blocker_code=code,
            run_id=db_run_id,
            evidence_relative_path=manifest.relative_path,
            evidence_sha256=manifest.sha256,
            consecutive_normal_passes=0,
        )
        return {
            "status": "BLOCKED" if code.startswith("BLOCKED") else "FAILED",
            "error_code": code,
            "instrument_key": instrument.key,
            "publication_status": "BLOCKED",
            "acquisition_run_id": run_id,
            "database_ingestion_run_id": db_run_id,
            "manifest_relative_path": manifest.relative_path,
            "orders_or_prechecks_sent": 0,
        }


def run_candidate_acceptance(
    instrument_key: str,
    *,
    client_factory: Callable[[], SaxoClient],
    required_passes: int = 2,
) -> dict[str, Any]:
    instrument = candidate_instrument(instrument_key)
    if required_passes != 2:
        raise ValueError("candidate acceptance requires exactly two normal passes")
    initial = _publication_snapshot(instrument.key)
    if initial["publication_status"] == "PUBLISHED" and initial["consecutive_normal_passes"] == 2:
        return {
            "status": "PASS",
            "instrument_key": instrument.key,
            "publication_status": "PUBLISHED",
            "consecutive_normal_passes": 2,
            "idempotent": True,
            "orders_or_prechecks_sent": 0,
            "runs": [],
        }
    if initial["publication_status"] != "STAGING":
        return {
            "status": "BLOCKED",
            "error_code": "BLOCKED_CANDIDATE_NOT_STAGING",
            "instrument_key": instrument.key,
            "publication_status": initial["publication_status"],
            "orders_or_prechecks_sent": 0,
            "runs": [],
        }
    previous_complete = initial.get("last_accepted_complete_time_utc")
    if not isinstance(previous_complete, datetime):
        return {
            "status": "BLOCKED",
            "error_code": "BLOCKED_CANDIDATE_ACCEPTANCE_BASELINE_MISSING",
            "instrument_key": instrument.key,
            "publication_status": str(initial["publication_status"]),
            "orders_or_prechecks_sent": 0,
            "runs": [],
        }
    result = run_incremental(
        client=client_factory(),
        instrument_keys=(instrument.key,),
        trigger="manual_fx_candidate_acceptance",
    )
    runs = [result]
    if result.get("status") != "PASS":
        code = str(result.get("error_code") or "FAILED_CANDIDATE_ACCEPTANCE")
        _update_publication(
            instrument.key,
            publication_status="BLOCKED",
            quality_status=str(initial["quality_status"]),
            coverage_status=str(initial["coverage_status"]),
            freshness_status=str(initial["freshness_status"]),
            blocker_code=code,
            run_id=result.get("database_ingestion_run_id"),
            evidence_relative_path=initial["evidence_manifest_relative_path"],
            evidence_sha256=initial["evidence_manifest_sha256"],
            consecutive_normal_passes=0,
        )
        return {
            "status": str(result.get("status") or "FAILED"),
            "error_code": code,
            "instrument_key": instrument.key,
            "publication_status": "BLOCKED",
            "consecutive_normal_passes": 0,
            "orders_or_prechecks_sent": 0,
            "runs": runs,
        }
    latest = _publication_watermark(instrument.key)
    observed_complete = latest.get("latest_complete_time_utc")
    if not isinstance(observed_complete, datetime) or observed_complete <= previous_complete:
        return {
            "status": "BLOCKED",
            "error_code": "DATA_NOT_READY_CANDIDATE_WATERMARK_NOT_ADVANCED",
            "instrument_key": instrument.key,
            "publication_status": "STAGING",
            "consecutive_normal_passes": int(initial["consecutive_normal_passes"]),
            "previous_complete_time_utc": previous_complete,
            "observed_complete_time_utc": observed_complete,
            "orders_or_prechecks_sent": 0,
            "runs": runs,
        }
    consecutive = int(initial["consecutive_normal_passes"]) + 1
    if consecutive not in {1, 2}:
        raise BarQualityError("BLOCKED_CANDIDATE_ACCEPTANCE_COUNTER_INVALID")
    warning_metadata = dict(initial.get("warning_metadata_json") or {})
    acceptance_history = list(warning_metadata.get("normal_acceptance_runs") or ())
    if not acceptance_history and int(initial["consecutive_normal_passes"]) == 1:
        acceptance_history.append(
            {
                "database_ingestion_run_id": int(initial["last_evaluated_run_id"]),
                "accepted_complete_time_utc": previous_complete.isoformat().replace(
                    "+00:00", "Z"
                ),
            }
        )
    acceptance_history.append(
        {
            "database_ingestion_run_id": int(result["database_ingestion_run_id"]),
            "accepted_complete_time_utc": observed_complete.isoformat().replace(
                "+00:00", "Z"
            ),
        }
    )
    if len(acceptance_history) != consecutive or len(
        {item["accepted_complete_time_utc"] for item in acceptance_history}
    ) != consecutive:
        raise BarQualityError("BLOCKED_CANDIDATE_ACCEPTANCE_HISTORY_INVALID")
    warning_metadata["normal_acceptance_runs"] = acceptance_history
    _update_publication(
        instrument.key,
        publication_status="PUBLISHED" if consecutive == required_passes else "STAGING",
        quality_status=str(initial["quality_status"]),
        coverage_status=str(initial["coverage_status"]),
        freshness_status="PASS",
        blocker_code=None,
        run_id=result.get("database_ingestion_run_id"),
        evidence_relative_path=initial["evidence_manifest_relative_path"],
        evidence_sha256=initial["evidence_manifest_sha256"],
        consecutive_normal_passes=consecutive,
        last_accepted_complete_time_utc=observed_complete,
        warning_metadata=warning_metadata,
    )
    return {
        "status": "PASS",
        "instrument_key": instrument.key,
        "publication_status": "PUBLISHED" if consecutive == required_passes else "STAGING",
        "consecutive_normal_passes": consecutive,
        "normal_pass_run_ids": [
            item["database_ingestion_run_id"] for item in acceptance_history
        ],
        "accepted_complete_time_utc": observed_complete,
        "orders_or_prechecks_sent": 0,
        "runs": runs,
    }


def _client_factory(
    auth_mode: str, callback_port: int, *, force_refresh: bool = False
) -> Callable[[], SaxoClient]:
    if auth_mode == "environment":
        return SaxoClient.from_environment
    manager = SaxoOAuthManager(OAuthConfig.from_environment(callback_port=callback_port))

    def build() -> SaxoClient:
        token = manager.access_token(force_refresh=force_refresh)
        try:
            return SaxoClient(token)
        finally:
            token = ""

    return build


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Onboard reviewed Saxo SIM FX research candidates")
    parser.add_argument("command", choices=("onboard", "finalize", "accept", "status"))
    parser.add_argument("--instrument-key")
    parser.add_argument("--acquisition-run-id")
    parser.add_argument("--database-ingestion-run-id", type=int)
    parser.add_argument("--source-database-ingestion-run-id", type=int)
    parser.add_argument("--auth-mode", choices=("environment", "keychain"), default="environment")
    parser.add_argument("--callback-port", type=int, default=DEFAULT_CALLBACK_PORT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "status":
        result = candidate_status()
    elif args.command == "finalize":
        if (
            not args.instrument_key
            or not args.acquisition_run_id
            or args.database_ingestion_run_id is None
        ):
            parser.error(
                "finalize requires --instrument-key, --acquisition-run-id, and "
                "--database-ingestion-run-id"
            )
        try:
            result = finalize_candidate_onboarding(
                args.instrument_key,
                acquisition_run_id=args.acquisition_run_id,
                database_ingestion_run_id=args.database_ingestion_run_id,
                source_database_ingestion_run_id=args.source_database_ingestion_run_id,
            )
        except (BarQualityError, RuntimeError, ValueError, PsycopgError) as exc:
            result = {
                "status": "BLOCKED",
                "error_code": _error_code(exc),
                "instrument_key": args.instrument_key.strip().lower(),
                "orders_or_prechecks_sent": 0,
            }
    else:
        if not args.instrument_key:
            parser.error(f"{args.command} requires --instrument-key")
        try:
            factory = _client_factory(
                args.auth_mode,
                args.callback_port,
                force_refresh=args.command == "onboard",
            )
            if args.command == "onboard":
                result = run_candidate_onboarding(args.instrument_key, client_factory=factory)
            else:
                result = run_candidate_acceptance(args.instrument_key, client_factory=factory)
        except SaxoAuthError as exc:
            result = {
                "status": "BLOCKED",
                "error_code": exc.code,
                "instrument_key": args.instrument_key.strip().lower(),
                "error_domain": "interface_operational",
                "token_values_exposed": False,
                "orders_or_prechecks_sent": 0,
            }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if result.get("status") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
