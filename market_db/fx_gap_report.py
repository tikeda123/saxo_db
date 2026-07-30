"""Account for every missing canonical FX 1H slot without synthesizing prices."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from collections import Counter
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from .connection import MARKET_DB, connect, project_root


FX_KEYS = ("eurusd", "usdjpy")
FX_PRICE_BASIS = "bid_ask_mid"
CALENDAR_ID = "SBFX_24X5"
NY = ZoneInfo("America/New_York")
CAUSE_CODES = {
    "CALENDAR_EXPECTATION_FALSE_POSITIVE",
    "WEEKEND_OR_HOLIDAY_CLOSURE",
    "DAILY_MAINTENANCE_BOUNDARY",
    "SAXO_RAW_NO_SAMPLE",
    "ACQUISITION_RUN_MISSED",
    "RAW_PRESENT_CURATED_REJECTED",
    "QUARANTINED_VALUE_ANOMALY",
    "UNCLASSIFIED",
}

# Frozen audit dates are concentration labels only. They never change a gap's
# cause, quality state, calendar, or price values.
MAJOR_MARKET_MOVE_DATES_V1 = {
    date(2015, 1, 15): "SNB_EURCHF_FLOOR_REMOVAL",
    date(2016, 6, 24): "BREXIT_REFERENDUM_RESULT",
    date(2020, 3, 9): "COVID_MARKET_DISLOCATION",
    date(2020, 3, 12): "COVID_MARKET_DISLOCATION",
    date(2020, 3, 16): "COVID_MARKET_DISLOCATION",
    date(2022, 2, 24): "RUSSIA_UKRAINE_INVASION",
}


GAP_SQL = """
WITH selected AS (
    SELECT i.instrument_id,i.market_key AS instrument_key,i.session_calendar_id,
           MIN(b.time_utc) AS min_time_utc,MAX(b.time_utc) AS max_time_utc
    FROM catalog.instrument i
    JOIN curated.market_bar b ON b.instrument_id=i.instrument_id
    WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
      AND i.asset_type='FxSpot' AND i.market_key=ANY(%s)
      AND b.horizon_minutes=60 AND b.price_basis=%s AND b.is_complete
    GROUP BY i.instrument_id,i.market_key,i.session_calendar_id
), expected AS (
    SELECT s.instrument_id,s.instrument_key,s.min_time_utc,s.max_time_utc,
           slot.time_utc
    FROM selected s
    JOIN catalog.session_interval si
      ON si.session_calendar_id=s.session_calendar_id
     AND si.session_status <> 'HOLIDAY'
    CROSS JOIN LATERAL generate_series(
      date_trunc('hour',si.open_time_utc)+interval '1 hour',
      date_trunc('hour',si.close_time_utc)-interval '2 hours',
      interval '1 hour'
    ) slot(time_utc)
    WHERE slot.time_utc BETWEEN s.min_time_utc AND s.max_time_utc
), missing AS (
    SELECT e.*
    FROM expected e
    LEFT JOIN curated.market_bar complete
      ON complete.instrument_id=e.instrument_id
     AND complete.horizon_minutes=60 AND complete.price_basis=%s
     AND complete.time_utc=e.time_utc AND complete.is_complete
    WHERE complete.instrument_id IS NULL
), raw_bounds AS (
    SELECT r.instrument_id,r.ingestion_run_id,
           run.run_manifest_relative_path,
           MIN(r.time_utc) AS min_time_utc,MAX(r.time_utc) AS max_time_utc
    FROM raw.market_bar_revision r
    JOIN ops.ingestion_run run USING (ingestion_run_id)
    WHERE r.horizon_minutes=60 AND r.price_basis=%s AND run.status='PASS'
    GROUP BY r.instrument_id,r.ingestion_run_id,run.run_manifest_relative_path
)
SELECT m.instrument_key,m.instrument_id,m.time_utc,m.min_time_utc,m.max_time_utc,
       exact_raw.ingestion_run_id IS NOT NULL AS raw_present,
       exact_raw.ingestion_run_id AS raw_sample_run_id,
       exact_raw.relative_path AS raw_artifact_relative_path,
       EXISTS (
         SELECT 1 FROM curated.market_bar c
         WHERE c.instrument_id=m.instrument_id AND c.horizon_minutes=60
           AND c.price_basis=%s AND c.time_utc=m.time_utc
       ) AS curated_rejected,
       quarantine.quality_event_id IS NOT NULL AS quarantined,
       quarantine.quality_event_id AS quarantine_event_id,
       covering.ingestion_run_id IS NOT NULL AS covered_by_successful_raw_run,
       covering.ingestion_run_id AS covering_successful_run_id,
       covering.run_manifest_relative_path AS covering_run_manifest_relative_path
