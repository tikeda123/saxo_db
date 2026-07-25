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
from typing import Any, Callable, Iterable

from psycopg.types.json import Jsonb

from .acquire_pages import ChartPage, fetch_chart_pages
from .connection import MARKET_DB, connect, project_root
from .derive_bars import rebuild
from .instrument_registry import (
    CanonicalInstrument,
    InstrumentDriftError,
    load_canonical_instruments,
    validate_detail,
)
from .normalize_bars import (
    BarQualityError,
    NormalizedBar,
    RejectedBar,
    merge_pages,
    normalize_chart_page,
    normalize_chart_page_quarantining_fx_extrema,
)
from .raw_artifacts import ArtifactRecord, RunArtifacts, utc_run_id
from .saxo_client import SaxoAPIError, SaxoClient


DATASET_ID = "v13_saxo_sim_chart_60m_incremental_v1"
SPEC_RELATIVE_PATH = Path("specs/source_collection/v13_db3_incremental_collection.json")
MAX_QUARANTINED_FX_EXTREMA_ROWS = 10
MAX_QUARANTINED_FX_EXTREMA_RATE = Decimal("0.0001")
FX_EXTREMA_QUARANTINE_POLICY_ID = "db3_bounded_fx_extrema_quarantine_v1"
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

    registry = load_canonical_instruments()
    if instrument_keys is None:
        return registry
    requested = tuple(str(key).strip().lower() for key in instrument_keys)
    if not requested or any(not key for key in requested) or len(set(requested)) != len(requested):
        raise ValueError("instrument keys must be a non-empty unique canonical subset")
    by_key = {item.key: item for item in registry}
    unknown = sorted(set(requested) - set(by_key))
    if unknown:
        raise ValueError("instrument keys contain a non-canonical key")
    return tuple(by_key[key] for key in requested)


def _ensure_dataset(cursor: Any) -> None:
    manifest_path = project_root() / SPEC_RELATIVE_PATH
    cursor.execute(
        """
        INSERT INTO catalog.source_dataset (
            source_dataset_id, dataset_name, provider, environment, dataset_kind,
            price_basis, canonical_horizon_minutes, expected_update_interval_seconds,
            freshness_grace_seconds, authoritative_layer, research_eligibility,
            active, source_manifest_relative_path, source_manifest_sha256, metadata_json
        ) VALUES (
            %s,'Saxo SIM canonical 13 incremental 60m chart','Saxo OpenAPI','SIM',
            'raw_market','asset_specific',60,3600,7200,'raw',
            'operational_market_data_not_frozen_research_input',TRUE,%s,%s,%s
        )
        ON CONFLICT (source_dataset_id) DO UPDATE SET
            dataset_name=EXCLUDED.dataset_name,
            active=TRUE,
            source_manifest_relative_path=EXCLUDED.source_manifest_relative_path,
            source_manifest_sha256=EXCLUDED.source_manifest_sha256,
            metadata_json=EXCLUDED.metadata_json
        """,
        (
            DATASET_ID,
            str(SPEC_RELATIVE_PATH),
            _sha256(manifest_path),
            Jsonb({"canonical_instruments": 13, "horizon_minutes": 60, "write_endpoints": 0}),
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


def _create_run(
    run_id: str,
    registry: tuple[CanonicalInstrument, ...],
    *,
    trigger: str = "manual_db3",
) -> int:
    manifest_path = f"data/acquisition/runs/{run_id}/run_manifest.json"
    requested = [
        {"uic": item.uic, "asset_type": item.asset_type, "horizon_minutes": 60}
        for item in registry
    ]
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_incremental_start") as conn:
        with conn.cursor() as cursor:
            _ensure_dataset(cursor)
            cursor.execute(
                """
                INSERT INTO ops.ingestion_run (
                    trigger, environment, status, requested_series,
                    run_manifest_relative_path, last_success_step, metadata_json
                ) VALUES (%s,'SIM','RUNNING',%s,%s,'run_registered',%s)
                RETURNING ingestion_run_id
                """,
                (trigger, Jsonb(requested), manifest_path, Jsonb({"acquisition_run_id": run_id})),
            )
            return int(cursor.fetchone()[0])


def _register_sources(cursor: Any, run_id: int, artifacts: list[ArtifactRecord]) -> dict[str, int]:
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
                DATASET_ID,
            ),
        )
        result[artifact.relative_path] = int(cursor.fetchone()[0])
    return result


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
    if len(unique_rejected) > MAX_QUARANTINED_FX_EXTREMA_ROWS:
        raise BarQualityError("FX_EXTREMA_QUARANTINE_ROW_LIMIT_EXCEEDED")
    if not unique_rejected:
        return ()

    observed_times = accepted | set(rejected_by_time)
    if any(rejected.time_utc == max(observed_times) for rejected in unique_rejected):
        raise BarQualityError("FX_EXTREMA_QUARANTINE_LATEST_SAMPLE_INELIGIBLE")
    rejected_rate = Decimal(len(unique_rejected)) / Decimal(len(observed_times))
    if rejected_rate > MAX_QUARANTINED_FX_EXTREMA_RATE:
        raise BarQualityError("FX_EXTREMA_QUARANTINE_RATE_LIMIT_EXCEEDED")
    return unique_rejected


