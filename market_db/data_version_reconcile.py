"""Explicit, review-gated reconciliation for Saxo Chart DataVersion changes.

Normal acquisition records future DataVersion changes as non-blocking warning
evidence.  This module keeps review and apply as separate manual actions.  An
apply requires an audited APPROVE_APPLY decision and an exact event identity;
there is no scheduler entry point or automatic full-history fallback.  Canonical
provider values are never repaired, swapped, clamped, filled or interpolated.
An independently audited C2-only overlay may be appended after canonical
rebuild, but it never changes the accepted provider rows.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from psycopg.types.json import Jsonb

from .connection import MARKET_DB, connect, project_root
from .c2_imputation import refresh_c2_imputation_overlay
from .derive_bars import rebuild
from .incremental_update import (
    DATASET_ID,
    AcquiredInstrument,
    InstrumentState,
    _create_run,
    _register_sources,
    _stage,
    _write_run_manifest,
)
from .instrument_registry import CanonicalInstrument, load_canonical_instruments, validate_detail
from .normalize_bars import BarQualityError, NormalizedBar, normalize_chart_page
from .raw_artifacts import ArtifactRecord, RunArtifacts, utc_run_id
from .saxo_auth import DEFAULT_CALLBACK_PORT, OAuthConfig, SaxoOAuthManager
from .saxo_client import SaxoClient


REVISION_POLICY_ID = "bounded_data_version_reconciliation_v1"
WINDOW_COUNTS = (96, 384, 1200)
STABLE_ANCHOR_ROWS = 16
MAX_AFFECTED_ROWS = 240
MAX_REMOVED_ROWS = 64
CORPORATE_ACTION_RATIO = 0.80


@dataclass(frozen=True)
class StoredBar:
    time_utc: datetime
    content: tuple[Any, ...]
    data_version: int | None


@dataclass(frozen=True)
class RevisionComparison:
    decision: str
    reason_code: str
    old_data_version: int
    new_data_version: int
    compared_from_utc: datetime
    compared_to_utc: datetime
    provider_rows: int
    matched_rows: int
    content_difference_rows: int
    version_only_rows: int
    new_rows: int
    removed_rows: int
    stable_anchor_rows: int
    affected_from_utc: datetime | None
    affected_to_utc: datetime | None
    content_difference_times: tuple[datetime, ...]
    new_times: tuple[datetime, ...]
    removed_times: tuple[datetime, ...]

    def public_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        for key, value in tuple(payload.items()):
            if isinstance(value, datetime):
                payload[key] = _utc_text(value)
            elif isinstance(value, tuple) and value and isinstance(value[0], datetime):
                payload[key] = [_utc_text(item) for item in value]
        return payload


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _bar_content(bar: NormalizedBar) -> tuple[Any, ...]:
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


def compare_revision_window(
    provider_bars: Sequence[NormalizedBar],
    stored_bars: Mapping[datetime, StoredBar],
    *,
    old_data_version: int,
    requested_count: int,
    maximum_count: int = WINDOW_COUNTS[-1],
    stable_anchor_required: int = STABLE_ANCHOR_ROWS,
) -> RevisionComparison:
    """Compare one provider window and decide whether its boundary is proven."""

    if not provider_bars:
        raise BarQualityError("REVISION_EMPTY_PROVIDER_WINDOW")
    ordered = tuple(sorted(provider_bars, key=lambda item: item.time_utc))
    if tuple(item.time_utc for item in ordered) != tuple(
        sorted({item.time_utc for item in ordered})
    ):
        raise BarQualityError("REVISION_NON_UNIQUE_PROVIDER_WINDOW")
    versions = {item.data_version for item in ordered if item.data_version is not None}
    if len(versions) != 1:
        raise BarQualityError("REVISION_MULTIPLE_OR_MISSING_DATA_VERSIONS")
    new_data_version = int(next(iter(versions)))
    if new_data_version == old_data_version:
        raise BarQualityError("REVISION_DATA_VERSION_NOT_CHANGED")

    provider_by_time = {item.time_utc: item for item in ordered}
    content_differences: list[datetime] = []
    version_only: list[datetime] = []
    new_times: list[datetime] = []
    matched = 0
    for bar in ordered:
        stored = stored_bars.get(bar.time_utc)
        if stored is None:
            new_times.append(bar.time_utc)
            continue
        matched += 1
        if _bar_content(bar) != stored.content:
            content_differences.append(bar.time_utc)
        elif stored.data_version != new_data_version:
            version_only.append(bar.time_utc)

    lower = ordered[0].time_utc
    upper = ordered[-1].time_utc
    removed_times = sorted(
        time_utc
        for time_utc in stored_bars
        if lower <= time_utc <= upper and time_utc not in provider_by_time
    )
    affected = sorted({*content_differences, *new_times, *removed_times})

    if affected:
        affected_from = affected[0]
        affected_to = affected[-1]
        preceding = [item for item in ordered if item.time_utc < affected_from]
    else:
        affected_from = None
        affected_to = None
        preceding = list(ordered)

    stable_anchor = 0
    for bar in reversed(preceding):
        stored = stored_bars.get(bar.time_utc)
        if stored is None or not bar.is_complete or _bar_content(bar) != stored.content:
            break
        stable_anchor += 1

    matched_complete = sum(
        1
        for bar in ordered
        if bar.is_complete and bar.time_utc in stored_bars
    )
    completed_content_differences = sum(
        1
        for time_utc in content_differences
        if provider_by_time[time_utc].is_complete
    )
    change_ratio = (
        completed_content_differences / matched_complete if matched_complete else 0.0
    )
    affected_span_rows = sum(
        1
        for bar in ordered
        if affected_from is not None
        and affected_from <= bar.time_utc <= affected_to
    ) + len(removed_times)

    at_window_boundary = bool(affected and affected_from == lower)
    if len(removed_times) > MAX_REMOVED_ROWS:
        decision = "BLOCKED_FULL_REFETCH"
        reason = "REVISION_REMOVAL_LIMIT_EXCEEDED"
    elif affected_span_rows > MAX_AFFECTED_ROWS:
        decision = "BLOCKED_FULL_REFETCH"
        reason = "REVISION_AFFECTED_RANGE_LIMIT_EXCEEDED"
    elif (
        matched_complete >= stable_anchor_required
        and change_ratio >= CORPORATE_ACTION_RATIO
    ):
        decision = "BLOCKED_FULL_REFETCH"
        reason = "REVISION_WHOLE_HISTORY_CHANGE_SUSPECTED"
    elif stable_anchor < stable_anchor_required or at_window_boundary:
        if requested_count < maximum_count:
            decision = "EXPAND"
            reason = "REVISION_STABLE_BOUNDARY_NOT_YET_PROVEN"
        else:
            decision = "BLOCKED_FULL_REFETCH"
            reason = "REVISION_COMPARISON_LIMIT_EXCEEDED"
    else:
        decision = "READY_TO_APPLY"
        reason = (
            "REVISION_NO_CONTENT_CHANGE_WITH_STABLE_ANCHOR"
            if not affected
            else "REVISION_BOUNDED_RANGE_IDENTIFIED"
        )

    return RevisionComparison(
        decision=decision,
        reason_code=reason,
        old_data_version=old_data_version,
        new_data_version=new_data_version,
        compared_from_utc=lower,
        compared_to_utc=upper,
        provider_rows=len(ordered),
        matched_rows=matched,
        content_difference_rows=len(content_differences),
        version_only_rows=len(version_only),
        new_rows=len(new_times),
        removed_rows=len(removed_times),
        stable_anchor_rows=stable_anchor,
        affected_from_utc=affected_from,
        affected_to_utc=affected_to,
        content_difference_times=tuple(content_differences),
        new_times=tuple(new_times),
        removed_times=tuple(removed_times),
    )


def _instrument(instrument_key: str) -> CanonicalInstrument:
    matches = tuple(
        item
        for item in load_canonical_instruments()
        if item.key == instrument_key.lower()
    )
    if len(matches) != 1:
        raise ValueError("instrument key must identify one canonical instrument")
    return matches[0]


def _load_revision_state(instrument: CanonicalInstrument) -> InstrumentState:
    with connect(
        "saxo_ingest", MARKET_DB, application_name="saxo_db_revision_state"
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT i.instrument_id,w.latest_complete_time_utc,w.data_version,w.data_status
                FROM catalog.instrument i
                JOIN ops.watermark w USING (instrument_id)
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                  AND i.uic=%s AND i.asset_type=%s
                  AND w.horizon_minutes=60 AND w.price_basis=%s
                """,
                (instrument.uic, instrument.asset_type, instrument.price_basis),
            )
            row = cursor.fetchone()
    if row is None or row[2] is None:
        raise BarQualityError("REVISION_WATERMARK_STATE_MISSING")
    if str(row[3]) != "STALE_DATA_VERSION":
        raise BarQualityError("REVISION_WATERMARK_NOT_STALE")
    return InstrumentState(int(row[0]), row[1], int(row[2]), str(row[3]))


