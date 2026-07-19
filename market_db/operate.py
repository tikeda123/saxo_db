"""Procedure-only operational changes through the least-privileged role."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from dataclasses import dataclass
from typing import Iterable

from .connection import MARKET_DB, connect


@dataclass(frozen=True)
class LegacyReview:
    event_id: int
    rule_id: str
    failed_run_id: int
    scope_kind: str
    source_dataset_id: str | None
    affected_layer: str
    price_basis: str | None
    applicability: str
    superseded_by_run_id: int | None


LEGACY_DMI1_REVIEWS = (
    LegacyReview(13, "source_series_quality_gate", 21, "SERIES", "saxo_etf_daily_raw_20260712T132427Z", "raw", "native_ohlc", "CURRENT", None),
    LegacyReview(14, "source_series_quality_gate", 21, "SERIES", "saxo_etf_daily_raw_20260712T132427Z", "raw", "native_ohlc", "CURRENT", None),
    LegacyReview(15, "source_series_quality_gate", 21, "SERIES", "saxo_etf_daily_raw_20260712T132427Z", "raw", "native_ohlc", "CURRENT", None),
    LegacyReview(16, "source_series_quality_gate", 43, "SERIES", "v12shortterm_saxo_sim_intraday_raw_v1", "raw", "native_ohlc", "CURRENT", None),
    LegacyReview(17, "source_series_quality_gate", 43, "SERIES", "v12shortterm_saxo_sim_intraday_raw_v1", "raw", "native_ohlc", "CURRENT", None),
    LegacyReview(32, "db3_atomic_run_gate", 72, "RUN", None, "curated", None, "HISTORICAL", 104),
    LegacyReview(33, "db3_atomic_run_gate", 73, "RUN", None, "curated", "native_ohlc", "HISTORICAL", 74),
    LegacyReview(28213, "db3_atomic_run_gate", 76, "RUN", None, "curated", "native_ohlc", "HISTORICAL", 77),
    LegacyReview(56385, "db3_atomic_run_gate", 78, "RUN", None, "curated", "native_ohlc", "HISTORICAL", 79),
    LegacyReview(84557, "db3_atomic_run_gate", 80, "RUN", None, "curated", "native_ohlc", "HISTORICAL", 82),
    LegacyReview(84558, "db3_atomic_run_gate", 81, "RUN", None, "curated", None, "HISTORICAL", 104),
    LegacyReview(112730, "db3_atomic_run_gate", 83, "RUN", None, "curated", "native_ohlc", "HISTORICAL", 84),
    LegacyReview(140908, "db3_atomic_run_gate", 85, "RUN", None, "curated", "native_ohlc", "HISTORICAL", 86),
    LegacyReview(156595, "db3_atomic_run_gate", 87, "RUN", None, "curated", "native_ohlc", "HISTORICAL", 88),
    LegacyReview(172282, "db3_atomic_run_gate", 89, "RUN", None, "curated", "native_ohlc", "HISTORICAL", 90),
    LegacyReview(190609, "db3_atomic_run_gate", 91, "RUN", None, "curated", "native_ohlc", "HISTORICAL", 92),
    LegacyReview(218786, "db3_atomic_run_gate", 93, "RUN", None, "curated", "native_ohlc", "HISTORICAL", 94),
    LegacyReview(246964, "db3_atomic_run_gate", 95, "RUN", None, "curated", "native_ohlc", "HISTORICAL", 96),
    LegacyReview(275135, "db3_atomic_run_gate", 97, "RUN", None, "curated", "bid_ask_mid", "HISTORICAL", 100),
    LegacyReview(275136, "db3_atomic_run_gate", 98, "RUN", None, "curated", "bid_ask_mid", "HISTORICAL", 100),
    LegacyReview(335098, "db3_atomic_run_gate", 101, "RUN", None, "curated", "bid_ask_mid", "HISTORICAL", 103),
    LegacyReview(335099, "db3_atomic_run_gate", 102, "RUN", None, "curated", "bid_ask_mid", "HISTORICAL", 103),
)


def _private_note(prompt: str) -> str:
    if not sys.stdin.isatty():
        value = sys.stdin.readline().strip()
    else:
        value = getpass.getpass(prompt)
    if not value:
        raise ValueError("a non-empty note is required")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run allow-listed operational procedures")
    commands = parser.add_subparsers(dest="command", required=True)

    for name in ("acknowledge-quality", "resolve-quality"):
        subparser = commands.add_parser(name)
        subparser.add_argument("quality_event_id", type=int)
        subparser.add_argument("--operator", required=True)

    scope = commands.add_parser("record-quality-scope")
    scope.add_argument("quality_event_id", type=int)
    scope.add_argument(
        "--scope-kind",
        required=True,
        choices=("INSTRUMENT", "SERIES", "DATASET", "RUN", "LAYER", "GLOBAL", "UNKNOWN"),
    )
    scope.add_argument("--source-dataset-id")
    scope.add_argument(
        "--affected-layer", choices=("raw", "curated", "derived", "research_metadata")
    )
    scope.add_argument("--price-basis")
    scope.add_argument("--operator", required=True)

    review = commands.add_parser("review-quality")
    review.add_argument("quality_event_id", type=int)
    review.add_argument(
        "--applicability", required=True, choices=("CURRENT", "HISTORICAL", "UNKNOWN")
    )
    review.add_argument("--superseded-by-run-id", type=int)
    review.add_argument("--operator", required=True)

    reconcile = commands.add_parser("reconcile-dmi1-legacy")
    reconcile.add_argument("--operator", required=True)
    reconcile.add_argument("--apply", action="store_true")

    start = commands.add_parser("start-backup")
    start.add_argument("database_name", choices=("saxo_market", "saxo_research_v13", "saxo_forward_v13"))
    start.add_argument("relative_path")

    finish = commands.add_parser("finish-backup")
    finish.add_argument("backup_run_id", type=int)
    finish.add_argument("status", choices=("PASS", "FAILED", "BLOCKED"))
    finish.add_argument("--sha256")
    finish.add_argument("--size-bytes", type=int)
    finish.add_argument("--pg-restore-list-pass", action="store_true")
    finish.add_argument("--error-code")
    return parser


def _run_legacy_reconciliation(args: argparse.Namespace) -> dict[str, object]:
    event_ids = [item.event_id for item in LEGACY_DMI1_REVIEWS]
    recovery_ids = sorted({
        item.superseded_by_run_id for item in LEGACY_DMI1_REVIEWS
        if item.superseded_by_run_id is not None
    })
    with connect("saxo_app_reader", MARKET_DB, application_name="saxo_db_dmi1_reconcile_check") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                "SELECT quality_event_id,rule_id,ingestion_run_id,scope_kind,source_dataset_id,"
                "affected_layer,price_basis,applicability,superseded_by_ingestion_run_id "
                "FROM quality.v_event_status WHERE quality_event_id = ANY(%s)",
                (event_ids,),
            )
            events = {int(row[0]): row for row in cursor.fetchall()}
            cursor.execute(
                "SELECT ingestion_run_id,status FROM ops.v_ingestion_status "
                "WHERE ingestion_run_id = ANY(%s)",
                (recovery_ids,),
            )
            recovery = {int(row[0]): str(row[1]) for row in cursor.fetchall()}
            cursor.execute(
                "SELECT COUNT(*), COUNT(*) FILTER (WHERE last_ingestion_run_id=105 AND data_status='ACTIVE') "
                "FROM analytics.v_data_freshness WHERE horizon_minutes=60"
            )
            series_count, final_run_count = (int(value) for value in cursor.fetchone())

    if set(events) != set(event_ids):
        raise RuntimeError("DMI1_LEGACY_EVENT_SET_MISMATCH")
    if any(recovery.get(run_id) != "PASS" for run_id in recovery_ids):
        raise RuntimeError("DMI1_RECOVERY_RUN_NOT_PASS")
    if series_count != 13 or final_run_count != 13:
        raise RuntimeError("DMI1_CURRENT_WATERMARK_EVIDENCE_MISMATCH")

    for item in LEGACY_DMI1_REVIEWS:
        row = events[item.event_id]
        if str(row[1]) != item.rule_id or int(row[2]) != item.failed_run_id:
            raise RuntimeError(f"DMI1_EVENT_EVIDENCE_MISMATCH:{item.event_id}")
        current_scope = str(row[3])
        expected_scope = (
            item.scope_kind, item.source_dataset_id, item.affected_layer, item.price_basis
        )
        actual_scope = (current_scope, row[4], row[5], row[6])
        correctable_global_scope = (
            item.event_id in {32, 84558}
            and actual_scope == ("RUN", None, "curated", "native_ohlc")
            and expected_scope == ("RUN", None, "curated", None)
        )
        if current_scope != "UNKNOWN" and actual_scope != expected_scope and not correctable_global_scope:
            raise RuntimeError(f"DMI1_SCOPE_CONFLICT:{item.event_id}")
        current_applicability = str(row[7])
        if current_applicability != "UNKNOWN" and (
            current_applicability != item.applicability
            or row[8] != item.superseded_by_run_id
        ):
            raise RuntimeError(f"DMI1_APPLICABILITY_CONFLICT:{item.event_id}")

    if not args.apply:
        return {
            "command": args.command,
            "status": "PLAN_VALID",
            "event_count": len(LEGACY_DMI1_REVIEWS),
            "current_count": sum(item.applicability == "CURRENT" for item in LEGACY_DMI1_REVIEWS),
            "historical_count": sum(item.applicability == "HISTORICAL" for item in LEGACY_DMI1_REVIEWS),
            "database_writes": 0,
        }

    scoped = reviewed = 0
    with connect("saxo_ops_operator", MARKET_DB, application_name="saxo_db_dmi1_reconcile_apply") as conn:
        with conn.cursor() as cursor:
            for item in LEGACY_DMI1_REVIEWS:
                row = events[item.event_id]
                actual_scope = (str(row[3]), row[4], row[5], row[6])
                expected_scope = (
                    item.scope_kind, item.source_dataset_id, item.affected_layer, item.price_basis
                )
                if actual_scope != expected_scope:
                    evidence = {
                        "failed_run_id": item.failed_run_id,
                        "recovery_run_id": item.superseded_by_run_id,
                        "review_basis": "DMI1B event-by-event evidence review",
                    }
                    cursor.execute(
                        "CALL quality.record_event_scope(%s,%s,%s,%s,%s,%s::jsonb,%s)",
                        (
                            item.event_id, item.scope_kind, item.source_dataset_id,
                            item.affected_layer, item.price_basis,
                            json.dumps(evidence, sort_keys=True), args.operator,
                        ),
                    )
                    scoped += 1
                if str(row[7]) == "UNKNOWN":
                    reason = (
                        "immutable legacy raw archive quality failure remains CURRENT; "
                        "its scope does not include canonical 1h"
                        if item.applicability == "CURRENT"
                        else (
                            f"failed run {item.failed_run_id} was superseded by PASS run "
                            f"{item.superseded_by_run_id}; current 13-series watermarks point to normal PASS run 105"
                        )
                    )
                    cursor.execute(
                        "CALL quality.review_event_applicability(%s,%s,%s,%s,%s)",
                        (
                            item.event_id, item.applicability, reason,
                            item.superseded_by_run_id, args.operator,
                        ),
                    )
                    reviewed += 1
        conn.commit()
    return {
        "command": args.command,
        "status": "PASS",
        "event_count": len(LEGACY_DMI1_REVIEWS),
        "scopes_appended": scoped,
        "reviews_appended": reviewed,
        "orders_or_prechecks_sent": 0,
    }


def run(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "reconcile-dmi1-legacy":
        return _run_legacy_reconciliation(args)
    with connect("saxo_ops_operator", MARKET_DB, application_name=f"saxo_db_operate_{args.command}") as conn:
        with conn.cursor() as cursor:
            if args.command in {"acknowledge-quality", "resolve-quality"}:
                note = _private_note("resolution note: ")
                procedure = "acknowledge_event" if args.command == "acknowledge-quality" else "resolve_event"
                cursor.execute(
                    f"CALL quality.{procedure}(%s, %s, %s)",
                    (args.quality_event_id, args.operator, note),
                )
                result: dict[str, object] = {
                    "command": args.command,
                    "quality_event_id": args.quality_event_id,
                    "status": "completed",
                }
            elif args.command == "record-quality-scope":
                note = _private_note("scope evidence note: ")
                cursor.execute(
                    "CALL quality.record_event_scope(%s, %s, %s, %s, %s, %s::jsonb, %s)",
                    (
                        args.quality_event_id,
                        args.scope_kind,
                        args.source_dataset_id,
                        args.affected_layer,
                        args.price_basis,
                        json.dumps({"note": note}, ensure_ascii=False),
                        args.operator,
                    ),
                )
                result = {
                    "command": args.command,
                    "quality_event_id": args.quality_event_id,
                    "scope_kind": args.scope_kind,
                    "status": "completed",
                }
            elif args.command == "review-quality":
                reason = _private_note("applicability reason: ")
                cursor.execute(
                    "CALL quality.review_event_applicability(%s, %s, %s, %s, %s)",
                    (
                        args.quality_event_id,
                        args.applicability,
                        reason,
                        args.superseded_by_run_id,
                        args.operator,
                    ),
                )
                result = {
                    "applicability": args.applicability,
                    "command": args.command,
                    "quality_event_id": args.quality_event_id,
                    "status": "completed",
                }
            elif args.command == "start-backup":
                cursor.execute(
                    "CALL ops.start_backup_run(%s, %s, NULL)",
                    (args.database_name, args.relative_path),
                )
                result = {
                    "backup_run_id": int(cursor.fetchone()[0]),
                    "command": args.command,
                    "status": "RUNNING",
                }
            else:
                cursor.execute(
                    "CALL ops.finish_backup_run(%s, %s, %s, %s, %s, %s)",
                    (
                        args.backup_run_id,
                        args.status,
                        args.sha256,
                        args.size_bytes,
                        args.pg_restore_list_pass,
                        args.error_code,
                    ),
                )
                result = {
                    "backup_run_id": args.backup_run_id,
                    "command": args.command,
                    "status": args.status,
                }
        conn.commit()
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    try:
        result = run(args)
    except (ValueError, RuntimeError, OSError) as exc:
        print(f"operation failed: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        print(f"operation failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