def _quarantined_row_evidence(rejected: RejectedBar) -> dict[str, Any]:
    return {
        "policy_id": FX_EXTREMA_QUARANTINE_POLICY_ID,
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
) -> dict[str, Any]:
    if quarantined_rows and (
        full_replace_instrument_id is None
        or len(acquired) != 1
        or acquired[0].registry.asset_type != "FxSpot"
        or acquired[0].state.instrument_id != full_replace_instrument_id
    ):
        raise BarQualityError("FX_EXTREMA_QUARANTINE_SCOPE_VIOLATION")
    quarantined_rows = _validate_full_refetch_quarantine(
        (bar.time_utc for item in acquired for bar in item.bars),
        quarantined_rows,
    )
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_incremental_commit") as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext('saxo_db_db3_incremental'))")
                cursor.execute("DELETE FROM staging.market_bar WHERE ingestion_run_id=%s", (run_id,))
                sources = _register_sources(cursor, run_id, chart_artifacts)
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
                            'bounded manual full-refetch quarantine accepted by frozen DB3 policy'
                        )
                        """,
                        (
                            run_id,
                            full_replace_instrument_id,
                            rejected.time_utc,
                            Jsonb(_quarantined_row_evidence(rejected)),
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

                derived_counts = rebuild(cursor)
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
                                "quarantine_policy_id": (
                                    FX_EXTREMA_QUARANTINE_POLICY_ID if quarantined_rows else None
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
        "inserted_rows": inserted_rows,
        "raw_rows": raw_rows,
        "rejected_rows": len(quarantined_rows),
        "quarantined_fx_extrema": [
            _quarantined_row_evidence(rejected) for rejected in quarantined_rows
        ],
        "removed_rows": removed_rows,
        "revision_rows": revision_rows,
        "updated_rows": updated_rows,
    }


def _error_code(exc: Exception) -> str:
    if isinstance(exc, SaxoAPIError):
        return exc.code
    if isinstance(exc, InstrumentDriftError):
        return str(exc).split(":", 1)[0]
    if isinstance(exc, BarQualityError):
        return str(exc)
    if isinstance(exc, ValueError) and str(exc) == "SAXO_ACCESS_TOKEN is required":
        return "BLOCKED_LIVE_SIM_TOKEN"
    return f"FAILED_{type(exc).__name__.upper()}"


def _failed_instrument_context(status: str, instrument_key: str | None) -> dict[str, str]:
    if status == "PASS" or instrument_key is None:
        return {}
    return {"failed_instrument_key": instrument_key}


def _record_failure(
    run_id: int,
    code: str,
    chart_artifacts: list[ArtifactRecord],
    failed_instrument_id: int | None,
) -> None:
    status = "BLOCKED" if code.startswith("BLOCKED") else "FAILED"
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_incremental_failure") as conn:
        with conn.cursor() as cursor:
            _ensure_dataset(cursor)
            _register_sources(cursor, run_id, chart_artifacts)
            if code == "BLOCKED_FULL_REFETCH_REQUIRED" and failed_instrument_id is not None:
                cursor.execute(
                    "UPDATE ops.watermark SET data_status='STALE_DATA_VERSION', updated_at_utc=clock_timestamp() "
                    "WHERE instrument_id=%s AND horizon_minutes=60",
                    (failed_instrument_id,),
                )
            cursor.execute(
                """
                INSERT INTO quality.event (
                    ingestion_run_id,instrument_id,horizon_minutes,rule_id,severity,
                    observed_value,action,status
                ) VALUES (%s,%s,60,'db3_atomic_run_gate',%s,%s,%s,'OPEN')
                """,
                (
                    run_id,
                    failed_instrument_id,
                    "CRITICAL" if status == "BLOCKED" else "ERROR",
                    Jsonb({"error_code": code}),
                    "do not advance curated/derived/watermark; resolve cause and rerun",
                ),
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
    instrument_keys: Iterable[str] | None = None,
    trigger: str = "manual_db3",
) -> dict[str, Any]:
    registry = select_instruments(instrument_keys)
    run_id = utc_run_id(secrets.token_hex(4))
    artifacts = RunArtifacts(run_id)
    db_run_id = _create_run(run_id, registry, trigger=trigger)
    chart_artifacts: list[ArtifactRecord] = []
    all_artifacts: list[ArtifactRecord] = []
    acquired: list[AcquiredInstrument] = []
    failed_instrument_id: int | None = None
    failed_instrument_key: str | None = None
    selected_client = client
    smoke_result: dict[str, Any] | None = None
    try:
        selected_client = selected_client or SaxoClient.from_environment()
        smoke_result = selected_client.smoke_test()
        states = _load_states()
        if not {(item.uic, item.asset_type) for item in registry}.issubset(states):
            raise BarQualityError("BLOCKED_CANONICAL_WATERMARK_SET")

        for instrument in registry:
            failed_instrument_key = instrument.key
            state = states[(instrument.uic, instrument.asset_type)]
            failed_instrument_id = state.instrument_id
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
            bars = tuple(merge_pages(normalized_pages))
            if len(bars) < 2 or not any(bar.is_complete for bar in bars):
                raise BarQualityError("INSUFFICIENT_INCREMENTAL_CHART_DATA")
            versions = {bar.data_version for bar in bars if bar.data_version is not None}
            if len(versions) > 1:
                raise BarQualityError("MULTIPLE_DATA_VERSIONS_IN_RUN")
            observed_version = next(iter(versions), None)
            if state.data_version is not None and observed_version is not None and observed_version != state.data_version:
                raise BarQualityError("BLOCKED_FULL_REFETCH_REQUIRED")
            if any(
                bar.is_complete and bar.time_utc > datetime.now(timezone.utc)
                for bar in bars
            ):
                raise BarQualityError("FUTURE_COMPLETED_BAR")
            acquired.append(AcquiredInstrument(instrument, state, bars))

        result = _commit_acquired(db_run_id, acquired, chart_artifacts)
        status = "PASS"
        error_code = None
    except Exception as exc:
        error_code = _error_code(exc)
        _record_failure(db_run_id, error_code, chart_artifacts, failed_instrument_id)
        result = {}
        status = "BLOCKED" if error_code.startswith("BLOCKED") else "FAILED"

    manifest = _write_run_manifest(
        artifacts,
        db_run_id=db_run_id,
        status=status,
        error_code=error_code,
        smoke_result=smoke_result,
        successful_series=len(acquired) if status == "PASS" else 0,
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
    failed_instrument_id: int | None = None
    try:
        state, existing_min_time = _load_full_refetch_state(instrument)
        failed_instrument_id = state.instrument_id
        selected_client = selected_client or SaxoClient.from_environment()
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
        bars = tuple(merge_pages(normalized_pages))
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
        _record_failure(db_run_id, error_code, chart_artifacts, failed_instrument_id)
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


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Saxo SIM DB3 incremental updater")
    parser.add_argument(
        "command", choices=("initialize-watermarks", "run", "full-refetch", "reconcile", "status")
    )
    parser.add_argument("--instrument-key")
    parser.add_argument("--profile", choices=("canonical", "s6v5a"), default="canonical")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "initialize-watermarks":
        result = initialize_watermarks()
    elif args.command == "run":
        selected_keys = S6V5A_PRIORITY_INSTRUMENT_KEYS if args.profile == "s6v5a" else None
        result = run_incremental(instrument_keys=selected_keys)
    elif args.command == "full-refetch":
        if not args.instrument_key:
            parser.error("full-refetch requires --instrument-key")
        result = run_full_refetch(args.instrument_key)
    elif args.command == "reconcile":
        result = reconcile_incremental(
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