def _load_approved_revision_state(
    instrument: CanonicalInstrument, revision_event_id: int
) -> InstrumentState:
    """Load a future-policy event only after a separately audited approval."""

    with connect(
        "saxo_ingest", MARKET_DB, application_name="saxo_db_approved_revision_state"
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT i.instrument_id,w.latest_complete_time_utc,w.data_version,w.data_status,
                       e.old_data_version,e.new_data_version,e.reconciliation_status,
                       e.policy_id,e.review_status
                FROM catalog.instrument i
                JOIN ops.watermark w USING (instrument_id)
                JOIN ops.data_version_revision_event e
                  ON e.instrument_id=i.instrument_id
                 AND e.horizon_minutes=w.horizon_minutes
                 AND e.price_basis=w.price_basis
                WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                  AND i.uic=%s AND i.asset_type=%s
                  AND w.horizon_minutes=60 AND w.price_basis=%s
                  AND e.revision_event_id=%s
                """,
                (
                    instrument.uic,
                    instrument.asset_type,
                    instrument.price_basis,
                    revision_event_id,
                ),
            )
            row = cursor.fetchone()
    if row is None:
        raise BarQualityError("REVISION_APPROVED_EVENT_NOT_FOUND")
    if (
        str(row[3]) != "ACTIVE"
        or row[2] is None
        or int(row[2]) != int(row[4])
        or str(row[6]) != "REVIEW_PENDING"
        or str(row[7]) != "data_version_revision_warning_v2"
        or str(row[8]) != "APPLY_APPROVED"
    ):
        raise BarQualityError("REVISION_APPLY_APPROVAL_GUARD_FAILED")
    return InstrumentState(int(row[0]), row[1], int(row[2]), str(row[3]))


def record_revision_review(
    revision_event_id: int,
    *,
    decision: str,
    reviewer: str,
    note: str,
) -> dict[str, Any]:
    """Record a human review decision without changing market data."""

    selected = decision.upper()
    if selected not in {"KEEP_CURRENT", "APPROVE_APPLY"}:
        raise ValueError("decision must be KEEP_CURRENT or APPROVE_APPLY")
    with connect(
        "saxo_ops_operator", MARKET_DB, application_name="saxo_db_revision_review"
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute(
                    "CALL ops.review_data_version_revision(%s,%s,%s,%s)",
                    (revision_event_id, selected, reviewer, note),
                )
                cursor.execute(
                    """
                    SELECT instrument_key,reconciliation_status,availability_status,
                           review_status,policy_id,old_data_version,new_data_version
                    FROM ops.v_data_version_revision_state
                    WHERE revision_event_id=%s
                    """,
                    (revision_event_id,),
                )
                row = cursor.fetchone()
                if row is None:
                    raise RuntimeError("FAILED_REVISION_REVIEW_READBACK")
    return {
        "status": "PASS",
        "revision_event_id": revision_event_id,
        "instrument_key": str(row[0]),
        "reconciliation_status": str(row[1]),
        "availability_status": str(row[2]),
        "review_status": str(row[3]),
        "policy_id": str(row[4]),
        "old_data_version": int(row[5]),
        "new_data_version": int(row[6]),
        "curated_rows_changed": 0,
        "watermark_changed": False,
        "derived_rows_changed": 0,
        "orders_or_prechecks_sent": 0,
    }


def _load_stored_window(
    instrument_id: int,
    price_basis: str,
    lower: datetime,
    upper: datetime,
) -> dict[datetime, StoredBar]:
    with connect(
        "saxo_ingest", MARKET_DB, application_name="saxo_db_revision_compare"
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
                (instrument_id, price_basis, lower, upper),
            )
            rows = cursor.fetchall()
    return {
        row[0]: StoredBar(row[0], tuple(row[1:-1]), None if row[-1] is None else int(row[-1]))
        for row in rows
    }


def _evidence_payload(
    instrument: CanonicalInstrument,
    comparisons: Sequence[RevisionComparison],
    *,
    source: str,
) -> dict[str, Any]:
    final = comparisons[-1]
    return {
        "policy_id": REVISION_POLICY_ID,
        "source": source,
        "instrument_key": instrument.key,
        "uic": instrument.uic,
        "asset_type": instrument.asset_type,
        "horizon_minutes": 60,
        "price_basis": instrument.price_basis,
        "old_data_version": final.old_data_version,
        "new_data_version": final.new_data_version,
        "final_decision": final.decision,
        "final_reason_code": final.reason_code,
        "steps": [item.public_dict() for item in comparisons],
        "quality_controls": {
            "interpolation": False,
            "bid_ask_swap": False,
            "clamp": False,
            "manual_watermark_update": False,
            "orders": 0,
            "prechecks": 0,
            "write_requests_to_saxo": 0,
        },
    }


def analyze_retained_chart(
    instrument_key: str,
    chart_path: Path,
) -> dict[str, Any]:
    """Read-only comparison for an already retained blocked-run chart payload."""

    instrument = _instrument(instrument_key)
    selected_path = chart_path if chart_path.is_absolute() else project_root() / chart_path
    raw = selected_path.read_bytes()
    payload = json.loads(raw)
    state = _load_revision_state(instrument)
    bars = tuple(
        normalize_chart_page(
            instrument,
            payload,
            retrieved_at_utc=datetime.now(timezone.utc),
            payload_sha256=hashlib.sha256(raw).hexdigest(),
            artifact_relative_path=str(selected_path.relative_to(project_root())),
        )
    )
    stored = _load_stored_window(
        state.instrument_id,
        instrument.price_basis,
        bars[0].time_utc,
        bars[-1].time_utc,
    )
    comparison = compare_revision_window(
        bars,
        stored,
        old_data_version=int(state.data_version),
        requested_count=len(bars),
        maximum_count=len(bars),
    )
    return _evidence_payload(
        instrument,
        (comparison,),
        source="retained_blocked_run_read_only",
    )


def _insert_revision_event(
    cursor: Any,
    *,
    run_id: int,
    state: InstrumentState,
    instrument: CanonicalInstrument,
    comparisons: Sequence[RevisionComparison],
    step_artifacts: Sequence[ArtifactRecord],
    evidence: ArtifactRecord,
    status: str,
    approved_revision_event_id: int | None = None,
) -> int:
    final = comparisons[-1]
    if len(step_artifacts) != len(comparisons):
        raise RuntimeError("REVISION_STEP_ARTIFACT_COUNT_MISMATCH")
    if approved_revision_event_id is not None:
        cursor.execute(
            """
            SELECT revision_event_id
            FROM ops.data_version_revision_event
            WHERE revision_event_id=%s AND instrument_id=%s
              AND horizon_minutes=60 AND price_basis=%s
              AND old_data_version=%s AND new_data_version=%s
              AND reconciliation_status='REVIEW_PENDING'
              AND policy_id='data_version_revision_warning_v2'
              AND review_status='APPLY_APPROVED'
            FOR UPDATE
            """,
            (
                approved_revision_event_id,
                state.instrument_id,
                instrument.price_basis,
                final.old_data_version,
                final.new_data_version,
            ),
        )
        selected = cursor.fetchone()
        if selected is None:
            raise BarQualityError("REVISION_APPLY_APPROVAL_GUARD_FAILED")
        event_id = int(selected[0])
        cursor.execute(
            """
            UPDATE ops.data_version_revision_event SET
                reconciliation_status=%s,comparison_from_utc=%s,comparison_to_utc=%s,
                compared_rows=%s,content_difference_rows=%s,version_only_rows=%s,
                new_rows=%s,removed_rows=%s,affected_from_utc=%s,affected_to_utc=%s,
                stable_anchor_rows=%s,reason_code=%s,
                discovery_manifest_relative_path=%s,discovery_manifest_sha256=%s,
                updated_at_utc=clock_timestamp()
            WHERE revision_event_id=%s
            """,
            (
                status,
                final.compared_from_utc,
                final.compared_to_utc,
                final.provider_rows,
                final.content_difference_rows,
                final.version_only_rows,
                final.new_rows,
                final.removed_rows,
                final.affected_from_utc,
                final.affected_to_utc,
                final.stable_anchor_rows,
                final.reason_code,
                evidence.relative_path,
                evidence.sha256,
                event_id,
            ),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("FAILED_APPROVED_REVISION_EVENT_UPDATE")
    else:
        cursor.execute(
            """
            SELECT revision_event_id
            FROM ops.data_version_revision_event
            WHERE instrument_id=%s AND horizon_minutes=60 AND price_basis=%s
              AND new_data_version=%s
              AND reconciliation_status IN ('DETECTED','DISCOVERING','READY_TO_APPLY')
            FOR UPDATE
            """,
            (state.instrument_id, instrument.price_basis, final.new_data_version),
        )
        existing = cursor.fetchone()
        if existing is not None:
            event_id = int(existing[0])
            cursor.execute(
                """
                UPDATE ops.data_version_revision_event SET
                    old_data_version=%s,reconciliation_status=%s,
                    comparison_from_utc=%s,comparison_to_utc=%s,compared_rows=%s,
                    content_difference_rows=%s,version_only_rows=%s,new_rows=%s,
                    removed_rows=%s,affected_from_utc=%s,affected_to_utc=%s,
                    stable_anchor_rows=%s,reason_code=%s,
                    discovery_manifest_relative_path=%s,
                    discovery_manifest_sha256=%s,updated_at_utc=clock_timestamp()
                WHERE revision_event_id=%s
                """,
                (
                    final.old_data_version,
                    status,
                    final.compared_from_utc,
                    final.compared_to_utc,
                    final.provider_rows,
                    final.content_difference_rows,
                    final.version_only_rows,
                    final.new_rows,
                    final.removed_rows,
                    final.affected_from_utc,
                    final.affected_to_utc,
                    final.stable_anchor_rows,
                    final.reason_code,
                    evidence.relative_path,
                    evidence.sha256,
                    event_id,
                ),
            )
        else:
            cursor.execute(
                """
                INSERT INTO ops.data_version_revision_event (
                    instrument_id,horizon_minutes,price_basis,detected_ingestion_run_id,
                    old_data_version,new_data_version,reconciliation_status,
                    comparison_from_utc,comparison_to_utc,compared_rows,
                    content_difference_rows,version_only_rows,new_rows,removed_rows,
                    affected_from_utc,affected_to_utc,stable_anchor_rows,reason_code,
                    discovery_manifest_relative_path,discovery_manifest_sha256
                ) VALUES (
                    %s,60,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s
                ) RETURNING revision_event_id
                """,
                (
                    state.instrument_id,
                    instrument.price_basis,
                    run_id,
                    final.old_data_version,
                    final.new_data_version,
                    status,
                    final.compared_from_utc,
                    final.compared_to_utc,
                    final.provider_rows,
                    final.content_difference_rows,
                    final.version_only_rows,
                    final.new_rows,
                    final.removed_rows,
                    final.affected_from_utc,
                    final.affected_to_utc,
                    final.stable_anchor_rows,
                    final.reason_code,
                    evidence.relative_path,
                    evidence.sha256,
                ),
            )
            event_id = int(cursor.fetchone()[0])
    # A warning-only event already owns its immutable detection step.  Review
    # and apply append new comparison steps; reusing 1..N would collide with
    # the event's primary key and roll the whole guarded transaction back.
    cursor.execute(
        """
        SELECT COALESCE(MAX(step_number),0)
        FROM ops.data_version_revision_step
        WHERE revision_event_id=%s
        """,
        (event_id,),
    )
    step_offset = int(cursor.fetchone()[0])
    cursor.executemany(
        """
        INSERT INTO ops.data_version_revision_step (
            revision_event_id,step_number,requested_count,request_mode,
            request_time_utc,compared_from_utc,compared_to_utc,provider_rows,
            matched_rows,content_difference_rows,version_only_rows,new_rows,
            removed_rows,stable_anchor_rows,decision,reason_code,
            artifact_relative_path,artifact_sha256
        ) VALUES (%s,%s,%s,'UpTo',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        [
            (
                event_id,
                step_offset + index,
                WINDOW_COUNTS[min(index - 1, len(WINDOW_COUNTS) - 1)],
                item.compared_to_utc,
                item.compared_from_utc,
                item.compared_to_utc,
                item.provider_rows,
                item.matched_rows,
                item.content_difference_rows,
                item.version_only_rows,
                item.new_rows,
                item.removed_rows,
                item.stable_anchor_rows,
                item.decision,
                item.reason_code,
                step_artifacts[index - 1].relative_path,
                step_artifacts[index - 1].sha256,
            )
            for index, item in enumerate(comparisons, start=1)
        ],
    )
    return event_id


def _commit_bounded_revision(
    *,
    run_id: int,
    instrument: CanonicalInstrument,
    state: InstrumentState,
    final_bars: Sequence[NormalizedBar],
    chart_artifacts: Sequence[ArtifactRecord],
    evidence: ArtifactRecord,
    comparisons: Sequence[RevisionComparison],
    approved_revision_event_id: int | None = None,
) -> dict[str, Any]:
    final = comparisons[-1]
    if final.decision != "READY_TO_APPLY":
        raise BarQualityError("REVISION_NOT_READY_TO_APPLY")
    if final.affected_from_utc is None:
        affected_from = final.compared_to_utc
        affected_to = final.compared_to_utc
        # A version-only no-op still replaces one stable boundary row so the
        # guarded procedure cannot delete it and the new provider version is
        # represented without rewriting the whole comparison window.
        apply_bars: tuple[NormalizedBar, ...] = tuple(
            bar for bar in final_bars if bar.time_utc == affected_from
        )
    else:
        affected_from = final.affected_from_utc
        affected_to = final.affected_to_utc
        apply_bars = tuple(
            bar
            for bar in final_bars
            if affected_from <= bar.time_utc <= affected_to
        )
    acquired = [AcquiredInstrument(instrument, state, tuple(final_bars))]

    with connect(
        "saxo_ingest", MARKET_DB, application_name="saxo_db_bounded_revision_commit"
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute(
                    "SELECT pg_advisory_xact_lock(hashtext('saxo_db_db3_incremental'))"
                )
                cursor.execute(
                    "DELETE FROM staging.market_bar WHERE ingestion_run_id=%s", (run_id,)
                )
                sources = _register_sources(
                    cursor, run_id, list(chart_artifacts), dataset_id=DATASET_ID
                )
                _stage(cursor, run_id, acquired, sources)
                event_id = _insert_revision_event(
                    cursor,
                    run_id=run_id,
                    state=state,
                    instrument=instrument,
                    comparisons=comparisons,
                    step_artifacts=chart_artifacts,
                    evidence=evidence,
                    status="READY_TO_APPLY",
                    approved_revision_event_id=approved_revision_event_id,
                )

                cursor.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (WHERE c.instrument_id IS NULL),
                        COUNT(*) FILTER (
                            WHERE c.instrument_id IS NOT NULL AND ROW(
                                c.open,c.high,c.low,c.close,c.open_bid,c.high_bid,
                                c.low_bid,c.close_bid,c.open_ask,c.high_ask,c.low_ask,
                                c.close_ask,c.volume,c.market_trading_state,c.is_complete
                            ) IS DISTINCT FROM ROW(
                                s.open,s.high,s.low,s.close,s.open_bid,s.high_bid,
                                s.low_bid,s.close_bid,s.open_ask,s.high_ask,s.low_ask,
                                s.close_ask,s.volume,s.market_trading_state,s.is_complete
                            )
                        )
                    FROM staging.market_bar s
                    LEFT JOIN curated.market_bar c
                      ON c.instrument_id=s.instrument_id
                     AND c.horizon_minutes=s.horizon_minutes
                     AND c.time_utc=s.time_utc
                     AND c.price_basis=s.price_basis
                    WHERE s.ingestion_run_id=%s
                      AND s.time_utc BETWEEN %s AND %s
                    """,
                    (run_id, affected_from, affected_to),
                )
                inserted_rows, updated_rows = (int(value) for value in cursor.fetchone())

                cursor.execute(
                    "INSERT INTO raw.market_bar_revision SELECT * FROM staging.market_bar WHERE ingestion_run_id=%s",
                    (run_id,),
                )
                raw_rows = cursor.rowcount
                cursor.execute(
                    "CALL curated.prepare_bounded_revision(%s,%s,%s,%s,%s)",
                    (run_id, event_id, state.instrument_id, affected_from, affected_to),
                )
                if apply_bars:
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
                        FROM staging.market_bar
                        WHERE ingestion_run_id=%s AND time_utc BETWEEN %s AND %s
                        """,
                        (run_id, affected_from, affected_to),
                    )

                latest_seen = max(bar.time_utc for bar in final_bars)
                completed = [bar.time_utc for bar in final_bars if bar.is_complete]
                latest_complete = max(completed) if completed else state.latest_complete_time_utc
                cursor.execute(
                    """
                    UPDATE ops.watermark SET
                        latest_seen_time_utc=GREATEST(latest_seen_time_utc,%s),
                        latest_complete_time_utc=GREATEST(latest_complete_time_utc,%s),
                        data_version=%s,last_ingestion_run_id=%s,data_status='ACTIVE',
                        updated_at_utc=clock_timestamp()
                    WHERE instrument_id=%s AND horizon_minutes=60 AND price_basis=%s
                    """,
                    (
                        latest_seen,
                        latest_complete,
                        final.new_data_version,
                        run_id,
                        state.instrument_id,
                        instrument.price_basis,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("FAILED_BOUNDED_REVISION_WATERMARK_UPDATE")

                derived = rebuild(cursor, instrument_ids=(state.instrument_id,))
                c2_imputation = refresh_c2_imputation_overlay(
                    cursor, instrument_ids=(state.instrument_id,)
                )
                replacement = {
                    "policy_id": REVISION_POLICY_ID,
                    "inserted_rows": inserted_rows,
                    "updated_rows": updated_rows,
                    "removed_rows": final.removed_rows,
                    "raw_rows": raw_rows,
                    "derived": derived,
                    "c2_imputation_overlay": c2_imputation,
                    "affected_from_utc": _utc_text(affected_from),
                    "affected_to_utc": _utc_text(affected_to),
                    "other_instruments_touched": 0,
                }
                cursor.execute(
                    """
                    UPDATE ops.data_version_revision_event SET
                        reconciliation_status='APPLIED',applied_ingestion_run_id=%s,
                        replacement_result=%s,
                        review_status=CASE
                            WHEN policy_id='data_version_revision_warning_v2' THEN 'APPLIED'
                            ELSE review_status
                        END,
                        updated_at_utc=clock_timestamp()
                    WHERE revision_event_id=%s AND reconciliation_status='READY_TO_APPLY'
                    """,
                    (run_id, Jsonb(replacement), event_id),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("FAILED_BOUNDED_REVISION_EVENT_UPDATE")
                cursor.execute(
                    """
                    INSERT INTO quality.event (
                        ingestion_run_id,instrument_id,horizon_minutes,rule_id,severity,
                        observed_value,action,status,resolved_at_utc,resolved_by,resolution_note
                    ) VALUES (
                        %s,%s,60,'db3_bounded_data_version_revision','INFO',%s,
                        'bounded provider range superseded through immutable raw and curated path',
                        'RESOLVED',clock_timestamp(),'data_version_reconcile',
                        'stable boundary and frozen limits passed'
                    )
                    """,
                    (
                        run_id,
                        state.instrument_id,
                        Jsonb(
                            {
                                "old_data_version": final.old_data_version,
                                "new_data_version": final.new_data_version,
                                "comparison_from_utc": _utc_text(final.compared_from_utc),
                                "comparison_to_utc": _utc_text(final.compared_to_utc),
                                "content_difference_rows": final.content_difference_rows,
                                "replacement": replacement,
                            }
                        ),
                    ),
                )
                cursor.execute(
                    "DELETE FROM staging.market_bar WHERE ingestion_run_id=%s", (run_id,)
                )
                cursor.execute(
                    """
                    UPDATE ops.ingestion_run SET
                        finished_at_utc=clock_timestamp(),status='PASS',successful_series=1,
                        inserted_rows=%s,updated_rows=%s,revision_rows=%s,rejected_rows=0,
                        last_success_step='bounded_revision_committed',metadata_json=metadata_json || %s
                    WHERE ingestion_run_id=%s AND status='RUNNING'
                    """,
                    (
                        inserted_rows,
                        updated_rows,
                        final.content_difference_rows + final.removed_rows,
                        Jsonb(replacement),
                        run_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError("FAILED_BOUNDED_REVISION_RUN_UPDATE")
    return {"revision_event_id": event_id, **replacement}


def _record_unresolved_revision(
    *,
    run_id: int,
    instrument: CanonicalInstrument,
    state: InstrumentState,
    chart_artifacts: Sequence[ArtifactRecord],
    evidence: ArtifactRecord,
    comparisons: Sequence[RevisionComparison],
) -> dict[str, Any]:
    final = comparisons[-1]
    with connect(
        "saxo_ingest", MARKET_DB, application_name="saxo_db_bounded_revision_block"
    ) as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                _register_sources(cursor, run_id, list(chart_artifacts), dataset_id=DATASET_ID)
                event_id = _insert_revision_event(
                    cursor,
                    run_id=run_id,
                    state=state,
                    instrument=instrument,
                    comparisons=comparisons,
                    step_artifacts=chart_artifacts,
                    evidence=evidence,
                    status="BLOCKED_FULL_REFETCH",
                )
                cursor.execute(
                    """
                    UPDATE ops.ingestion_run SET
                        finished_at_utc=clock_timestamp(),status='BLOCKED',
                        error_code='BLOCKED_REVISION_SCOPE_UNRESOLVED_FULL_REFETCH_REQUIRED',
                        last_success_step='revision_evidence_preserved_no_curated_change',
                        metadata_json=metadata_json || %s
                    WHERE ingestion_run_id=%s AND status='RUNNING'
                    """,
                    (
                        Jsonb(
                            {
                                "revision_event_id": event_id,
                                "reason_code": final.reason_code,
                                "orders_or_prechecks_sent": 0,
                            }
                        ),
                        run_id,
                    ),
                )
    return {
        "revision_event_id": event_id,
        "reason_code": final.reason_code,
        "required_action": "guarded single-instrument full-refetch",
    }


def run_bounded_revision_reconcile(
    instrument_key: str,
    client: SaxoClient | None = None,
    *,
    trigger: str = "manual_db3_bounded_revision",
    approved_revision_event_id: int | None = None,
) -> dict[str, Any]:
    instrument = _instrument(instrument_key)
    state = (
        _load_approved_revision_state(instrument, approved_revision_event_id)
        if approved_revision_event_id is not None
        else _load_revision_state(instrument)
    )
    run_id = utc_run_id(secrets.token_hex(4))
    artifacts = RunArtifacts(run_id)
    database_run_id = _create_run(run_id, (instrument,), trigger=trigger)
    selected_client = client
    all_artifacts: list[ArtifactRecord] = []
    chart_artifacts: list[ArtifactRecord] = []
    comparisons: list[RevisionComparison] = []
    final_bars: tuple[NormalizedBar, ...] = ()
    result: dict[str, Any] = {}
    error_code: str | None = None
    smoke_result: dict[str, Any] | None = None
    status = "FAILED"
    try:
        selected_client = selected_client or SaxoClient.from_environment()
        smoke_result = selected_client.smoke_test()
        detail = selected_client.instrument_detail(instrument.uic, instrument.asset_type)
        validate_detail(instrument, detail)
        all_artifacts.append(
            artifacts.write_json(
                f"instruments/{instrument.key}/detail.json", detail, row_count=1
            )
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

        request_time = datetime.now(timezone.utc)
        for step_number, count in enumerate(WINDOW_COUNTS, start=1):
            payload = selected_client.chart(
                instrument.uic,
                instrument.asset_type,
                count=count,
                mode="UpTo",
                time_utc=_utc_text(request_time),
            )
            record = artifacts.write_json(
                f"instruments/{instrument.key}/revision_window_{step_number:02d}_{count}.json",
                payload,
                row_count=len(payload.get("Data") or []),
            )
            chart_artifacts.append(record)
            all_artifacts.append(record)
            final_bars = tuple(
                normalize_chart_page(
                    instrument,
                    payload,
                    retrieved_at_utc=datetime.now(timezone.utc),
                    payload_sha256=record.sha256,
                    artifact_relative_path=record.relative_path,
                )
            )
            stored = _load_stored_window(
                state.instrument_id,
                instrument.price_basis,
                final_bars[0].time_utc,
                final_bars[-1].time_utc,
            )
            comparison = compare_revision_window(
                final_bars,
                stored,
                old_data_version=int(state.data_version),
                requested_count=count,
            )
            comparisons.append(comparison)
            if comparison.decision != "EXPAND":
                break

        evidence = artifacts.write_json(
            f"instruments/{instrument.key}/revision_evidence.json",
            _evidence_payload(instrument, comparisons, source="live_saxo_get"),
            row_count=len(comparisons),
        )
        all_artifacts.append(evidence)
        if comparisons[-1].decision == "READY_TO_APPLY":
            result = _commit_bounded_revision(
                run_id=database_run_id,
                instrument=instrument,
                state=state,
                final_bars=final_bars,
                chart_artifacts=chart_artifacts,
                evidence=evidence,
                comparisons=comparisons,
                approved_revision_event_id=approved_revision_event_id,
            )
            status = "PASS"
        else:
            result = _record_unresolved_revision(
                run_id=database_run_id,
                instrument=instrument,
                state=state,
                chart_artifacts=chart_artifacts,
                evidence=evidence,
                comparisons=comparisons,
            )
            status = "BLOCKED"
            error_code = "BLOCKED_REVISION_SCOPE_UNRESOLVED_FULL_REFETCH_REQUIRED"
    except Exception as exc:
        error_code = str(exc) if isinstance(exc, BarQualityError) else f"FAILED_{type(exc).__name__.upper()}"
        status = "BLOCKED" if error_code.startswith("BLOCKED") else "FAILED"
        with connect(
            "saxo_ingest", MARKET_DB, application_name="saxo_db_bounded_revision_failure"
        ) as conn:
            with conn.cursor() as cursor:
                if chart_artifacts:
                    _register_sources(cursor, database_run_id, chart_artifacts, dataset_id=DATASET_ID)
                cursor.execute(
                    """
                    UPDATE ops.ingestion_run SET finished_at_utc=clock_timestamp(),status=%s,
                        error_code=%s,last_success_step='revision_failure_no_curated_change'
                    WHERE ingestion_run_id=%s AND status='RUNNING'
                    """,
                    (status, error_code, database_run_id),
                )

    manifest = _write_run_manifest(
        artifacts,
        db_run_id=database_run_id,
        status=status,
        error_code=error_code,
        smoke_result=smoke_result,
        successful_series=1 if status == "PASS" else 0,
        client=selected_client,
        all_artifacts=all_artifacts,
        result=result,
        failed_instrument_key=None if status == "PASS" else instrument.key,
    )
    return {
        "acquisition_run_id": run_id,
        "database_ingestion_run_id": database_run_id,
        "instrument_key": instrument.key,
        "status": status,
        "error_code": error_code,
        "manifest_relative_path": manifest.relative_path,
        "orders_or_prechecks_sent": 0,
        **result,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="review-first Saxo DataVersion reconciliation"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    dry = subparsers.add_parser("dry-run-retained")
    dry.add_argument("--instrument-key", required=True)
    dry.add_argument("--chart-path", required=True, type=Path)
    review = subparsers.add_parser("review")
    review.add_argument("--revision-event-id", required=True, type=int)
    review.add_argument(
        "--decision", required=True, choices=("KEEP_CURRENT", "APPROVE_APPLY")
    )
    review.add_argument("--reviewer", required=True)
    review.add_argument("--note", required=True)
    apply = subparsers.add_parser("apply")
    apply.add_argument("--instrument-key", required=True)
    apply.add_argument("--revision-event-id", required=True, type=int)
    apply.add_argument("--confirm", required=True, choices=("APPLY_RECONCILE",))
    apply.add_argument(
        "--auth-mode", choices=("environment", "keychain"), default="environment"
    )
    apply.add_argument("--callback-port", type=int, default=DEFAULT_CALLBACK_PORT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "dry-run-retained":
        result = analyze_retained_chart(args.instrument_key, args.chart_path)
    elif args.command == "review":
        result = record_revision_review(
            args.revision_event_id,
            decision=args.decision,
            reviewer=args.reviewer,
            note=args.note,
        )
    else:
        client = None
        if args.auth_mode == "keychain":
            manager = SaxoOAuthManager(
                OAuthConfig.from_environment(callback_port=args.callback_port)
            )
            access_token = manager.access_token(force_refresh=True)
            try:
                client = SaxoClient(access_token)
            finally:
                access_token = ""
        result = run_bounded_revision_reconcile(
            args.instrument_key,
            client=client,
            trigger="manual_db3_approved_revision_apply",
            approved_revision_event_id=args.revision_event_id,
        )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status", result.get("final_decision")) in {
        "PASS", "READY_TO_APPLY"
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
