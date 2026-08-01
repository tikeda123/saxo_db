"""Atomic DB3 incremental acquisition for the canonical Saxo SIM universe."""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from psycopg.types.json import Jsonb

from .acquire_pages import ChartPage, fetch_chart_pages
from .connection import MARKET_DB, connect, project_root
from .c2_imputation import refresh_c2_imputation_overlay
from .derive_bars import rebuild
from .instrument_registry import (
    CanonicalInstrument,
    InstrumentDriftError,
    load_canonical_instruments,
    load_research_candidate_instruments,
    validate_detail,
)
from .normalize_bars import (
    BarQualityError,
    NormalizedBar,
    RejectedBar,
    mark_terminal_session_bar_complete,
    merge_pages,
    normalize_chart_page,
    normalize_chart_page_quarantining_fx_extrema,
)
from .raw_artifacts import ArtifactRecord, RunArtifacts, utc_run_id
from .saxo_auth import DEFAULT_CALLBACK_PORT, OAuthConfig, SaxoAuthError, SaxoOAuthManager
from .saxo_client import SaxoAPIError, SaxoClient


DATASET_ID = "v13_saxo_sim_chart_60m_incremental_v1"
SPEC_RELATIVE_PATH = Path("specs/source_collection/v13_db3_incremental_collection.json")
CANDIDATE_DATASET_ID = "saxo_sim_fx_research_candidates_60m_v1"
CANDIDATE_SPEC_RELATIVE_PATH = Path(
    "specs/source_collection/fx_research_candidates_v1.json"
)
CANDIDATE_INSTRUMENT_KEYS = ("audusd", "usdcad", "usdchf")
CANDIDATE_RESEARCH_WARNING_POLICY_ID = (
    "fx_research_candidate_user_approved_warnings_v1"
)
MAX_QUARANTINED_FX_EXTREMA_ROWS = 10
MAX_QUARANTINED_FX_EXTREMA_RATE = Decimal("0.0001")
FX_EXTREMA_QUARANTINE_POLICY_ID = "db3_bounded_fx_extrema_quarantine_v1"
REVISION_WARNING_POLICY_ID = "data_version_revision_warning_v2"
REVISION_WARNING_CODE = "DATA_VERSION_REVISION_REVIEW_PENDING"
S6V5A_PRIORITY_INSTRUMENT_KEYS = ("spy", "iwm", "efa", "eem", "vnq", "eurusd")


@dataclass(frozen=True)
class InstrumentState:
    instrument_id: int
    latest_complete_time_utc: datetime
    data_version: int | None
    data_status: str


@dataclass(frozen=True)
class AcquiredInstrument:
    registry: CanonicalInstrument
    state: InstrumentState
    bars: tuple[NormalizedBar, ...]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def select_instruments(instrument_keys: Iterable[str] | None = None) -> tuple[CanonicalInstrument, ...]:
    """Select a stable canonical subset without accepting symbol substitution."""

    canonical = load_canonical_instruments()
    if instrument_keys is None:
        return canonical
    requested = tuple(str(key).strip().lower() for key in instrument_keys)
    if not requested or any(not key for key in requested) or len(set(requested)) != len(requested):
        raise ValueError("instrument keys must be a non-empty unique canonical subset")
    candidates = load_research_candidate_instruments()
    by_key = {item.key: item for item in (*canonical, *candidates)}
    unknown = sorted(set(requested) - set(by_key))
    if unknown:
        raise ValueError("instrument keys contain an unreviewed key")
    selected_candidate = set(requested) & set(CANDIDATE_INSTRUMENT_KEYS)
    if selected_candidate and selected_candidate != set(requested):
        raise ValueError("canonical and research-candidate instruments must run separately")
    return tuple(by_key[key] for key in requested)


def _dataset_contract(
    registry: Iterable[CanonicalInstrument],
) -> tuple[str, Path, str, str]:
    keys = {item.key for item in registry}
    if keys and keys <= set(CANDIDATE_INSTRUMENT_KEYS):
        return (
            CANDIDATE_DATASET_ID,
            CANDIDATE_SPEC_RELATIVE_PATH,
            "Saxo SIM FX research candidates 60m chart",
            "SIM_RESEARCH_CANDIDATE",
        )
    if keys & set(CANDIDATE_INSTRUMENT_KEYS):
        raise ValueError("candidate dataset cannot be mixed with the canonical dataset")
    return (
        DATASET_ID,
        SPEC_RELATIVE_PATH,
        "Saxo SIM canonical 13 incremental 60m chart",
        "operational_market_data_not_frozen_research_input",
    )


def _ensure_dataset(
    cursor: Any,
    *,
    dataset_id: str = DATASET_ID,
    spec_relative_path: Path = SPEC_RELATIVE_PATH,
    dataset_name: str = "Saxo SIM canonical 13 incremental 60m chart",
    research_eligibility: str = "operational_market_data_not_frozen_research_input",
    instrument_count: int = 13,
) -> None:
    manifest_path = project_root() / spec_relative_path
    metadata = {
        "instrument_count": instrument_count,
        "horizon_minutes": 60,
        "write_endpoints": 0,
    }
    if dataset_id == CANDIDATE_DATASET_ID:
        metadata.update(
            {
                "instrument_count": len(CANDIDATE_INSTRUMENT_KEYS),
                "research_warning_policy_id": CANDIDATE_RESEARCH_WARNING_POLICY_ID,
                "consumer_availability_status": "AVAILABLE_WITH_WARNINGS",
                "value_repair": False,
                "interpolation": False,
            }
        )
    cursor.execute(
        """
        INSERT INTO catalog.source_dataset (
            source_dataset_id, dataset_name, provider, environment, dataset_kind,
            price_basis, canonical_horizon_minutes, expected_update_interval_seconds,
            freshness_grace_seconds, authoritative_layer, research_eligibility,
            active, source_manifest_relative_path, source_manifest_sha256, metadata_json
        ) VALUES (
            %s,%s,'Saxo OpenAPI','SIM',
            'raw_market','asset_specific',60,3600,7200,'raw',
            %s,TRUE,%s,%s,%s
        )
        ON CONFLICT (source_dataset_id) DO UPDATE SET
            dataset_name=EXCLUDED.dataset_name,
            active=TRUE,
            source_manifest_relative_path=EXCLUDED.source_manifest_relative_path,
            source_manifest_sha256=EXCLUDED.source_manifest_sha256,
            metadata_json=EXCLUDED.metadata_json
        """,
        (
            dataset_id,
            dataset_name,
            research_eligibility,
            str(spec_relative_path),
            _sha256(manifest_path),
            Jsonb(metadata),
        ),
    )


def initialize_watermarks() -> dict[str, Any]:
    registry = load_canonical_instruments()
    initialized = 0
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_watermark_init") as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                _ensure_dataset(cursor)
                for instrument in registry:
                    cursor.execute(
                        """
                        INSERT INTO ops.watermark (
                            instrument_id, horizon_minutes, price_basis,
                            latest_seen_time_utc, latest_complete_time_utc,
                            data_version, last_ingestion_run_id, data_status, updated_at_utc
                        )
                        SELECT
                            i.instrument_id, 60, %s,
                            MAX(b.time_utc),
                            MAX(b.time_utc) FILTER (WHERE b.is_complete),
                            MAX(b.data_version),
                            MAX(b.latest_ingestion_run_id),
                            'ACTIVE', clock_timestamp()
                        FROM catalog.instrument i
                        JOIN curated.market_bar b ON b.instrument_id=i.instrument_id
                        WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                          AND i.uic=%s AND i.asset_type=%s
                          AND b.horizon_minutes=60 AND b.price_basis=%s
                        GROUP BY i.instrument_id
                        ON CONFLICT (instrument_id, horizon_minutes, price_basis) DO NOTHING
                        """,
                        (
                            instrument.price_basis,
                            instrument.uic,
                            instrument.asset_type,
                            instrument.price_basis,
                        ),
                    )
                    initialized += cursor.rowcount
                cursor.execute(
                    """
                    SELECT COUNT(*)
                    FROM ops.watermark w
                    JOIN catalog.instrument i USING (instrument_id)
                    WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                      AND (i.uic, i.asset_type) IN (
                          SELECT uic, asset_type FROM catalog.instrument
                          WHERE provider='Saxo OpenAPI' AND environment='SIM'
                      )
                      AND w.horizon_minutes=60
                    """
                )
                total = int(cursor.fetchone()[0])
    return {
        "initialized": initialized,
        "status": "PASS" if total >= 13 else "FAIL",
        "watermarks": total,
    }


