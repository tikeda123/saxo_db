"""Procedure-only operational changes through the least-privileged role."""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from typing import Iterable

from .connection import MARKET_DB, connect


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


def run(args: argparse.Namespace) -> dict[str, object]:
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
