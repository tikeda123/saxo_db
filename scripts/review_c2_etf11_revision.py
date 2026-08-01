#!/usr/bin/env python3
"""Read-only audit of pending C2 ETF11 Saxo DataVersion revisions.

The command reads the immutable revision evidence and accepted curated rows.  It
never calls Saxo and starts every database transaction as READ ONLY.  Output is
JSON so the review can be reproduced without mutating revision state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from market_db.connection import MARKET_DB, connect, project_root
from market_db.instrument_registry import load_canonical_instruments
from market_db.normalize_bars import (
    NormalizedBar,
    mark_terminal_session_bar_complete,
    merge_pages,
    normalize_chart_page,
)


ETF11 = ("spy", "iwm", "efa", "eem", "vnq", "shy", "ief", "tlt", "tip", "lqd", "gld")
CONTENT_FIELDS = (
    "open", "high", "low", "close",
    "open_bid", "high_bid", "low_bid", "close_bid",
    "open_ask", "high_ask", "low_ask", "close_ask",
    "volume", "market_trading_state", "is_complete",
)


def _json_value(value: Any) -> Any:
    if isinstance(value, (datetime,)):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, Decimal):
        return str(value)
    return value


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_repo_path(relative_path: str) -> Path:
    selected = Path(relative_path)
    if selected.is_absolute() or ".." in selected.parts:
        raise RuntimeError("UNSAFE_EVIDENCE_PATH")
    resolved = (project_root() / selected).resolve()
    resolved.relative_to(project_root().resolve())
    return resolved


def _bar_content(bar: NormalizedBar) -> tuple[Any, ...]:
    return tuple(getattr(bar, field) for field in CONTENT_FIELDS)


def _load_gap_probe(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    selected = path if path.is_absolute() else project_root() / path
    selected = selected.resolve()
    selected.relative_to(project_root().resolve())
    payload = json.loads(selected.read_text(encoding="utf-8"))
    if payload.get("probe_id") != "c2_tip_gld_20260729_readonly_probe_v1":
        raise RuntimeError("UNREVIEWED_GAP_PROBE")
    if payload.get("requested_instruments") != ["tip", "gld"]:
        raise RuntimeError("GAP_PROBE_SCOPE_MISMATCH")
    if (
        payload.get("method") != "GET"
        or payload.get("saxo_get_request_count") != 2
        or payload.get("write_requests_to_saxo") != 0
        or payload.get("database_writes") != 0
        or payload.get("orders_or_prechecks_sent") != 0
        or payload.get("usdjpy_touched") is not False
        or payload.get("raw_payload_persisted") is not False
        or payload.get("provider_values_emitted") is not False
    ):
        raise RuntimeError("GAP_PROBE_SAFETY_CONTRACT_FAILED")
    return {
        "relative_path": str(selected.relative_to(project_root())),
        "sha256": _sha256(selected),
        "generated_at_utc": payload.get("generated_at_utc"),
        "saxo_get_request_count": 2,
        "series": {str(item["instrument_key"]): item for item in payload["series"]},
    }


def _latest_events() -> dict[str, dict[str, Any]]:
    with connect(
        "saxo_app_reader", MARKET_DB,
        application_name="c2_etf11_revision_readonly_review",
    ) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            rows = conn.execute(
                """
                SELECT instrument_key,revision_event_id,old_data_version,new_data_version,
                       reconciliation_status,review_status,availability_status,
                       last_accepted_data_version,last_accepted_complete_time_utc,
                       latest_provider_observed_time_utc,evidence_sample_count
                FROM ops.v_series_revision_availability
                WHERE instrument_key=ANY(%s)
                ORDER BY instrument_key
                """,
                (list(ETF11),),
            ).fetchall()
    return {
        str(row[0]): {
            "revision_event_id": int(row[1]),
            "old_data_version": int(row[2]),
            "new_data_version": int(row[3]),
            "reconciliation_status": str(row[4]),
            "review_status": str(row[5]),
            "availability_status": str(row[6]),
            "last_accepted_data_version": int(row[7]),
            "last_accepted_complete_time_utc": row[8],
            "latest_provider_observed_time_utc": row[9],
            "evidence_sample_count": int(row[10]),
        }
        for row in rows
    }


def review(*, gap_probe_path: Path | None = None) -> dict[str, Any]:
    registry = {item.key: item for item in load_canonical_instruments()}
    events = _latest_events()
    gap_probe = _load_gap_probe(gap_probe_path)
    if set(events) != set(ETF11):
        raise RuntimeError("ETF11_PENDING_EVENT_SET_INCOMPLETE")

    reviewed: list[dict[str, Any]] = []
    with connect(
        "saxo_ingest", MARKET_DB,
        application_name="c2_etf11_revision_readonly_review",
    ) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            transaction_read_only = conn.execute("SHOW transaction_read_only").fetchone()[0]
            for key in ETF11:
                event = events[key]
                row = conn.execute(
                    """
                    SELECT e.instrument_id,e.comparison_from_utc,e.comparison_to_utc,
                           e.compared_rows,e.content_difference_rows,e.version_only_rows,
                           e.new_rows,e.removed_rows,e.discovery_manifest_relative_path,
                           e.discovery_manifest_sha256,i.session_calendar_id
                    FROM ops.data_version_revision_event e
                    JOIN catalog.instrument i ON i.instrument_id=e.instrument_id
                    WHERE e.revision_event_id=%s
                    """,
                    (event["revision_event_id"],),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"REVISION_EVENT_NOT_FOUND:{key}")
                instrument_id = int(row[0])
                lower, upper = row[1], row[2]
                detection_relative = str(row[8])
                detection_path = _safe_repo_path(detection_relative)
                chart_path = detection_path.with_name("chart_0001.json")
                manifest_path = detection_path.parents[2] / "run_manifest.json"
                detection_payload = json.loads(detection_path.read_text(encoding="utf-8"))
                chart_payload = json.loads(chart_path.read_text(encoding="utf-8"))
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                manifest_artifacts = {
                    item["relative_path"]: item for item in manifest.get("artifacts", [])
                }
                chart_relative = str(chart_path.relative_to(project_root()))
                step_artifacts = conn.execute(
                    """
                    SELECT artifact_relative_path,artifact_sha256
                    FROM ops.data_version_revision_step
                    WHERE revision_event_id=%s
                    ORDER BY step_number
                    """,
                    (event["revision_event_id"],),
                ).fetchall()
                repeated_chart_sha256: set[str] = set()
                repeated_evidence_hashes_valid = True
                for step_relative, step_sha256 in step_artifacts:
                    step_path = _safe_repo_path(str(step_relative))
                    repeated_evidence_hashes_valid = (
                        repeated_evidence_hashes_valid
                        and _sha256(step_path) == str(step_sha256)
                    )
                    repeated_chart_sha256.add(_sha256(step_path.with_name("chart_0001.json")))
                raw_times = [datetime.fromisoformat(str(item["Time"]).replace("Z", "+00:00")) for item in chart_payload["Data"]]
                raw_sha = _sha256(chart_path)
                detection_sha = _sha256(detection_path)
                normalized = merge_pages([
                    normalize_chart_page(
                        registry[key], chart_payload,
                        retrieved_at_utc=datetime.fromisoformat(
                            str(detection_payload["detected_at_utc"]).replace("Z", "+00:00")
                        ),
                        payload_sha256=raw_sha,
                        artifact_relative_path=chart_relative,
                    )
                ])

                expected_rows = conn.execute(
                    """
                    SELECT si.session_date,slot.time_utc,
                           si.open_time_utc,si.close_time_utc
                    FROM catalog.session_interval si
                    CROSS JOIN LATERAL generate_series(
                        si.open_time_utc,
                        si.close_time_utc - interval '1 minute',
                        interval '1 hour'
                    ) AS slot(time_utc)
                    WHERE si.session_calendar_id=%s
                      AND si.session_status <> 'HOLIDAY'
                      AND slot.time_utc BETWEEN %s AND %s
                    ORDER BY slot.time_utc
                    """,
                    (str(row[10]), lower, upper),
                ).fetchall()
                terminal_session = next(
                    (
                        item for item in expected_rows
                        if item[2] <= normalized[-1].time_utc < item[3]
                    ),
                    None,
                )
                if terminal_session is not None:
                    normalized = mark_terminal_session_bar_complete(
                        normalized,
                        session_open_utc=terminal_session[2],
                        session_close_utc=terminal_session[3],
                    )

                stored_rows = conn.execute(
                    """
                    SELECT time_utc,open,high,low,close,
                           open_bid,high_bid,low_bid,close_bid,
                           open_ask,high_ask,low_ask,close_ask,
                           volume,market_trading_state,is_complete,data_version
                    FROM curated.market_bar
                    WHERE instrument_id=%s AND horizon_minutes=60
                      AND price_basis='native_ohlc' AND time_utc BETWEEN %s AND %s
                    ORDER BY time_utc
                    """,
                    (instrument_id, lower, upper),
                ).fetchall()
                stored = {item[0]: (tuple(item[1:-1]), item[-1]) for item in stored_rows}
                changed: list[dict[str, Any]] = []
                matched = version_only = new_rows = 0
                for bar in normalized:
                    previous = stored.get(bar.time_utc)
                    if previous is None:
                        new_rows += 1
                        continue
                    matched += 1
                    previous_content, previous_version = previous
                    current_content = _bar_content(bar)
                    fields = [
                        {
                            "field": field,
                            "accepted": _json_value(before),
                            "provider": _json_value(after),
                        }
                        for field, before, after in zip(
                            CONTENT_FIELDS, previous_content, current_content, strict=True
                        )
                        if before != after
                    ]
                    if fields:
                        changed.append({
                            "time_utc": _json_value(bar.time_utc),
                            "fields": fields,
                        })
                    elif previous_version != bar.data_version:
                        version_only += 1

                provider_times = {bar.time_utc for bar in normalized}
                removed = sum(lower <= time <= upper and time not in provider_times for time in stored)
                expected_times = {item[1] for item in expected_rows}
                expected_by_session: dict[str, set[datetime]] = {}
                for session_date, time_utc, _open_time, _close_time in expected_rows:
                    expected_by_session.setdefault(str(session_date), set()).add(time_utc)
                missing_expected = sorted(expected_times - provider_times)
                out_of_session = sorted(provider_times - expected_times)
                by_session = Counter(item.date().isoformat() for item in provider_times)
                complete_times = [bar.time_utc for bar in normalized if bar.is_complete]
                provider_by_time = {bar.time_utc: bar for bar in normalized}
                stored_complete = {item[0]: bool(item[-2]) for item in stored_rows}
                derived_session_simulation = []
                for session_date, session_times in sorted(expected_by_session.items()):
                    completed_times = {
                        time_utc
                        for time_utc in session_times
                        if (
                            provider_by_time[time_utc].is_complete
                            if time_utc in provider_by_time
                            else stored_complete.get(time_utc, False)
                        )
                    }
                    derived_session_simulation.append({
                        "session_date": session_date,
                        "expected_slots": len(session_times),
                        "completed_slots_after_overlay": len(completed_times),
                        "missing_or_incomplete_times_utc": [
                            _json_value(item) for item in sorted(session_times - completed_times)
                        ],
                        "would_derive_quality_status": (
                            "PASS" if completed_times == session_times else "WARN"
                        ),
                    })
                source_checks = {
                    "detection_sha256_matches_event": detection_sha == str(row[9]),
                    "detection_sha256_matches_manifest": (
                        manifest_artifacts.get(detection_relative, {}).get("sha256") == detection_sha
                    ),
                    "chart_sha256_matches_manifest": (
                        manifest_artifacts.get(chart_relative, {}).get("sha256") == raw_sha
                    ),
                    "all_recorded_step_hashes_match_files": repeated_evidence_hashes_valid,
                    "repeated_provider_chart_content_is_stable": len(repeated_chart_sha256) == 1,
                    "evidence_sample_count_matches_steps": (
                        event["evidence_sample_count"] == len(step_artifacts)
                    ),
                }
                gap_probe_evidence = None
                if key in {"tip", "gld"} and gap_probe is not None:
                    probe_item = gap_probe["series"].get(key)
                    if probe_item is None:
                        raise RuntimeError(f"GAP_PROBE_SERIES_MISSING:{key}")
                    gap_probe_evidence = {
                        "probe_relative_path": gap_probe["relative_path"],
                        "probe_sha256": gap_probe["sha256"],
                        "data_version_matches_revision": (
                            int(probe_item["data_version"]) == event["new_data_version"]
                        ),
                        "missing_times_match_immutable_evidence": (
                            probe_item["missing_expected_times_utc"]
                            == [_json_value(item) for item in missing_expected]
                        ),
                        "session_gap_status": probe_item["session_gap_status"],
                        "normalization_status": probe_item["normalization_status"],
                        "timestamps_strict_unique": probe_item["timestamps_strict_unique"],
                        "provider_rows": probe_item["provider_rows"],
                    }
                    source_checks["limited_provider_gap_probe_consistent"] = all(
                        (
                            gap_probe_evidence["data_version_matches_revision"],
                            gap_probe_evidence["missing_times_match_immutable_evidence"],
                            gap_probe_evidence["session_gap_status"]
                            == "PROVIDER_ROWS_STILL_MISSING",
                            gap_probe_evidence["normalization_status"] == "PASS",
                            gap_probe_evidence["timestamps_strict_unique"] is True,
                        )
                    )
                computed_counts = {
                    "provider_rows": len(normalized),
                    "matched_rows": matched,
                    "content_difference_rows": len(changed),
                    "version_only_rows": version_only,
                    "new_rows": new_rows,
                    "removed_rows": removed,
                }
                recorded_counts = {
                    "provider_rows": int(row[3]),
                    "content_difference_rows": int(row[4]),
                    "version_only_rows": int(row[5]),
                    "new_rows": int(row[6]),
                    "removed_rows": int(row[7]),
                }
                counts_match = all(
                    computed_counts[name] == value
                    for name, value in recorded_counts.items()
                )
                curated_sample_quality_pass = (
                    all(source_checks.values())
                    and raw_times == sorted(raw_times)
                    and len(raw_times) == len(set(raw_times))
                    and not missing_expected
                    and not out_of_session
                    and counts_match
                    and len(changed) == 1
                    and removed == 0
                )
                new_or_changed_session_dates = {
                    bar.time_utc.date().isoformat()
                    for bar in normalized
                    if bar.time_utc not in stored or _bar_content(bar) != stored[bar.time_utc][0]
                }
                affected_daily_sessions = [
                    item for item in derived_session_simulation
                    if item["session_date"] in new_or_changed_session_dates
                ]
                daily_derived_quality_pass = bool(affected_daily_sessions) and all(
                    item["would_derive_quality_status"] == "PASS"
                    for item in affected_daily_sessions
                )
                overall_apply_ready = curated_sample_quality_pass and daily_derived_quality_pass
                reviewed.append({
                    "instrument_key": key,
                    **{name: _json_value(value) for name, value in event.items()},
                    "comparison_from_utc": _json_value(lower),
                    "comparison_to_utc": _json_value(upper),
                    "latest_provider_complete_time_utc": _json_value(max(complete_times)),
                    "raw_last_incomplete_time_utc": _json_value(normalized[-1].time_utc),
                    "source_checks": source_checks,
                    "limited_provider_gap_probe": gap_probe_evidence,
                    "repeated_evidence": {
                        "sample_count": len(step_artifacts),
                        "distinct_provider_chart_sha256_count": len(repeated_chart_sha256),
                    },
                    "raw_order_strict": raw_times == sorted(raw_times) and len(raw_times) == len(set(raw_times)),
                    "required_ohlc_normalization": "PASS",
                    "expected_slot_rows": len(expected_times),
                    "missing_expected_times_utc": [_json_value(item) for item in missing_expected],
                    "out_of_session_times_utc": [_json_value(item) for item in out_of_session],
                    "provider_rows_by_utc_date": dict(sorted(by_session.items())),
                    "derived_session_simulation": derived_session_simulation,
                    "affected_daily_session_dates": sorted(new_or_changed_session_dates),
                    "recorded_counts": recorded_counts,
                    "computed_counts": computed_counts,
                    "recorded_counts_match": counts_match,
                    "content_differences": changed,
                    "curated_60m_review_status": (
                        "PASS" if curated_sample_quality_pass else "BLOCKED_REVIEW"
                    ),
                    "curated_60m_apply_recommendation": (
                        "ELIGIBLE_FOR_GUARDED_BOUNDED_APPLY"
                        if curated_sample_quality_pass else "DO_NOT_APPLY"
                    ),
                    "c2_daily_derived_review_status": (
                        "PASS" if daily_derived_quality_pass else "BLOCKED_INCOMPLETE_SESSION"
                    ),
                    "apply_recommendation": (
                        "ELIGIBLE_FOR_GUARDED_BOUNDED_APPLY"
                        if overall_apply_ready else "DO_NOT_APPLY_YET"
                    ),
                    "evidence_relative_paths": {
                        "manifest": str(manifest_path.relative_to(project_root())),
                        "revision_detection": detection_relative,
                        "chart": chart_relative,
                    },
                })

    curated_eligible = [
        item for item in reviewed
        if item["curated_60m_apply_recommendation"] == "ELIGIBLE_FOR_GUARDED_BOUNDED_APPLY"
    ]
    eligible = [
        item for item in reviewed
        if item["apply_recommendation"] == "ELIGIBLE_FOR_GUARDED_BOUNDED_APPLY"
    ]
    return {
        "review_id": "c2_etf11_dataversion_revision_readonly_recheck_20260801",
        "generated_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "scope": {"instrument_keys": list(ETF11), "horizon_minutes": 60, "price_basis": "native_ohlc"},
        "database_transaction_read_only": str(transaction_read_only),
        "saxo_get_requests": 0,
        "supporting_probe_saxo_get_requests": (
            0 if gap_probe is None else gap_probe["saxo_get_request_count"]
        ),
        "database_writes": 0,
        "orders_or_prechecks_sent": 0,
        "usdjpy_touched": False,
        "curated_60m_eligible_count": len(curated_eligible),
        "eligible_count": len(eligible),
        "blocked_count": len(reviewed) - len(eligible),
        "series": reviewed,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gap-probe", type=Path)
    args = parser.parse_args()
    print(json.dumps(
        review(gap_probe_path=args.gap_probe),
        ensure_ascii=False,
        sort_keys=True,
        default=_json_value,
    ))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