def _load_states() -> dict[tuple[int, str], InstrumentState]:
    states: dict[tuple[int, str], InstrumentState] = {}
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_incremental_state") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT i.uic, i.asset_type, i.instrument_id,
                       w.latest_complete_time_utc, w.data_version, w.data_status
                FROM catalog.instrument i
                JOIN ops.watermark w ON w.instrument_id=i.instrument_id
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                  AND w.horizon_minutes=60 AND w.data_status='ACTIVE'
                """
            )
            for uic, asset_type, instrument_id, latest_complete, data_version, data_status in cursor.fetchall():
                states[(int(uic), str(asset_type))] = InstrumentState(
                    int(instrument_id), latest_complete,
                    None if data_version is None else int(data_version), str(data_status)
                )
    return states


def _load_full_refetch_state(instrument: CanonicalInstrument) -> tuple[InstrumentState, datetime]:
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_full_refetch_state") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT i.instrument_id, w.latest_complete_time_utc, w.data_version,
                       w.data_status, MIN(b.time_utc)
                FROM catalog.instrument i
                JOIN ops.watermark w ON w.instrument_id=i.instrument_id
                JOIN curated.market_bar b ON b.instrument_id=i.instrument_id
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                  AND i.uic=%s AND i.asset_type=%s
                  AND w.horizon_minutes=60 AND b.horizon_minutes=60
                  AND b.price_basis=%s
                GROUP BY i.instrument_id, w.latest_complete_time_utc,
                         w.data_version, w.data_status
                """,
                (instrument.uic, instrument.asset_type, instrument.price_basis),
            )
            row = cursor.fetchone()
    if row is None:
        raise BarQualityError("BLOCKED_FULL_REFETCH_STATE_MISSING")
    state = InstrumentState(
        int(row[0]), row[1], None if row[2] is None else int(row[2]), str(row[3])
    )
    if state.data_status != "STALE_DATA_VERSION":
        raise BarQualityError("BLOCKED_FULL_REFETCH_NOT_REQUIRED")
    return state, row[4]


def _overlap_start(state: InstrumentState, instrument: CanonicalInstrument) -> datetime:
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_overlap_start") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT time_utc
                FROM curated.market_bar
                WHERE instrument_id=%s AND horizon_minutes=60 AND price_basis=%s
                  AND is_complete AND quality_status='PASS'
                ORDER BY time_utc DESC
                OFFSET %s LIMIT 1
                """,
                (state.instrument_id, instrument.price_basis, instrument.overlap_bars - 1),
            )
            row = cursor.fetchone()
    if row is None:
        raise BarQualityError("BLOCKED_WATERMARK_WITHOUT_OVERLAP")
    return row[0]


def _finalize_etf_terminal_bar(
    instrument: CanonicalInstrument,
    state: InstrumentState,
    bars: Iterable[NormalizedBar],
) -> tuple[NormalizedBar, ...]:
    """Use the verified local calendar to finalize a closed ETF terminal bar."""

    selected = tuple(bars)
    if instrument.asset_type != "Etf" or not selected:
        return selected
    terminal_time = max(bar.time_utc for bar in selected)
    with connect(
        "saxo_ingest", MARKET_DB,
        application_name="saxo_db_terminal_bar_completion",
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT si.open_time_utc,si.close_time_utc
                FROM catalog.instrument i
                JOIN catalog.session_interval si
                  ON si.session_calendar_id=i.session_calendar_id
                JOIN catalog.session_calendar c
                  ON c.session_calendar_id=si.session_calendar_id
                WHERE i.instrument_id=%s
                  AND si.session_status <> 'HOLIDAY'
                  AND si.open_time_utc <= %s AND %s < si.close_time_utc
                  AND c.metadata_json->>'verification_status'='VERIFIED'
                ORDER BY si.interval_sequence
                LIMIT 1
                """,
                (state.instrument_id, terminal_time, terminal_time),
            )
            row = cursor.fetchone()
    if row is None:
        return selected
    return tuple(
        mark_terminal_session_bar_complete(
            selected,
            session_open_utc=row[0],
            session_close_utc=row[1],
        )
    )


def _create_run(
    run_id: str,
    registry: tuple[CanonicalInstrument, ...],
    *,
    trigger: str = "manual_db3",
) -> int:
    manifest_path = f"data/acquisition/runs/{run_id}/run_manifest.json"
    dataset_id, spec_path, dataset_name, eligibility = _dataset_contract(registry)
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_incremental_start") as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                _ensure_dataset(
                    cursor,
                    dataset_id=dataset_id,
                    spec_relative_path=spec_path,
                    dataset_name=dataset_name,
                    research_eligibility=eligibility,
                    instrument_count=len(registry),
                )
                cursor.execute(
                    """
                    SELECT instrument_id, lower(market_key), uic, asset_type, price_basis
                    FROM catalog.instrument i
                    LEFT JOIN LATERAL (
                        SELECT w.price_basis
                        FROM ops.watermark w
                        WHERE w.instrument_id=i.instrument_id AND w.horizon_minutes=60
                        ORDER BY w.price_basis
                        LIMIT 1
                    ) selected_basis ON TRUE
                    WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                      AND i.active_to_utc IS NULL
                    """
                )
                database_scope = {
                    (int(uic), str(asset_type)): {
                        "instrument_id": int(instrument_id),
                        "instrument_key": str(key),
                        "uic": int(uic),
                        "asset_type": str(asset_type),
                        "price_basis": None if price_basis is None else str(price_basis),
                    }
                    for instrument_id, key, uic, asset_type, price_basis in cursor.fetchall()
                }
                requested = []
                for item in registry:
                    scope = database_scope.get((item.uic, item.asset_type))
                    if scope is None or scope["instrument_key"] != item.key:
                        raise RuntimeError("BLOCKED_RUN_SCOPE_INSTRUMENT_MISMATCH")
                    if scope["price_basis"] is not None and scope["price_basis"] != item.price_basis:
                        raise RuntimeError("BLOCKED_RUN_SCOPE_PRICE_BASIS_MISMATCH")
                    requested.append(
                        {**scope, "price_basis": item.price_basis, "horizon_minutes": 60}
                    )

                cursor.execute(
                    """
                    INSERT INTO ops.ingestion_run (
                        trigger, environment, status, requested_series,
                        run_manifest_relative_path, last_success_step, metadata_json
                    ) VALUES (%s,'SIM','RUNNING',%s,%s,'run_registered',%s)
                    RETURNING ingestion_run_id
                    """,
                    (
                        trigger,
                        Jsonb(requested),
                        manifest_path,
                        Jsonb(
                            {
                                "acquisition_run_id": run_id,
                                "selected_instrument_keys": [row["instrument_key"] for row in requested],
                                "selected_instrument_ids": [row["instrument_id"] for row in requested],
                                "scope_contract": "ops.ingestion_run_instrument_scope_v1",
                            }
                        ),
                    ),
                )
                database_run_id = int(cursor.fetchone()[0])
                cursor.executemany(
                    """
                    INSERT INTO ops.ingestion_run_instrument_scope (
                        ingestion_run_id,instrument_id,instrument_key,uic,asset_type,
                        horizon_minutes,price_basis
                    ) VALUES (%s,%s,%s,%s,%s,60,%s)
                    """,
                    [
                        (
                            database_run_id,
                            row["instrument_id"],
                            row["instrument_key"],
                            row["uic"],
                            row["asset_type"],
                            row["price_basis"],
                        )
                        for row in requested
                    ],
                )
                return database_run_id


def _register_sources(
    cursor: Any,
    run_id: int,
    artifacts: list[ArtifactRecord],
    *,
    dataset_id: str = DATASET_ID,
) -> dict[str, int]:
    result: dict[str, int] = {}
    for artifact in artifacts:
        cursor.execute(
            """
            INSERT INTO ops.source_file (
                ingestion_run_id, relative_path, sha256, size_bytes,
                row_count, source_dataset_id
            ) VALUES (%s,%s,%s,%s,%s,%s)
            ON CONFLICT (relative_path,sha256) DO UPDATE SET
                size_bytes=EXCLUDED.size_bytes,
                row_count=EXCLUDED.row_count
            RETURNING source_file_id
            """,
            (
                run_id,
                artifact.relative_path,
                artifact.sha256,
                artifact.size_bytes,
                artifact.row_count,
                dataset_id,
            ),
        )
        result[artifact.relative_path] = int(cursor.fetchone()[0])
    return result


def _revision_bar_content(bar: NormalizedBar) -> tuple[Any, ...]:
    """Return the value identity used for a warning-only revision sample."""

    return (
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.open_bid,
        bar.high_bid,
        bar.low_bid,
        bar.close_bid,
        bar.open_ask,
        bar.high_ask,
        bar.low_ask,
        bar.close_ask,
        bar.volume,
        bar.market_trading_state,
        bar.is_complete,
    )


def compare_revision_sample(
    provider_bars: Iterable[NormalizedBar],
    stored_bars: Mapping[datetime, tuple[tuple[Any, ...], int | None]],
) -> dict[str, Any]:
    """Compare one retained incremental sample without deciding or applying a repair."""

    ordered = tuple(sorted(provider_bars, key=lambda item: item.time_utc))
    if not ordered:
        raise BarQualityError("REVISION_EMPTY_PROVIDER_SAMPLE")
    if len({item.time_utc for item in ordered}) != len(ordered):
        raise BarQualityError("REVISION_NON_UNIQUE_PROVIDER_SAMPLE")
    provider_by_time = {item.time_utc: item for item in ordered}
    matched_rows = 0
    content_difference_rows = 0
    version_only_rows = 0
    new_rows = 0
    for bar in ordered:
        stored = stored_bars.get(bar.time_utc)
        if stored is None:
            new_rows += 1
            continue
        matched_rows += 1
        stored_content, stored_version = stored
        if _revision_bar_content(bar) != stored_content:
            content_difference_rows += 1
        elif stored_version != bar.data_version:
            version_only_rows += 1
    lower = ordered[0].time_utc
    upper = ordered[-1].time_utc
    removed_rows = sum(
        lower <= time_utc <= upper and time_utc not in provider_by_time
        for time_utc in stored_bars
    )
    completed = [bar.time_utc for bar in ordered if bar.is_complete]
    return {
        "comparison_from_utc": lower,
        "comparison_to_utc": upper,
        "provider_rows": len(ordered),
        "matched_rows": matched_rows,
        "content_difference_rows": content_difference_rows,
        "version_only_rows": version_only_rows,
        "new_rows": new_rows,
        "removed_rows": removed_rows,
        "latest_provider_complete_time_utc": max(completed) if completed else None,
    }