FROM missing m
LEFT JOIN LATERAL (
         SELECT r.ingestion_run_id,sf.relative_path
         FROM raw.market_bar_revision r
         JOIN ops.source_file sf USING (source_file_id)
         WHERE r.instrument_id=m.instrument_id AND r.horizon_minutes=60
           AND r.price_basis=%s AND r.time_utc=m.time_utc
         ORDER BY r.ingestion_run_id DESC LIMIT 1
       ) exact_raw ON TRUE
LEFT JOIN LATERAL (
         SELECT q.quality_event_id FROM quality.event q
         WHERE q.instrument_id=m.instrument_id AND q.horizon_minutes=60
           AND q.time_utc=m.time_utc
           AND q.rule_id='db3_fx_crossed_extrema_quarantine'
         ORDER BY q.quality_event_id DESC LIMIT 1
       ) quarantine ON TRUE
LEFT JOIN LATERAL (
         SELECT rb.ingestion_run_id,rb.run_manifest_relative_path
         FROM raw_bounds rb
         WHERE rb.instrument_id=m.instrument_id
           AND m.time_utc BETWEEN rb.min_time_utc AND rb.max_time_utc
         ORDER BY rb.ingestion_run_id DESC LIMIT 1
       ) covering ON TRUE
ORDER BY m.instrument_key,m.time_utc
"""


def classify_gap(row: Mapping[str, Any]) -> str:
    """Classify from retained evidence; never infer or fill a price."""

    timestamp = row.get("time_utc")
    if not isinstance(timestamp, datetime):
        return "UNCLASSIFIED"
    if bool(row.get("quarantined")):
        return "QUARANTINED_VALUE_ANOMALY"
    raw_run_id = row.get("raw_sample_run_id")
    covering_run_id = row.get("covering_successful_run_id")
    raw_is_current = bool(row.get("raw_present")) and (
        covering_run_id is None
        or raw_run_id is None
        or int(raw_run_id) >= int(covering_run_id)
    )
    if raw_is_current or bool(row.get("curated_rejected")):
        return "RAW_PRESENT_CURATED_REJECTED"
    # An expected FX slot can legitimately begin on Sunday evening New York
    # time. Closure and maintenance codes therefore require explicit calendar
    # evidence and must never be guessed from weekday/hour alone.
    if bool(row.get("calendar_false_positive")):
        return "CALENDAR_EXPECTATION_FALSE_POSITIVE"
    if bool(row.get("closed_session")):
        return "WEEKEND_OR_HOLIDAY_CLOSURE"
    if bool(row.get("maintenance_boundary")):
        return "DAILY_MAINTENANCE_BOUNDARY"
    if bool(row.get("covered_by_successful_raw_run")):
        return "SAXO_RAW_NO_SAMPLE"
    return "ACQUISITION_RUN_MISSED"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_gap(row: Mapping[str, Any]) -> dict[str, Any]:
    timestamp = row["time_utc"]
    if not isinstance(timestamp, datetime):
        raise TypeError("time_utc must be datetime")
    local = timestamp.astimezone(NY)
    cause = classify_gap(row)
    return {
        "instrument_key": str(row["instrument_key"]),
        "time_utc": _iso(timestamp),
        "new_york_local_time": local.isoformat(),
        "year": timestamp.year,
        "month": timestamp.month,
        "weekday_utc": timestamp.strftime("%A"),
        "utc_hour": timestamp.hour,
        "new_york_hour": local.hour,
        "cause_code": cause,
        "raw_present": bool(row.get("raw_present")),
        "raw_sample_run_id": row.get("raw_sample_run_id"),
        "raw_artifact_relative_path": row.get("raw_artifact_relative_path"),
        "raw_sample_superseded_by_run_id": (
            row.get("covering_successful_run_id")
            if row.get("raw_sample_run_id") is not None
            and row.get("covering_successful_run_id") is not None
            and int(row["raw_sample_run_id"]) < int(row["covering_successful_run_id"])
            else None
        ),
        "curated_row_present": bool(row.get("curated_rejected")),
        "quarantine_event_id": row.get("quarantine_event_id"),
        "covered_by_successful_raw_run": bool(row.get("covered_by_successful_raw_run")),
        "covering_successful_run_id": row.get("covering_successful_run_id"),
        "covering_run_manifest_relative_path": row.get("covering_run_manifest_relative_path"),
        "major_market_move_label": MAJOR_MARKET_MOVE_DATES_V1.get(local.date()),
        "owner": (
            "saxo_db" if cause in {"ACQUISITION_RUN_MISSED", "RAW_PRESENT_CURATED_REJECTED", "UNCLASSIFIED"}
            else "Saxo provider/source coverage"
        ),
        "blocking": cause in {
            "ACQUISITION_RUN_MISSED",
            "RAW_PRESENT_CURATED_REJECTED",
            "UNCLASSIFIED",
        },
        "required_evidence": (
            "provider chart response or successful acquisition spanning the slot"
            if cause in {"ACQUISITION_RUN_MISSED", "UNCLASSIFIED"}
            else None
        ),
    }


def _instrument_keys(values: Iterable[str]) -> tuple[str, ...]:
    keys = tuple(str(value).strip().lower() for value in values)
    if not keys or any(not key for key in keys) or len(set(keys)) != len(keys):
        raise ValueError("instrument keys must be a non-empty unique sequence")
    return keys


def build_summary(
    gaps: Iterable[Mapping[str, Any]],
    instrument_keys: Iterable[str] = FX_KEYS,
) -> dict[str, Any]:
    selected_keys = _instrument_keys(instrument_keys)
    selected = [dict(row) for row in gaps]
    times_by_key = {
        key: {str(row["time_utc"]) for row in selected if row["instrument_key"] == key}
        for key in selected_keys
    }
    common = set.intersection(*(times_by_key[key] for key in selected_keys))
    per_instrument: dict[str, Any] = {}
    for key in selected_keys:
        rows = [row for row in selected if row["instrument_key"] == key]
        per_instrument[key] = {
            "missing_rows": len(rows),
            "cause_counts": dict(sorted(Counter(row["cause_code"] for row in rows).items())),
            "by_year": dict(sorted(Counter(str(row["year"]) for row in rows).items())),
            "by_month": dict(sorted(Counter(f"{row['year']:04d}-{row['month']:02d}" for row in rows).items())),
            "by_weekday_utc": dict(sorted(Counter(row["weekday_utc"] for row in rows).items())),
            "by_utc_hour": dict(sorted(Counter(str(row["utc_hour"]) for row in rows).items())),
            "by_new_york_hour": dict(sorted(Counter(str(row["new_york_hour"]) for row in rows).items())),
            "major_market_move_matches": dict(sorted(Counter(
                row["major_market_move_label"] for row in rows if row["major_market_move_label"]
            ).items())),
            "blocking_rows": sum(bool(row["blocking"]) for row in rows),
            "unclassified_rows": sum(row["cause_code"] == "UNCLASSIFIED" for row in rows),
        }
    return {
        "calendar_id": CALENDAR_ID,
        "calendar_contract": "verified_complete_hour_v1",
        "horizon_minutes": 60,
        "price_basis": FX_PRICE_BASIS,
        "instrument_keys": list(selected_keys),
        "per_instrument": per_instrument,
        "cross_instrument": {
            "common_missing_rows": len(common),
            "only_rows": {
                key: len(
                    times_by_key[key]
                    - set().union(
                        *(times_by_key[other] for other in selected_keys if other != key)
                    )
                )
                for key in selected_keys
            },
        },
        "interpolation_performed": False,
        "orders_or_prechecks_sent": 0,
    }


def render_markdown(summary: Mapping[str, Any], detail_sha256: str) -> str:
    lines = [
        "# FX 1H gap classification summary",
        "",
        f"- Calendar: `{summary['calendar_id']}` / `{summary['calendar_contract']}`",
        f"- Price basis: `{summary['price_basis']}`",
        f"- Detail SHA-256: `{detail_sha256}`",
        "- Price interpolation: **not performed**",
        "- Orders / prechecks: **0 / 0**",
        "",
        "| Instrument | Missing | Blocking | Unclassified | Cause counts |",
        "|---|---:|---:|---:|---|",
    ]
    for key in summary["instrument_keys"]:
        item = summary["per_instrument"][key]
        causes = ", ".join(f"{name}={count}" for name, count in item["cause_counts"].items())
        lines.append(
            f"| {key.upper()} | {item['missing_rows']} | {item['blocking_rows']} | "
            f"{item['unclassified_rows']} | {causes} |"
        )
    cross = summary["cross_instrument"]
    lines.extend([
        "",
        "## Coverage reconciliation",
        "",
        *[
            f"- {key.upper()}: classified_missing={summary['per_instrument'][key]['missing_rows']}, "
            f"curated_duplicate_rows={summary['per_instrument'][key]['curated_reconciliation']['duplicate_rows']}, "
            f"curated_incomplete_rows={summary['per_instrument'][key]['curated_reconciliation']['incomplete_rows']}"
            for key in summary["instrument_keys"]
            if "curated_reconciliation" in summary["per_instrument"][key]
        ],
        "",
        "## Cross-instrument overlap",
        "",
        f"- Common: {cross['common_missing_rows']}",
        *[
            f"- {key.upper()} only: {cross['only_rows'][key]}"
            for key in summary["instrument_keys"]
        ],
        "",
        "Historical coverage warnings remain separate from freshness, current content quality, and interface status.",
        "A missing source observation is retained as source coverage evidence and is never synthesized.",
        "",
    ])
    return "\n".join(lines)


def _write_report(output_dir: Path, gaps: list[dict[str, Any]], summary: dict[str, Any]) -> dict[str, Any]:
    root = project_root().resolve()
    selected = output_dir.resolve()
    if selected != root and root not in selected.parents:
        raise ValueError("output directory must be inside the project")
    selected.mkdir(parents=True, exist_ok=True)
    detail_path = selected / "fx_gap_classification.json"
    csv_path = selected / "fx_gap_classification.csv"
    summary_path = selected / "fx_gap_classification_summary.md"
    manifest_path = selected / "fx_gap_classification_manifest.json"
    detail_bytes = (json.dumps(gaps, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    detail_path.write_bytes(detail_bytes)
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(gaps[0]) if gaps else ["instrument_key", "time_utc"])
        writer.writeheader()
        writer.writerows(gaps)
    digest = hashlib.sha256(detail_bytes).hexdigest()
    summary_bytes = render_markdown(summary, digest).encode("utf-8")
    summary_path.write_bytes(summary_bytes)
    csv_bytes = csv_path.read_bytes()
    manifest = {
        "schema_version": 1,
        "status": "PASS_ACCOUNTED" if all(
            item["blocking_rows"] == 0 and item["unclassified_rows"] == 0
            for item in summary["per_instrument"].values()
        ) else "BLOCKED_UNACCOUNTED",
        "source_database": MARKET_DB,
        "calendar_id": summary["calendar_id"],
        "schedule_version": summary["schedule_version"],
        "calendar_contract": summary["calendar_contract"],
        "horizon_minutes": summary["horizon_minutes"],
        "price_basis": summary["price_basis"],
        "generated_at_utc": summary["generated_at_utc"],
        "per_instrument": summary["per_instrument"],
        "cross_instrument": summary["cross_instrument"],
        "interpolation_performed": False,
        "orders_or_prechecks_sent": 0,
        "artifacts": {
            detail_path.name: {"size_bytes": len(detail_bytes), "sha256": digest},
            csv_path.name: {
                "size_bytes": len(csv_bytes),
                "sha256": hashlib.sha256(csv_bytes).hexdigest(),
            },
            summary_path.name: {
                "size_bytes": len(summary_bytes),
                "sha256": hashlib.sha256(summary_bytes).hexdigest(),
            },
        },
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {
        "detail_relative_path": str(detail_path.relative_to(root)),
        "detail_sha256": digest,
        "csv_relative_path": str(csv_path.relative_to(root)),
        "summary_relative_path": str(summary_path.relative_to(root)),
        "manifest_relative_path": str(manifest_path.relative_to(root)),
    }


def generate_report(
    output_dir: Path,
    instrument_keys: Iterable[str] = FX_KEYS,
) -> dict[str, Any]:
    selected_keys = _instrument_keys(instrument_keys)
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_fx_gap_report") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                cursor.execute(
                    "SELECT schedule_version,metadata_json->>'verification_status' AS verification_status "
                    "FROM catalog.session_calendar WHERE session_calendar_id=%s",
                    (CALENDAR_ID,),
                )
                calendar = cursor.fetchone()
                if calendar is None or calendar["verification_status"] != "VERIFIED":
                    raise RuntimeError("FX_CALENDAR_NOT_VERIFIED")
                cursor.execute(
                    """
                    SELECT i.market_key AS instrument_key,
                           COUNT(*)-COUNT(DISTINCT b.time_utc) AS duplicate_rows,
                           COUNT(*) AS actual_rows,
                           COUNT(*) FILTER (WHERE b.is_complete) AS complete_rows,
                           COUNT(*) FILTER (WHERE NOT b.is_complete) AS incomplete_rows
                    FROM curated.market_bar b
                    JOIN catalog.instrument i USING (instrument_id)
                    WHERE i.market_key=ANY(%s) AND b.horizon_minutes=60 AND b.price_basis=%s
                    GROUP BY i.market_key ORDER BY i.market_key
                    """,
                    (list(selected_keys), FX_PRICE_BASIS),
                )
                coverage_rows = cursor.fetchall()
                cursor.execute(
                    GAP_SQL,
                    (list(selected_keys), FX_PRICE_BASIS, FX_PRICE_BASIS, FX_PRICE_BASIS,
                     FX_PRICE_BASIS, FX_PRICE_BASIS),
                )
                rows = cursor.fetchall()
    gaps = [normalize_gap(row) for row in rows]
    summary = build_summary(gaps, selected_keys)
    coverage_by_key = {str(row["instrument_key"]): dict(row) for row in coverage_rows}
    if set(coverage_by_key) != set(selected_keys):
        raise RuntimeError("FX_COVERAGE_COMPONENT_MISSING")
    for key in selected_keys:
        observed = coverage_by_key[key]
        summary["per_instrument"][key]["curated_reconciliation"] = {
            name: observed[name]
            for name in (
                "actual_rows", "complete_rows", "incomplete_rows", "duplicate_rows",
            )
        }
    summary["generated_at_utc"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    summary["schedule_version"] = calendar["schedule_version"]
    artifacts = _write_report(output_dir, gaps, summary)
    status = "PASS" if all(
        item["blocking_rows"] == 0 and item["unclassified_rows"] == 0
        for item in summary["per_instrument"].values()
    ) else "BLOCKED"
    return {"status": status, "summary": summary, "artifacts": artifacts}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Classify canonical FX 1H coverage gaps")
    parser.add_argument(
        "--output-dir",
        default="manifests/fx_gap_classification",
        help="project-relative report directory",
    )
    parser.add_argument(
        "--instrument-key",
        action="append",
        dest="instrument_keys",
        help="explicit FxSpot instrument key; repeat for multiple keys",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = generate_report(
        project_root() / args.output_dir,
        args.instrument_keys or FX_KEYS,
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
