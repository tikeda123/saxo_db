#!/usr/bin/env python3
"""Read-only TIP/GLD bounded-imputation candidate review from immutable evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from market_db.c2_imputation import plan_c2_session_imputation
from market_db.connection import MARKET_DB, connect, project_root
from market_db.instrument_registry import load_canonical_instruments
from market_db.normalize_bars import merge_pages, normalize_chart_page


TARGETS = ("tip", "gld")
SESSION_DATE = date(2026, 7, 29)


def _repo_path(relative_path: str) -> Path:
    selected = Path(relative_path)
    if selected.is_absolute() or ".." in selected.parts:
        raise RuntimeError("UNSAFE_EVIDENCE_PATH")
    resolved = (project_root() / selected).resolve()
    resolved.relative_to(project_root().resolve())
    return resolved


def _utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def review() -> dict[str, Any]:
    registry = {item.key: item for item in load_canonical_instruments()}
    reviewed: list[dict[str, Any]] = []
    with connect(
        "saxo_ingest", MARKET_DB,
        application_name="c2_bounded_imputation_candidate_review",
    ) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            read_only = str(conn.execute("SHOW transaction_read_only").fetchone()[0])
            for key in TARGETS:
                row = conn.execute(
                    """
                    SELECT e.revision_event_id,e.new_data_version,
                           e.discovery_manifest_relative_path,
                           i.session_calendar_id
                    FROM ops.data_version_revision_event e
                    JOIN catalog.instrument i USING (instrument_id)
                    WHERE lower(i.market_key)=%s
                    ORDER BY e.revision_event_id DESC LIMIT 1
                    """,
                    (key,),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"REVISION_EVENT_NOT_FOUND:{key}")
                event_id, data_version, discovery_relative, calendar_id = row
                discovery_path = _repo_path(str(discovery_relative))
                chart_path = discovery_path.with_name("chart_0001.json")
                discovery = json.loads(discovery_path.read_text(encoding="utf-8"))
                chart_payload = json.loads(chart_path.read_text(encoding="utf-8"))
                chart_sha256 = hashlib.sha256(chart_path.read_bytes()).hexdigest()
                chart_relative = str(chart_path.relative_to(project_root()))
                bars = merge_pages([
                    normalize_chart_page(
                        registry[key],
                        chart_payload,
                        retrieved_at_utc=datetime.fromisoformat(
                            str(discovery["detected_at_utc"]).replace("Z", "+00:00")
                        ),
                        payload_sha256=chart_sha256,
                        artifact_relative_path=chart_relative,
                    )
                ])
                session = conn.execute(
                    """
                    SELECT si.open_time_utc,si.close_time_utc,
                           c.metadata_json->>'verification_status'
                    FROM catalog.session_interval si
                    JOIN catalog.session_calendar c USING (session_calendar_id)
                    WHERE si.session_calendar_id=%s AND si.session_date=%s
                      AND si.session_status <> 'HOLIDAY'
                    ORDER BY si.interval_sequence LIMIT 1
                    """,
                    (calendar_id, SESSION_DATE),
                ).fetchone()
                if session is None:
                    raise RuntimeError(f"SESSION_NOT_FOUND:{key}")
                session_open, session_close, verification = session
                expected: list[datetime] = []
                selected = session_open
                while selected < session_close:
                    expected.append(selected)
                    selected += timedelta(hours=1)
                actual = [bar for bar in bars if session_open <= bar.time_utc < session_close]
                previous_session = conn.execute(
                    """
                    SELECT open_time_utc,close_time_utc
                    FROM catalog.session_interval
                    WHERE session_calendar_id=%s AND session_status <> 'HOLIDAY'
                      AND close_time_utc <= %s
                    ORDER BY close_time_utc DESC LIMIT 1
                    """,
                    (calendar_id, session_open),
                ).fetchone()
                previous_terminal_time = None
                if previous_session is not None:
                    previous_open, previous_close = previous_session
                    duration = (previous_close - previous_open).total_seconds()
                    previous_terminal_time = previous_open + timedelta(
                        hours=int((duration - 1) // 3600)
                    )
                previous = max(
                    (
                        bar for bar in bars
                        if bar.time_utc == previous_terminal_time and bar.is_complete
                    ),
                    key=lambda bar: bar.time_utc,
                    default=None,
                )
                plan = plan_c2_session_imputation(
                    instrument_key=key,
                    session_date=SESSION_DATE,
                    expected_times_utc=expected,
                    actual_bars=actual,
                    calendar_verified=str(verification) == "VERIFIED",
                    previous_session_terminal_bar=previous,
                    previous_session_terminal_time_utc=previous_terminal_time,
                )
                reviewed.append(
                    {
                        "instrument_key": key,
                        "revision_event_id": int(event_id),
                        "candidate_data_version": int(data_version),
                        "status": plan.status,
                        "warning_ids": list(plan.warning_ids),
                        "blocker_ids": list(plan.blocker_ids),
                        "expected_slot_count": plan.expected_slot_count,
                        "actual_slot_count": plan.actual_slot_count,
                        "imputed_row_count": len(plan.imputed_rows),
                        "imputed_rows": [
                            {
                                "time_utc": _utc(item.time_utc),
                                "source_kind": item.source_kind,
                                "reason": item.reason,
                                "source_time_utc": _utc(item.source_time_utc),
                                "consecutive_gap_index": item.consecutive_gap_index,
                                "consecutive_gap_count": item.consecutive_gap_count,
                                "source_payload_sha256": item.source_payload_sha256,
                                "source_artifact_relative_path": item.source_artifact_relative_path,
                                "provider_values_emitted": False,
                            }
                            for item in plan.imputed_rows
                        ],
                    }
                )
    return {
        "review_id": "c2_etf11_bounded_imputation_candidate_review_20260801",
        "generated_at_utc": _utc(datetime.now(timezone.utc)),
        "policy_id": "c2_etf_bounded_previous_valid_v1",
        "database_transaction_read_only": read_only,
        "saxo_get_requests": 0,
        "database_writes": 0,
        "orders_or_prechecks_sent": 0,
        "usdjpy_touched": False,
        "production_apply_performed": False,
        "series": reviewed,
    }


def main() -> int:
    print(json.dumps(review(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