def _load_revision_sample(
    state: InstrumentState,
    price_basis: str,
    provider_bars: Iterable[NormalizedBar],
) -> dict[str, Any]:
    selected = tuple(provider_bars)
    lower = min(bar.time_utc for bar in selected)
    upper = max(bar.time_utc for bar in selected)
    with connect(
        "saxo_ingest", MARKET_DB, application_name="saxo_db_revision_warning_compare"
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT time_utc,open,high,low,close,
                       open_bid,high_bid,low_bid,close_bid,
                       open_ask,high_ask,low_ask,close_ask,
                       volume,market_trading_state,is_complete,data_version
                FROM curated.market_bar
                WHERE instrument_id=%s AND horizon_minutes=60 AND price_basis=%s
                  AND time_utc BETWEEN %s AND %s
                ORDER BY time_utc
                """,
                (state.instrument_id, price_basis, lower, upper),
            )
            stored = {
                row[0]: (
                    tuple(row[1:-1]),
                    None if row[-1] is None else int(row[-1]),
                )
                for row in cursor.fetchall()
            }
    return compare_revision_sample(selected, stored)


def _stage(cursor: Any, run_id: int, acquired: list[AcquiredInstrument], sources: dict[str, int]) -> None:
    rows = []
    for item in acquired:
        for bar in item.bars:
            rows.append(
                (
                    run_id,
                    sources[bar.artifact_relative_path],
                    item.state.instrument_id,
                    60,
                    bar.time_utc,
                    bar.open,
                    bar.high,
                    bar.low,
                    bar.close,
                    bar.open_bid,
                    bar.high_bid,
                    bar.low_bid,
                    bar.close_bid,
                    bar.open_ask,
                    bar.high_ask,
                    bar.low_ask,
                    bar.close_ask,
                    bar.volume,
                    bar.market_trading_state,
                    bar.price_basis,
                    bar.is_complete,
                    bar.data_version,
                    bar.delayed_by_minutes,
                    bar.retrieved_at_utc,
                    bar.payload_sha256,
                )
            )
    cursor.executemany(
        """
        INSERT INTO staging.market_bar (
            ingestion_run_id, source_file_id, instrument_id, horizon_minutes,
            time_utc, open, high, low, close,
            open_bid, high_bid, low_bid, close_bid,
            open_ask, high_ask, low_ask, close_ask,
            volume, market_trading_state, price_basis, is_complete,
            data_version, delayed_by_minutes, retrieved_at_utc, payload_sha256
        ) VALUES (
            %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
            %s,%s,%s,%s,%s
        )
        """,
        rows,
    )


def _validate_full_refetch_quarantine(
    accepted_times: Iterable[datetime],
    rejected_rows: Iterable[RejectedBar],
    *,
    approved_exception: Mapping[str, Any] | None = None,
) -> tuple[RejectedBar, ...]:
    """Validate and deduplicate the narrowly scoped FX-extrema quarantine."""
    accepted = set(accepted_times)
    rejected_by_time: dict[datetime, RejectedBar] = {}
    for rejected in rejected_rows:
        if rejected.time_utc in accepted:
            raise BarQualityError("FX_EXTREMA_QUARANTINE_ACCEPTED_REJECTED_CONFLICT")
        previous = rejected_by_time.get(rejected.time_utc)
        if previous is not None:
            previous_values = (previous.error_code, previous.violations, previous.data_version)
            current_values = (rejected.error_code, rejected.violations, rejected.data_version)
            if previous_values != current_values:
                raise BarQualityError("FX_EXTREMA_QUARANTINE_DUPLICATE_CONFLICT")
        rejected_by_time[rejected.time_utc] = rejected

    unique_rejected = tuple(rejected_by_time[key] for key in sorted(rejected_by_time))
    if not unique_rejected:
        if approved_exception is not None:
            raise BarQualityError("FX_EXTREMA_APPROVED_EXCEPTION_MISMATCH")
        return ()

    observed_times = accepted | set(rejected_by_time)
    if any(rejected.time_utc == max(observed_times) for rejected in unique_rejected):
        raise BarQualityError("FX_EXTREMA_QUARANTINE_LATEST_SAMPLE_INELIGIBLE")
    rejected_rate = Decimal(len(unique_rejected)) / Decimal(len(observed_times))
    if approved_exception is not None:
        evidence = [
            {
                "time_utc": rejected.time_utc.isoformat().replace("+00:00", "Z"),
                "violations": [
                    {
                        "field": violation.field,
                        "bid": str(violation.bid),
                        "ask": str(violation.ask),
                    }
                    for violation in rejected.violations
                ],
            }
            for rejected in unique_rejected
        ]
        content_sha256 = hashlib.sha256(
            json.dumps(
                evidence, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        fields = {
            violation.field
            for rejected in unique_rejected
            for violation in rejected.violations
        }
        allowed_fields = {str(value) for value in approved_exception.get("allowed_fields", ())}
        first_time = unique_rejected[0].time_utc.isoformat().replace("+00:00", "Z")
        last_time = unique_rejected[-1].time_utc.isoformat().replace("+00:00", "Z")
        matches = (
            int(approved_exception.get("unique_rows", -1)) == len(unique_rejected)
            and fields
            and fields <= allowed_fields
            and approved_exception.get("affected_from_utc") == first_time
            and approved_exception.get("affected_to_utc") == last_time
            and approved_exception.get("content_sha256") == content_sha256
            and approved_exception.get("values_modified") is False
            and approved_exception.get("exact_baseline_required_for_exception") is True
        )
        if not matches:
            raise BarQualityError("FX_EXTREMA_APPROVED_EXCEPTION_MISMATCH")
    else:
        if len(unique_rejected) > MAX_QUARANTINED_FX_EXTREMA_ROWS:
            raise BarQualityError("FX_EXTREMA_QUARANTINE_ROW_LIMIT_EXCEEDED")
        if rejected_rate > MAX_QUARANTINED_FX_EXTREMA_RATE:
            raise BarQualityError("FX_EXTREMA_QUARANTINE_RATE_LIMIT_EXCEEDED")
    return unique_rejected


def _quarantined_row_evidence(
    rejected: RejectedBar,
    *,
    policy_id: str = FX_EXTREMA_QUARANTINE_POLICY_ID,
    approved_exception: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "policy_id": policy_id,
        "user_approved_research_exception": approved_exception is not None,
        "time_utc": rejected.time_utc.isoformat().replace("+00:00", "Z"),
        "error_code": rejected.error_code,
        "violations": [
            {
                "field": violation.field,
                "bid": str(violation.bid),
                "ask": str(violation.ask),
            }
            for violation in rejected.violations
        ],
        "data_version": rejected.data_version,
        "artifact_relative_path": rejected.artifact_relative_path,
        "payload_sha256": rejected.payload_sha256,
    }


def _commit_acquired(
    run_id: int,
    acquired: list[AcquiredInstrument],
    chart_artifacts: list[ArtifactRecord],
    *,
    full_replace_instrument_id: int | None = None,
    quarantined_rows: tuple[RejectedBar, ...] = (),
    dataset_id: str = DATASET_ID,
    bootstrap_watermark: bool = False,
    approved_quarantine_exception: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    candidate_bootstrap_quarantine = (
        bootstrap_watermark
        and dataset_id == CANDIDATE_DATASET_ID
        and len(acquired) == 1
        and acquired[0].registry.asset_type == "FxSpot"
    )
    quarantine_instrument_id = (
        full_replace_instrument_id
        if full_replace_instrument_id is not None
        else acquired[0].state.instrument_id
        if candidate_bootstrap_quarantine
        else None
    )
    if quarantined_rows and (
        quarantine_instrument_id is None
        or len(acquired) != 1
        or acquired[0].registry.asset_type != "FxSpot"
        or acquired[0].state.instrument_id != quarantine_instrument_id
    ):
        raise BarQualityError("FX_EXTREMA_QUARANTINE_SCOPE_VIOLATION")
    quarantined_rows = _validate_full_refetch_quarantine(
        (bar.time_utc for item in acquired for bar in item.bars),
        quarantined_rows,
        approved_exception=approved_quarantine_exception,
    )
    quarantine_policy_id = str(
        (approved_quarantine_exception or {}).get(
            "policy_id", FX_EXTREMA_QUARANTINE_POLICY_ID
        )
    )
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_incremental_commit") as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext('saxo_db_db3_incremental'))")
                cursor.execute("DELETE FROM staging.market_bar WHERE ingestion_run_id=%s", (run_id,))
                sources = _register_sources(
                    cursor, run_id, chart_artifacts, dataset_id=dataset_id
                )
                _stage(cursor, run_id, acquired, sources)

                for rejected in quarantined_rows:
                    if rejected.artifact_relative_path not in sources:
                        raise RuntimeError("FAILED_QUARANTINE_SOURCE_REGISTRATION")
                    cursor.execute(
                        """
                        INSERT INTO quality.event (
                            ingestion_run_id, instrument_id, horizon_minutes, time_utc,
                            rule_id, severity, observed_value, action, status,
                            resolved_at_utc, resolved_by, resolution_note
                        ) VALUES (
                            %s,%s,60,%s,'db3_fx_crossed_extrema_quarantine','WARN',%s,
                            'raw source retained; row excluded without swapping, interpolation, clamping, or correction',
                            'RESOLVED',clock_timestamp(),'db3_incremental_update',
                            %s
                        )
                        """,
                        (
                            run_id,
                            quarantine_instrument_id,
                            rejected.time_utc,
                            Jsonb(
                                _quarantined_row_evidence(
                                    rejected,
                                    policy_id=quarantine_policy_id,
                                    approved_exception=approved_quarantine_exception,
                                )
                            ),
                            (
                                'user-approved SIM research exception; exact AUDUSD anomaly baseline matched'
                                if approved_quarantine_exception is not None
                                else 'bounded reviewed FX extrema quarantine accepted by frozen DB3 policy'
                            ),
                        ),
                    )

                cursor.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE c.instrument_id IS NULL),
                        COUNT(*) FILTER (
                            WHERE c.instrument_id IS NOT NULL AND ROW(
                                c.open,c.high,c.low,c.close,c.open_bid,c.high_bid,c.low_bid,c.close_bid,
                                c.open_ask,c.high_ask,c.low_ask,c.close_ask,c.volume,
                                c.market_trading_state,c.is_complete,c.data_version
                            ) IS DISTINCT FROM ROW(
                                s.open,s.high,s.low,s.close,s.open_bid,s.high_bid,s.low_bid,s.close_bid,
                                s.open_ask,s.high_ask,s.low_ask,s.close_ask,s.volume,
                                s.market_trading_state,s.is_complete,s.data_version
                            )
                        )
                    FROM staging.market_bar s
                    LEFT JOIN curated.market_bar c
                      ON c.instrument_id=s.instrument_id
                     AND c.horizon_minutes=s.horizon_minutes
                     AND c.time_utc=s.time_utc
                     AND c.price_basis=s.price_basis
                    WHERE s.ingestion_run_id=%s
                    """,
                    (run_id,),
                )
                inserted_rows, updated_rows = (int(value) for value in cursor.fetchone())

                cursor.execute(
                    """
                    INSERT INTO quality.event (
                        ingestion_run_id, instrument_id, horizon_minutes, time_utc,
                        rule_id, severity, observed_value, action, status,
                        resolved_at_utc, resolved_by, resolution_note
                    )
                    SELECT
                        %s, s.instrument_id, 60, s.time_utc,
                        'db3_historical_revision', 'INFO',
                        jsonb_build_object(
                            'old', jsonb_build_object('open',c.open,'high',c.high,'low',c.low,'close',c.close,'data_version',c.data_version),
                            'new', jsonb_build_object('open',s.open,'high',s.high,'low',s.low,'close',s.close,'data_version',s.data_version)
                        ),
                        'raw revision retained and curated latest updated',
                        'RESOLVED', clock_timestamp(), 'db3_incremental_update',
                        'deterministic revision audit record'
                    FROM staging.market_bar s
                    JOIN curated.market_bar c
                      ON c.instrument_id=s.instrument_id
                     AND c.horizon_minutes=s.horizon_minutes
                     AND c.time_utc=s.time_utc
                     AND c.price_basis=s.price_basis
                    WHERE s.ingestion_run_id=%s
                      AND ROW(
                          c.open,c.high,c.low,c.close,c.open_bid,c.high_bid,c.low_bid,c.close_bid,
                          c.open_ask,c.high_ask,c.low_ask,c.close_ask,c.volume,
                          c.market_trading_state,c.is_complete,c.data_version
                      ) IS DISTINCT FROM ROW(
                          s.open,s.high,s.low,s.close,s.open_bid,s.high_bid,s.low_bid,s.close_bid,
                          s.open_ask,s.high_ask,s.low_ask,s.close_ask,s.volume,
                          s.market_trading_state,s.is_complete,s.data_version
                      )
                    """,
                    (run_id, run_id),
                )
                revision_rows = cursor.rowcount
                removed_rows = 0
                if full_replace_instrument_id is not None:
                    cursor.execute(
                        """
                        SELECT COUNT(*)
                        FROM curated.market_bar c
                        LEFT JOIN staging.market_bar s
                          ON s.ingestion_run_id=%s
                         AND s.instrument_id=c.instrument_id
                         AND s.horizon_minutes=c.horizon_minutes
                         AND s.time_utc=c.time_utc
                         AND s.price_basis=c.price_basis
                        WHERE c.instrument_id=%s AND c.horizon_minutes=60
                          AND s.instrument_id IS NULL
                        """,
                        (run_id, full_replace_instrument_id),
                    )
                    removed_rows = int(cursor.fetchone()[0])
                    if removed_rows:
                        cursor.execute(
                            """
                            INSERT INTO quality.event (
                                ingestion_run_id,instrument_id,horizon_minutes,rule_id,severity,
                                observed_value,action,status,resolved_at_utc,resolved_by,resolution_note
                            ) VALUES (
                                %s,%s,60,'db3_full_refetch_removed_observations','INFO',%s,
                                'old curated observations absent from authoritative full response; raw audit retained',
                                'RESOLVED',clock_timestamp(),'db3_incremental_update',
                                'controlled DataVersion full refetch'
                            )
                            """,
                            (run_id, full_replace_instrument_id, Jsonb({"removed_rows": removed_rows})),
                        )
                    revision_rows += removed_rows

                cursor.execute(
                    """
                    INSERT INTO raw.market_bar_revision
                    SELECT * FROM staging.market_bar WHERE ingestion_run_id=%s
                    """,
                    (run_id,),
                )
                raw_rows = cursor.rowcount

                if full_replace_instrument_id is not None:
                    cursor.execute(
                        "CALL curated.prepare_full_refetch(%s,%s)",
                        (run_id, full_replace_instrument_id),
                    )

                cursor.execute(
                    """
                    INSERT INTO curated.market_bar (
                        instrument_id,horizon_minutes,time_utc,open,high,low,close,
                        open_bid,high_bid,low_bid,close_bid,
                        open_ask,high_ask,low_ask,close_ask,
                        volume,market_trading_state,price_basis,is_complete,data_version,
                        latest_ingestion_run_id,retrieved_at_utc,quality_status
                    )
                    SELECT
                        instrument_id,horizon_minutes,time_utc,open,high,low,close,
                        open_bid,high_bid,low_bid,close_bid,
                        open_ask,high_ask,low_ask,close_ask,
                        volume,market_trading_state,price_basis,is_complete,data_version,
                        ingestion_run_id,retrieved_at_utc,
                        CASE WHEN is_complete THEN 'PASS' ELSE 'NOT_EVALUATED' END
                    FROM staging.market_bar WHERE ingestion_run_id=%s
                    ON CONFLICT (instrument_id,horizon_minutes,time_utc,price_basis) DO UPDATE SET
                        open=EXCLUDED.open,high=EXCLUDED.high,low=EXCLUDED.low,close=EXCLUDED.close,
                        open_bid=EXCLUDED.open_bid,high_bid=EXCLUDED.high_bid,
                        low_bid=EXCLUDED.low_bid,close_bid=EXCLUDED.close_bid,
                        open_ask=EXCLUDED.open_ask,high_ask=EXCLUDED.high_ask,
                        low_ask=EXCLUDED.low_ask,close_ask=EXCLUDED.close_ask,
                        volume=EXCLUDED.volume,market_trading_state=EXCLUDED.market_trading_state,
                        is_complete=EXCLUDED.is_complete,data_version=EXCLUDED.data_version,
                        latest_ingestion_run_id=EXCLUDED.latest_ingestion_run_id,
                        retrieved_at_utc=EXCLUDED.retrieved_at_utc,
                        quality_status=EXCLUDED.quality_status
                    """,
                    (run_id,),
                )

                for item in acquired:
                    latest_seen = max(bar.time_utc for bar in item.bars)
                    latest_complete = max(bar.time_utc for bar in item.bars if bar.is_complete)
                    data_version = next((bar.data_version for bar in item.bars if bar.data_version is not None), None)
                    if bootstrap_watermark:
                        cursor.execute(
                            """
                            INSERT INTO ops.watermark (
                                instrument_id,horizon_minutes,price_basis,
                                latest_seen_time_utc,latest_complete_time_utc,data_version,
                                last_ingestion_run_id,data_status,updated_at_utc
                            ) VALUES (%s,60,%s,%s,%s,%s,%s,'ACTIVE',clock_timestamp())
                            ON CONFLICT (instrument_id,horizon_minutes,price_basis) DO NOTHING
                            """,
                            (
                                item.state.instrument_id,
                                item.registry.price_basis,
                                latest_seen,
                                latest_complete,
                                data_version,
                                run_id,
                            ),
                        )
                    else:
                        cursor.execute(
                            """
                            UPDATE ops.watermark SET
                                latest_seen_time_utc=%s,
                                latest_complete_time_utc=%s,
                                data_version=%s,
                                last_ingestion_run_id=%s,
                                data_status='ACTIVE',
                                updated_at_utc=clock_timestamp()
                            WHERE instrument_id=%s AND horizon_minutes=60 AND price_basis=%s
                            """,
                            (
                                latest_seen,
                                latest_complete,
                                data_version,
                                run_id,
                                item.state.instrument_id,
                                item.registry.price_basis,
                            ),
                        )
                    if cursor.rowcount != 1:
                        raise RuntimeError("FAILED_WATERMARK_UPDATE")

                # The transaction only changes the acquired instruments.  A
                # singleton scheduler lane must not rewrite derived rows for
                # the other managed series.
                acquired_instrument_ids = tuple(
                    item.state.instrument_id for item in acquired
                )
                derived_counts = rebuild(
                    cursor,
                    instrument_ids=acquired_instrument_ids,
                )
                c2_imputation = refresh_c2_imputation_overlay(
                    cursor,
                    instrument_ids=acquired_instrument_ids,
                )
                cursor.execute("DELETE FROM staging.market_bar WHERE ingestion_run_id=%s", (run_id,))
                cursor.execute(
                    """
                    UPDATE ops.ingestion_run SET
                        finished_at_utc=clock_timestamp(), status='PASS',
                        successful_series=%s, inserted_rows=%s, updated_rows=%s,
                        revision_rows=%s, rejected_rows=%s,
                        last_success_step='watermark_and_derived_committed',
                        metadata_json=metadata_json || %s
                    WHERE ingestion_run_id=%s AND status='RUNNING'
                    """,
                    (
                        len(acquired),
                        inserted_rows,
                        updated_rows,
                        revision_rows,
                        len(quarantined_rows),
                        Jsonb(
                            {
                                "raw_rows": raw_rows,
                                "derived": derived_counts,
                                "c2_imputation": c2_imputation,
                                "quarantine_policy_id": (
                                    quarantine_policy_id if quarantined_rows else None
                                ),
                            }
                        ),
                        run_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("FAILED_RUN_STATUS_UPDATE")
    return {
        "derived": derived_counts,
        "c2_imputation": c2_imputation,
        "inserted_rows": inserted_rows,
        "raw_rows": raw_rows,
        "rejected_rows": len(quarantined_rows),
        "quarantined_fx_extrema": [
            _quarantined_row_evidence(
                rejected,
                policy_id=quarantine_policy_id,
                approved_exception=approved_quarantine_exception,
            )
            for rejected in quarantined_rows
        ],
        "removed_rows": removed_rows,
        "revision_rows": revision_rows,
        "updated_rows": updated_rows,
    }


def _error_code(exc: Exception) -> str:
    if isinstance(exc, SaxoAuthError):
        return exc.code
    if isinstance(exc, SaxoAPIError):
        return exc.code
    if isinstance(exc, InstrumentDriftError):
        return str(exc).split(":", 1)[0]
    if isinstance(exc, BarQualityError):
        return str(exc)
    if isinstance(exc, ValueError) and str(exc) == "SAXO_ACCESS_TOKEN is required":
        return "BLOCKED_LIVE_SIM_TOKEN"
    return f"FAILED_{type(exc).__name__.upper()}"


def _records_quality_event(code: str) -> bool:
    """Keep interface/availability incidents out of content-quality blockers."""

    if (
        code.startswith("AUTH_")
        or code.startswith("FAILED_HTTP_")
        or code.startswith("BLOCKED_TOKEN")
        or code.startswith("BLOCKED_PERMISSION")
        or code.startswith("BLOCKED_REVISION_")
    ):
        return False
    return code not in {
        "BLOCKED_FULL_REFETCH_REQUIRED",
        "BLOCKED_BOUNDED_REVISION_REQUIRED",
        "BLOCKED_RATE_LIMIT",
        "FAILED_NETWORK",
        "FAILED_SERVICE_UNAVAILABLE",
        "FAILED_INVALID_JSON",
        "FAILED_JSON_NOT_OBJECT",
        "INSUFFICIENT_INCREMENTAL_CHART_DATA",
    }


def _failed_instrument_context(status: str, instrument_key: str | None) -> dict[str, str]:
    if status == "PASS" or instrument_key is None:
        return {}
    return {"failed_instrument_key": instrument_key}


def _record_failure(
    run_id: int,
    code: str,
    chart_artifacts: list[ArtifactRecord],
    failed_instrument_keys: Iterable[str] | None,
    *,
    revision_detection: Mapping[str, Any] | None = None,
    dataset_id: str = DATASET_ID,
    spec_relative_path: Path = SPEC_RELATIVE_PATH,
    dataset_name: str = "Saxo SIM canonical 13 incremental 60m chart",
    research_eligibility: str = "operational_market_data_not_frozen_research_input",
) -> None:
    status = "BLOCKED" if code.startswith("BLOCKED") else "FAILED"
    selected_failed_key_tuple = tuple(str(key) for key in failed_instrument_keys or ())
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_incremental_failure") as conn:
        with conn.cursor() as cursor:
            _ensure_dataset(
                cursor,
                dataset_id=dataset_id,
                spec_relative_path=spec_relative_path,
                dataset_name=dataset_name,
                research_eligibility=research_eligibility,
                instrument_count=max(1, len(selected_failed_key_tuple)),
            )
            _register_sources(cursor, run_id, chart_artifacts, dataset_id=dataset_id)
            cursor.execute(
                """
                SELECT instrument_id,instrument_key,price_basis
                FROM ops.ingestion_run_instrument_scope
                WHERE ingestion_run_id=%s
                ORDER BY instrument_key
                """,
                (run_id,),
            )
            run_scope = [
                {"instrument_id": int(row[0]), "instrument_key": str(row[1]), "price_basis": str(row[2])}
                for row in cursor.fetchall()
            ]
            selected_failed_keys = {
                key.lower() for key in selected_failed_key_tuple
            }
            failed_scope = [
                row for row in run_scope
                if not selected_failed_keys or row["instrument_key"] in selected_failed_keys
            ]
            failed_instrument_ids = [row["instrument_id"] for row in failed_scope]
            if code in {
                "BLOCKED_FULL_REFETCH_REQUIRED",
                "BLOCKED_BOUNDED_REVISION_REQUIRED",
            } and len(failed_instrument_ids) == 1:
                cursor.execute(
                    "UPDATE ops.watermark SET data_status='STALE_DATA_VERSION', updated_at_utc=clock_timestamp() "
                    "WHERE instrument_id=%s AND horizon_minutes=60",
                    (failed_instrument_ids[0],),
                )
            if (
                code == "BLOCKED_BOUNDED_REVISION_REQUIRED"
                and len(failed_scope) == 1
                and revision_detection is not None
            ):
                row = failed_scope[0]
                cursor.execute(
                    """
                    INSERT INTO ops.data_version_revision_event (
                        instrument_id,horizon_minutes,price_basis,
                        detected_ingestion_run_id,old_data_version,new_data_version,
                        reconciliation_status,comparison_from_utc,comparison_to_utc,
                        compared_rows,reason_code,discovery_manifest_relative_path,
                        discovery_manifest_sha256
                    ) VALUES (
                        %s,60,%s,%s,%s,%s,'DETECTED',%s,%s,%s,
                        'DATA_VERSION_CHANGED_PENDING_BOUNDED_COMPARE',%s,%s
                    )
                    ON CONFLICT (
                        instrument_id,horizon_minutes,price_basis,new_data_version
                    ) WHERE reconciliation_status IN (
                        'DETECTED','DISCOVERING','READY_TO_APPLY'
                    ) DO UPDATE SET
                        comparison_from_utc=EXCLUDED.comparison_from_utc,
                        comparison_to_utc=EXCLUDED.comparison_to_utc,
                        compared_rows=EXCLUDED.compared_rows,
                        discovery_manifest_relative_path=EXCLUDED.discovery_manifest_relative_path,
                        discovery_manifest_sha256=EXCLUDED.discovery_manifest_sha256,
                        updated_at_utc=clock_timestamp()
                    """,
                    (
                        row["instrument_id"],
                        row["price_basis"],
                        run_id,
                        int(revision_detection["old_data_version"]),
                        int(revision_detection["new_data_version"]),
                        revision_detection["comparison_from_utc"],
                        revision_detection["comparison_to_utc"],
                        int(revision_detection["compared_rows"]),
                        str(revision_detection["artifact_relative_path"]),
                        str(revision_detection["artifact_sha256"]),
                    ),
                )
            if _records_quality_event(code):
                cursor.executemany(
                    """
                    INSERT INTO quality.event (
                        ingestion_run_id,instrument_id,horizon_minutes,rule_id,severity,
                        observed_value,action,status
                    ) VALUES (%s,%s,60,'db3_atomic_run_gate',%s,%s,%s,'OPEN')
                    """,
                    [
                        (
                            run_id,
                            row["instrument_id"],
                            "CRITICAL" if status == "BLOCKED" else "ERROR",
                            Jsonb(
                                {
                                    "error_code": code,
                                    "instrument_key": row["instrument_key"],
                                    "selected_instrument_keys": [
                                        item["instrument_key"] for item in run_scope
                                    ],
                                    "scope_contract": "ops.ingestion_run_instrument_scope_v1",
                                }
                            ),
                            "do not advance curated/derived/watermark; resolve cause and rerun",
                        )
                        for row in failed_scope
                    ],
                )
            cursor.execute(
                """
                UPDATE ops.ingestion_run SET
                    finished_at_utc=clock_timestamp(),status=%s,error_code=%s,
                    last_success_step='raw_artifacts_preserved_database_rollback',
                    rejected_rows=0
                WHERE ingestion_run_id=%s AND status='RUNNING'
                """,
                (status, code, run_id),
            )


def _record_revision_warning(
    run_id: int,
    chart_artifacts: list[ArtifactRecord],
    detection_artifact: ArtifactRecord,
    revision_detection: Mapping[str, Any],
    *,
    dataset_id: str,
    spec_relative_path: Path,
    dataset_name: str,
    research_eligibility: str,
) -> dict[str, Any]:
    """Persist warning evidence without staging or changing accepted market data."""

    with connect(
        "saxo_ingest", MARKET_DB, application_name="saxo_db_revision_warning_record"
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                _ensure_dataset(
                    cursor,
                    dataset_id=dataset_id,
                    spec_relative_path=spec_relative_path,
                    dataset_name=dataset_name,
                    research_eligibility=research_eligibility,
                    instrument_count=1,
                )
                _register_sources(cursor, run_id, chart_artifacts, dataset_id=dataset_id)
                cursor.execute(
                    """
                    SELECT instrument_id,instrument_key,price_basis
                    FROM ops.ingestion_run_instrument_scope
                    WHERE ingestion_run_id=%s AND instrument_key=%s
                    """,
                    (run_id, revision_detection["instrument_key"]),
                )
                scope = cursor.fetchone()
                if scope is None:
                    raise RuntimeError("FAILED_REVISION_WARNING_SCOPE_MISSING")
                instrument_id = int(scope[0])
                price_basis = str(scope[2])
                cursor.execute(
                    """
                    INSERT INTO ops.data_version_revision_event (
                        instrument_id,horizon_minutes,price_basis,
                        detected_ingestion_run_id,old_data_version,new_data_version,
                        reconciliation_status,comparison_from_utc,comparison_to_utc,
                        compared_rows,content_difference_rows,version_only_rows,
                        new_rows,removed_rows,stable_anchor_rows,reason_code,
                        discovery_manifest_relative_path,discovery_manifest_sha256,
                        policy_id,review_status
                    ) VALUES (
                        %s,60,%s,%s,%s,%s,'REVIEW_PENDING',%s,%s,%s,%s,%s,%s,%s,0,
                        'DATA_VERSION_CHANGED_REVIEW_PENDING',%s,%s,%s,'PENDING_REVIEW'
                    )
                    ON CONFLICT (
                        instrument_id,horizon_minutes,price_basis,new_data_version
                    ) WHERE reconciliation_status IN (
                        'DETECTED','DISCOVERING','READY_TO_APPLY','REVIEW_PENDING'
                    ) DO NOTHING
                    RETURNING revision_event_id
                    """,
                    (
                        instrument_id,
                        price_basis,
                        run_id,
                        int(revision_detection["old_data_version"]),
                        int(revision_detection["new_data_version"]),
                        revision_detection["comparison_from_utc"],
                        revision_detection["comparison_to_utc"],
                        int(revision_detection["provider_rows"]),
                        int(revision_detection["content_difference_rows"]),
                        int(revision_detection["version_only_rows"]),
                        int(revision_detection["new_rows"]),
                        int(revision_detection["removed_rows"]),
                        detection_artifact.relative_path,
                        detection_artifact.sha256,
                        REVISION_WARNING_POLICY_ID,
                    ),
                )
                inserted = cursor.fetchone()
                if inserted is None:
                    cursor.execute(
                        """
                        SELECT revision_event_id
                        FROM ops.data_version_revision_event
                        WHERE instrument_id=%s AND horizon_minutes=60 AND price_basis=%s
                          AND new_data_version=%s AND policy_id=%s
                          AND reconciliation_status='REVIEW_PENDING'
                        FOR UPDATE
                        """,
                        (
                            instrument_id,
                            price_basis,
                            int(revision_detection["new_data_version"]),
                            REVISION_WARNING_POLICY_ID,
                        ),
                    )
                    selected = cursor.fetchone()
                    if selected is None:
                        raise RuntimeError("FAILED_REVISION_WARNING_EVENT_LOOKUP")
                    revision_event_id = int(selected[0])
                else:
                    revision_event_id = int(inserted[0])
                    cursor.execute(
                        "SELECT revision_event_id FROM ops.data_version_revision_event "
                        "WHERE revision_event_id=%s FOR UPDATE",
                        (revision_event_id,),
                    )
                    cursor.fetchone()
                cursor.execute(
                    "SELECT COALESCE(MAX(step_number),0)+1 "
                    "FROM ops.data_version_revision_step WHERE revision_event_id=%s",
                    (revision_event_id,),
                )
                step_number = int(cursor.fetchone()[0])
                cursor.execute(
                    """
                    INSERT INTO ops.data_version_revision_step (
                        revision_event_id,step_number,requested_count,request_mode,
                        request_time_utc,compared_from_utc,compared_to_utc,provider_rows,
                        matched_rows,content_difference_rows,version_only_rows,new_rows,
                        removed_rows,stable_anchor_rows,decision,reason_code,
                        artifact_relative_path,artifact_sha256
                    ) VALUES (
                        %s,%s,%s,'From',%s,%s,%s,%s,%s,%s,%s,%s,%s,0,
                        'WARNING_RECORDED','DATA_VERSION_CHANGED_REVIEW_PENDING',%s,%s
                    )
                    """,
                    (
                        revision_event_id,
                        step_number,
                        max(1, min(1200, int(revision_detection["provider_rows"]))),
                        revision_detection["detected_at_utc"],
                        revision_detection["comparison_from_utc"],
                        revision_detection["comparison_to_utc"],
                        int(revision_detection["provider_rows"]),
                        int(revision_detection["matched_rows"]),
                        int(revision_detection["content_difference_rows"]),
                        int(revision_detection["version_only_rows"]),
                        int(revision_detection["new_rows"]),
                        int(revision_detection["removed_rows"]),
                        detection_artifact.relative_path,
                        detection_artifact.sha256,
                    ),
                )
                cursor.execute(
                    """
                    UPDATE ops.ingestion_run SET
                        finished_at_utc=clock_timestamp(),status='PASS',error_code=NULL,
                        successful_series=0,inserted_rows=0,updated_rows=0,
                        revision_rows=0,rejected_rows=0,
                        last_success_step='revision_warning_recorded_no_curated_change',
                        metadata_json=metadata_json || %s
                    WHERE ingestion_run_id=%s AND status='RUNNING'
                    """,
                    (
                        Jsonb(
                            {
                                "warning_code": REVISION_WARNING_CODE,
                                "revision_event_id": revision_event_id,
                                "data_advanced": False,
                                "curated_rows_changed": 0,
                                "watermark_changed": False,
                                "derived_rows_changed": 0,
                                "orders_or_prechecks_sent": 0,
                            }
                        ),
                        run_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("FAILED_REVISION_WARNING_RUN_UPDATE")
    return {
        "warning_code": REVISION_WARNING_CODE,
        "revision_event_id": revision_event_id,
        "instrument_key": str(revision_detection["instrument_key"]),
        "old_data_version": int(revision_detection["old_data_version"]),
        "new_data_version": int(revision_detection["new_data_version"]),
        "data_advanced": False,
        "curated_rows_changed": 0,
        "watermark_changed": False,
        "derived_rows_changed": 0,
        "review_status": "PENDING_REVIEW",
        "availability_status": "AVAILABLE_WITH_REVISION_WARNING",
    }


def _write_run_manifest(
    artifacts: RunArtifacts,
    *,
    db_run_id: int,
    status: str,
    error_code: str | None,
    smoke_result: dict[str, Any] | None,
    successful_series: int,
    client: SaxoClient | None,
    all_artifacts: list[ArtifactRecord],
    result: dict[str, Any],
    failed_instrument_key: str | None = None,
) -> ArtifactRecord:
    manifest = artifacts.write_manifest(
        {
            "acquisition_run_id": artifacts.run_id,
            "database_ingestion_run_id": db_run_id,
            "environment": "SIM",
            "status": status,
            "error_code": error_code,
            **_failed_instrument_context(status, failed_instrument_key),
            "smoke_test": smoke_result,
            "successful_series": successful_series,
            "request_count": 0 if client is None else client.request_count,
            "write_request_count": 0 if client is None else client.write_request_count,
            "rate_limit_summary": {} if client is None else client.rate_limit_summary,
            "artifacts": [artifact.__dict__ for artifact in all_artifacts],
            "database_result": result,
        }
    )
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_incremental_manifest") as conn:
        with conn.cursor() as cursor:
            cursor.execute(
                "UPDATE ops.ingestion_run SET source_manifest_sha256=%s WHERE ingestion_run_id=%s",
                (manifest.sha256, db_run_id),
            )
    return manifest


def run_incremental(
    client: SaxoClient | None = None,
    *,
    client_factory: Callable[[], SaxoClient] | None = None,
    instrument_keys: Iterable[str] | None = None,
    trigger: str = "manual_db3",
) -> dict[str, Any]:
    registry = select_instruments(instrument_keys)
    dataset_id, spec_path, dataset_name, eligibility = _dataset_contract(registry)
    run_id = utc_run_id(secrets.token_hex(4))
    artifacts = RunArtifacts(run_id)
    db_run_id = _create_run(run_id, registry, trigger=trigger)
    chart_artifacts: list[ArtifactRecord] = []
    all_artifacts: list[ArtifactRecord] = []
    acquired: list[AcquiredInstrument] = []
    failed_instrument_keys: tuple[str, ...] = ()
    failed_instrument_key: str | None = None
    revision_detection: dict[str, Any] | None = None
    revision_warning_result: dict[str, Any] | None = None
    selected_client = client
    smoke_result: dict[str, Any] | None = None
    try:
        if selected_client is not None and client_factory is not None:
            raise ValueError("client and client_factory are mutually exclusive")
        selected_client = selected_client or (
            client_factory() if client_factory is not None else SaxoClient.from_environment()
        )
        smoke_result = selected_client.smoke_test()
        states = _load_states()
        missing = tuple(
            item for item in registry if (item.uic, item.asset_type) not in states
        )
        if missing:
            failed_instrument_keys = tuple(item.key for item in missing)
            failed_instrument_key = failed_instrument_keys[0]
            raise BarQualityError("BLOCKED_CANONICAL_WATERMARK_SET")

        for instrument in registry:
            failed_instrument_key = instrument.key
            failed_instrument_keys = (instrument.key,)
            state = states[(instrument.uic, instrument.asset_type)]
            detail = selected_client.instrument_detail(instrument.uic, instrument.asset_type)
            validate_detail(instrument, detail)
            detail_artifact = artifacts.write_json(
                f"instruments/{instrument.key}/detail.json", detail, row_count=1
            )
            all_artifacts.append(detail_artifact)

            schedule = selected_client.trading_schedule(instrument.uic, instrument.asset_type)
            if not isinstance(schedule.get("Sessions"), list):
                raise BarQualityError("INVALID_TRADING_SCHEDULE")
            schedule_artifact = artifacts.write_json(
                f"instruments/{instrument.key}/trading_schedule.json",
                schedule,
                row_count=len(schedule["Sessions"]),
            )
            all_artifacts.append(schedule_artifact)

            normalized_pages: list[list[NormalizedBar]] = []
            overlap_start = _overlap_start(state, instrument)

            def save_page(page: ChartPage) -> None:
                record = artifacts.write_json(
                    f"instruments/{instrument.key}/chart_{page.page_number:04d}.json",
                    page.payload,
                    row_count=len(page.payload.get("Data") or []),
                )
                chart_artifacts.append(record)
                all_artifacts.append(record)
                normalized_pages.append(
                    normalize_chart_page(
                        instrument,
                        page.payload,
                        retrieved_at_utc=datetime.now(timezone.utc),
                        payload_sha256=record.sha256,
                        artifact_relative_path=record.relative_path,
                    )
                )

            fetch_chart_pages(
                selected_client,
                instrument,
                mode="From",
                time_utc=overlap_start,
                on_page=save_page,
            )
            bars = _finalize_etf_terminal_bar(
                instrument, state, merge_pages(normalized_pages)
            )
            if len(bars) < 2 or not any(bar.is_complete for bar in bars):
                raise BarQualityError("INSUFFICIENT_INCREMENTAL_CHART_DATA")
            versions = {bar.data_version for bar in bars if bar.data_version is not None}
            if len(versions) > 1:
                raise BarQualityError("MULTIPLE_DATA_VERSIONS_IN_RUN")
            observed_version = next(iter(versions), None)
            if any(
                bar.is_complete and bar.time_utc > datetime.now(timezone.utc)
                for bar in bars
            ):
                raise BarQualityError("FUTURE_COMPLETED_BAR")
            if state.data_version is not None and observed_version is not None and observed_version != state.data_version:
                sample = _load_revision_sample(state, instrument.price_basis, bars)
                detected_at = datetime.now(timezone.utc)
                detection_payload = {
                    "policy_id": REVISION_WARNING_POLICY_ID,
                    "instrument_key": instrument.key,
                    "horizon_minutes": 60,
                    "price_basis": instrument.price_basis,
                    "old_data_version": state.data_version,
                    "new_data_version": observed_version,
                    **sample,
                    "detected_at_utc": detected_at,
                    "status": "REVIEW_PENDING",
                    "reason_code": "DATA_VERSION_CHANGED_REVIEW_PENDING",
                    "review_status": "PENDING_REVIEW",
                    "availability_status": "AVAILABLE_WITH_REVISION_WARNING",
                    "data_advanced": False,
                    "curated_rows_changed": 0,
                    "watermark_changed": False,
                    "derived_rows_changed": 0,
                    "orders_or_prechecks_sent": 0,
                }
                detection_artifact = artifacts.write_json(
                    f"instruments/{instrument.key}/revision_detection.json",
                    detection_payload,
                    row_count=len(bars),
                )
                all_artifacts.append(detection_artifact)
                revision_detection = {
                    **detection_payload,
                    "artifact_relative_path": detection_artifact.relative_path,
                    "artifact_sha256": detection_artifact.sha256,
                }
                revision_warning_result = _record_revision_warning(
                    db_run_id,
                    chart_artifacts,
                    detection_artifact,
                    revision_detection,
                    dataset_id=dataset_id,
                    spec_relative_path=spec_path,
                    dataset_name=dataset_name,
                    research_eligibility=eligibility,
                )
                break
            acquired.append(AcquiredInstrument(instrument, state, bars))

        result = (
            revision_warning_result
            if revision_warning_result is not None
            else _commit_acquired(
                db_run_id, acquired, chart_artifacts, dataset_id=dataset_id
            )
        )
        status = "PASS"
        error_code = None
    except Exception as exc:
        error_code = _error_code(exc)
        _record_failure(
            db_run_id,
            error_code,
            chart_artifacts,
            failed_instrument_keys,
            revision_detection=revision_detection,
            dataset_id=dataset_id,
            spec_relative_path=spec_path,
            dataset_name=dataset_name,
            research_eligibility=eligibility,
        )
        result = {}
        status = "BLOCKED" if error_code.startswith("BLOCKED") else "FAILED"

    manifest = _write_run_manifest(
        artifacts,
        db_run_id=db_run_id,
        status=status,
        error_code=error_code,
        smoke_result=smoke_result,
        successful_series=(
            len(acquired)
            if status == "PASS" and revision_warning_result is None
            else 0
        ),
        client=selected_client,
        all_artifacts=all_artifacts,
        result=result,
        failed_instrument_key=failed_instrument_key,
    )
    return {
        "acquisition_run_id": run_id,
        "database_ingestion_run_id": db_run_id,
        "error_code": error_code,
        "manifest_relative_path": manifest.relative_path,
        "orders_or_prechecks_sent": 0,
        "status": status,
        **_failed_instrument_context(status, failed_instrument_key),
        **result,
    }


def run_full_refetch(
    instrument_key: str,
    client: SaxoClient | None = None,
    *,
    client_factory: Callable[[], SaxoClient] | None = None,
    trigger: str = "manual_db3_full_refetch",
) -> dict[str, Any]:
    matches = tuple(item for item in load_canonical_instruments() if item.key == instrument_key.lower())
    if len(matches) != 1:
        raise ValueError("instrument key must identify one canonical instrument")
    instrument = matches[0]
    run_id = utc_run_id(secrets.token_hex(4))
    artifacts = RunArtifacts(run_id)
    db_run_id = _create_run(run_id, matches, trigger=trigger)
    chart_artifacts: list[ArtifactRecord] = []
    all_artifacts: list[ArtifactRecord] = []
    selected_client = client
    smoke_result: dict[str, Any] | None = None
    failed_instrument_keys: tuple[str, ...] = (instrument.key,)
    try:
        state, existing_min_time = _load_full_refetch_state(instrument)
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

        def save_page(page: ChartPage) -> None:
            record = artifacts.write_json(
                f"instruments/{instrument.key}/chart_{page.page_number:04d}.json",
                page.payload,
                row_count=len(page.payload.get("Data") or []),
            )
            chart_artifacts.append(record)
            all_artifacts.append(record)
            if instrument.asset_type == "FxSpot":
                normalized, rejected = normalize_chart_page_quarantining_fx_extrema(
                    instrument,
                    page.payload,
                    retrieved_at_utc=datetime.now(timezone.utc),
                    payload_sha256=record.sha256,
                    artifact_relative_path=record.relative_path,
                )
                normalized_pages.append(normalized)
                rejected_pages.append(rejected)
            else:
                normalized_pages.append(
                    normalize_chart_page(
                        instrument,
                        page.payload,
                        retrieved_at_utc=datetime.now(timezone.utc),
                        payload_sha256=record.sha256,
                        artifact_relative_path=record.relative_path,
                    )
                )

        fetch_chart_pages(
            selected_client,
            instrument,
            mode="UpTo",
            time_utc=datetime.now(timezone.utc),
            on_page=save_page,
        )
        bars = _finalize_etf_terminal_bar(
            instrument, state, merge_pages(normalized_pages)
        )
        quarantined_rows = _validate_full_refetch_quarantine(
            (bar.time_utc for bar in bars),
            (rejected for page in rejected_pages for rejected in page),
        )
        observed_times = [
            *(bar.time_utc for bar in bars),
            *(rejected.time_utc for rejected in quarantined_rows),
        ]
        if len(bars) < 2 or min(observed_times) > existing_min_time:
            raise BarQualityError("BLOCKED_FULL_REFETCH_HISTORY_TRUNCATED")
        versions = {bar.data_version for bar in bars if bar.data_version is not None}
        versions.update(
            rejected.data_version
            for rejected in quarantined_rows
            if rejected.data_version is not None
        )
        if len(versions) != 1:
            raise BarQualityError("MULTIPLE_DATA_VERSIONS_IN_RUN")
        acquired = [AcquiredInstrument(instrument, state, bars)]
        result = _commit_acquired(
            db_run_id,
            acquired,
            chart_artifacts,
            full_replace_instrument_id=state.instrument_id,
            quarantined_rows=quarantined_rows,
        )
        status = "PASS"
        error_code = None
    except Exception as exc:
        error_code = _error_code(exc)
        _record_failure(db_run_id, error_code, chart_artifacts, failed_instrument_keys)
        result = {}
        status = "BLOCKED" if error_code.startswith("BLOCKED") else "FAILED"

    manifest = _write_run_manifest(
        artifacts,
        db_run_id=db_run_id,
        status=status,
        error_code=error_code,
        smoke_result=smoke_result,
        successful_series=1 if status == "PASS" else 0,
        client=selected_client,
        all_artifacts=all_artifacts,
        result=result,
    )
    return {
        "acquisition_run_id": run_id,
        "database_ingestion_run_id": db_run_id,
        "error_code": error_code,
        "instrument_key": instrument.key,
        "manifest_relative_path": manifest.relative_path,
        "orders_or_prechecks_sent": 0,
        "status": status,
        **result,
    }


def incremental_status() -> dict[str, Any]:
    # This summary needs the trigger column, which the general reader view does
    # not expose. Keep the bounded ingest connection explicitly read-only.
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_incremental_status") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT status, COUNT(*) FROM ops.ingestion_run
                WHERE trigger='manual_db3' GROUP BY status ORDER BY status
                """
            )
            runs = {str(status): int(count) for status, count in cursor.fetchall()}
            cursor.execute("SELECT data_status, COUNT(*) FROM ops.watermark GROUP BY data_status")
            watermarks = {str(status): int(count) for status, count in cursor.fetchall()}
    return {"runs": runs, "watermarks": watermarks}


def load_stale_instrument_keys() -> tuple[str, ...]:
    registry_order = {item.key: index for index, item in enumerate(load_canonical_instruments())}
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_stale_watermarks") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT lower(i.market_key)
                FROM ops.watermark w
                JOIN catalog.instrument i USING (instrument_id)
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                  AND w.horizon_minutes=60
                  AND w.data_status='STALE_DATA_VERSION'
                """
            )
            keys = [str(row[0]) for row in cursor.fetchall()]
    return tuple(sorted(keys, key=lambda key: registry_order.get(key, len(registry_order))))


def _reconciliation_step(operation: str, result: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "acquisition_run_id",
        "database_ingestion_run_id",
        "status",
        "error_code",
        "failed_instrument_key",
        "instrument_key",
        "successful_series",
        "inserted_rows",
        "updated_rows",
        "revision_rows",
        "rejected_rows",
        "removed_rows",
        "manifest_relative_path",
    )
    return {"operation": operation, **{key: result.get(key) for key in keys if key in result}}


def reconcile_incremental(
    *,
    normal_runner: Callable[[], dict[str, Any]] | None = None,
    full_refetch_runner: Callable[[str], dict[str, Any]] | None = None,
    required_consecutive_passes: int = 2,
    max_full_refetches: int | None = None,
    stale_key_loader: Callable[[], tuple[str, ...]] | None = None,
    on_step: Callable[[dict[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Recover DataVersion drift and finish with consecutive canonical PASS runs."""
    if required_consecutive_passes < 1:
        raise ValueError("required_consecutive_passes must be positive")
    normal = normal_runner or run_incremental
    full_refetch = full_refetch_runner or run_full_refetch
    load_stale_keys = stale_key_loader or load_stale_instrument_keys
    refetch_limit = len(load_canonical_instruments()) if max_full_refetches is None else max_full_refetches
    if refetch_limit < 0:
        raise ValueError("max_full_refetches must not be negative")

    steps: list[dict[str, Any]] = []
    refetched_keys: list[str] = []
    consecutive_passes = 0
    pass_run_ids: list[int] = []

    def record(operation: str, result: dict[str, Any]) -> None:
        step = _reconciliation_step(operation, result)
        steps.append(step)
        if on_step is not None:
            on_step(step)

    def terminal(result: dict[str, Any], *, error_code: str | None = None) -> dict[str, Any]:
        selected_error = error_code or result.get("error_code")
        return {
            "status": result.get("status", "FAILED"),
            "error_code": selected_error,
            **_failed_instrument_context(
                str(result.get("status", "FAILED")),
                result.get("failed_instrument_key") or result.get("instrument_key"),
            ),
            "consecutive_normal_passes": consecutive_passes,
            "normal_pass_run_ids": pass_run_ids,
            "refetched_instruments": refetched_keys,
            "orders_or_prechecks_sent": 0,
            "steps": steps,
        }

    def recover(failed_key: str) -> dict[str, Any] | None:
        if failed_key in refetched_keys:
            return terminal(
                {"status": "BLOCKED", "failed_instrument_key": failed_key},
                error_code="BLOCKED_REPEATED_DATA_VERSION_CHANGE",
            )
        if len(refetched_keys) >= refetch_limit:
            return terminal(
                {"status": "BLOCKED", "failed_instrument_key": failed_key},
                error_code="BLOCKED_RECONCILIATION_LIMIT",
            )

        refetch_result = full_refetch(failed_key)
        record("full-refetch", refetch_result)
        if refetch_result.get("status") != "PASS":
            return terminal(refetch_result)
        returned_key = refetch_result.get("instrument_key")
        if returned_key is not None and returned_key != failed_key:
            return terminal(refetch_result, error_code="FAILED_REFETCH_INSTRUMENT_MISMATCH")
        refetched_keys.append(failed_key)
        return None

    for stale_key in load_stale_keys():
        blocked = recover(stale_key)
        if blocked is not None:
            return blocked

    while consecutive_passes < required_consecutive_passes:
        normal_result = normal()
        record("run", normal_result)
        if normal_result.get("status") == "PASS":
            consecutive_passes += 1
            run_id = normal_result.get("database_ingestion_run_id")
            if isinstance(run_id, int):
                pass_run_ids.append(run_id)
            continue

        consecutive_passes = 0
        pass_run_ids.clear()
        if normal_result.get("error_code") == "BLOCKED_CANONICAL_WATERMARK_SET":
            stale_keys = load_stale_keys()
            if stale_keys:
                for stale_key in stale_keys:
                    blocked = recover(stale_key)
                    if blocked is not None:
                        return blocked
                continue
        if normal_result.get("error_code") != "BLOCKED_FULL_REFETCH_REQUIRED":
            return terminal(normal_result)

        failed_key = normal_result.get("failed_instrument_key")
        if not isinstance(failed_key, str) or not failed_key:
            return terminal(normal_result, error_code="BLOCKED_MISSING_FAILED_INSTRUMENT_KEY")
        blocked = recover(failed_key)
        if blocked is not None:
            return blocked

    return {
        "status": "PASS",
        "error_code": None,
        "consecutive_normal_passes": consecutive_passes,
        "normal_pass_run_ids": pass_run_ids,
        "refetched_instruments": refetched_keys,
        "orders_or_prechecks_sent": 0,
        "steps": steps,
    }


def oauth_reconcile_runners(
    manager: SaxoOAuthManager,
) -> tuple[Callable[[], dict[str, Any]], Callable[[str], dict[str, Any]]]:
    """Build step-scoped runners so a long reconcile never relies on one access token."""

    def normal() -> dict[str, Any]:
        return run_incremental(
            client_factory=lambda: SaxoClient(manager.access_token()),
        )

    def full_refetch(instrument_key: str) -> dict[str, Any]:
        return run_full_refetch(
            instrument_key,
            client_factory=lambda: SaxoClient(manager.access_token(force_refresh=True)),
        )

    return normal, full_refetch


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Saxo SIM DB3 incremental updater")
    parser.add_argument(
        "command", choices=("initialize-watermarks", "run", "full-refetch", "reconcile", "status")
    )
    parser.add_argument("--instrument-key")
    parser.add_argument("--profile", choices=("canonical", "s6v5a"), default="canonical")
    parser.add_argument("--auth-mode", choices=("environment", "keychain"), default="environment")
    parser.add_argument("--callback-port", type=int, default=DEFAULT_CALLBACK_PORT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.auth_mode == "keychain" and args.command not in {"reconcile", "full-refetch"}:
        parser.error("keychain auth mode is supported only for reconcile or full-refetch")
    if args.command == "initialize-watermarks":
        result = initialize_watermarks()
    elif args.command == "run":
        selected_keys = S6V5A_PRIORITY_INSTRUMENT_KEYS if args.profile == "s6v5a" else None
        result = run_incremental(instrument_keys=selected_keys)
    elif args.command == "full-refetch":
        if not args.instrument_key:
            parser.error("full-refetch requires --instrument-key")
        if args.auth_mode == "keychain":
            oauth_manager = SaxoOAuthManager(
                OAuthConfig.from_environment(callback_port=args.callback_port)
            )
            result = run_full_refetch(
                args.instrument_key,
                client_factory=lambda: SaxoClient(
                    oauth_manager.access_token(force_refresh=True)
                ),
            )
        else:
            result = run_full_refetch(args.instrument_key)
    elif args.command == "reconcile":
        normal_runner = None
        full_refetch_runner = None
        if args.auth_mode == "keychain":
            oauth_manager = SaxoOAuthManager(
                OAuthConfig.from_environment(callback_port=args.callback_port)
            )
            normal_runner, full_refetch_runner = oauth_reconcile_runners(oauth_manager)
        result = reconcile_incremental(
            normal_runner=normal_runner,
            full_refetch_runner=full_refetch_runner,
            on_step=lambda step: print(
                json.dumps({"reconcile_step": step}, sort_keys=True), flush=True
            )
        )
    else:
        result = incremental_status()
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("status", "PASS") == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
