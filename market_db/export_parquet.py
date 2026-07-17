"""Read-only, bounded DB4 bar export to verified Parquet artifacts."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import duckdb

from .backup import sha256_file, write_json_atomic
from .connection import project_root
from .read_api import DatabaseReader, bar_rows, parse_utc


EXPORT_DIRECTORY = Path("exports/parquet")
MAX_EXPORT_ROWS = 100_000
OUTPUT_NAME = re.compile(r"^[a-z0-9][a-z0-9_.-]{0,127}\.parquet$")


class ExportError(RuntimeError):
    pass


def output_path(name: str) -> Path:
    if not OUTPUT_NAME.fullmatch(name):
        raise ExportError("output must be a simple .parquet filename")
    root = (project_root() / EXPORT_DIRECTORY).resolve()
    selected = (root / name).resolve()
    if selected.parent != root:
        raise ExportError("output is outside the allow-listed export directory")
    return selected


def default_output_name(instrument_key: str, layer: str, start: datetime, end: datetime) -> str:
    lower = start.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    upper = end.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return f"{instrument_key}_{layer}_{lower}_{upper}.parquet"


def export_bars(
    *,
    instrument_key: str,
    layer: str,
    start: datetime,
    end: datetime,
    name: str | None = None,
    reader: DatabaseReader | Any | None = None,
) -> dict[str, Any]:
    selected_key = instrument_key.strip().lower()
    selected_layer = layer.strip().lower()
    selected_name = name or default_output_name(selected_key, selected_layer, start, end)
    destination = output_path(selected_name)
    destination.parent.mkdir(parents=True, exist_ok=True)
    manifest_path = destination.with_suffix(".manifest.json")
    partial = destination.with_suffix(".parquet.partial")
    if destination.exists() or manifest_path.exists() or partial.exists():
        raise ExportError("export artifact already exists")

    selected_reader = reader or DatabaseReader()
    owns_reader = reader is None
    try:
        rows = bar_rows(
            selected_reader,
            instrument_key=selected_key,
            layer=selected_layer,
            start=start,
            end=end,
            limit=MAX_EXPORT_ROWS,
        )
    finally:
        if owns_reader:
            selected_reader.close()
    if len(rows) > MAX_EXPORT_ROWS:
        raise ExportError(f"export exceeds {MAX_EXPORT_ROWS} rows; shorten the period")

    connection = duckdb.connect(":memory:")
    try:
        connection.execute(
            """
            CREATE TABLE bars (
                instrument_key VARCHAR NOT NULL,
                symbol VARCHAR NOT NULL,
                layer VARCHAR NOT NULL,
                time_utc TIMESTAMPTZ,
                session_date DATE,
                price_basis VARCHAR NOT NULL,
                open DECIMAL(24,12) NOT NULL,
                high DECIMAL(24,12) NOT NULL,
                low DECIMAL(24,12) NOT NULL,
                close DECIMAL(24,12) NOT NULL,
                volume DECIMAL(30,8),
                is_complete BOOLEAN NOT NULL,
                quality_status VARCHAR NOT NULL
            )
            """
        )
        connection.executemany(
            "INSERT INTO bars VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    row["instrument_key"], row["symbol"], row["layer"],
                    row.get("time_utc"), row.get("session_date"), row["price_basis"],
                    row["open"], row["high"], row["low"], row["close"], row.get("volume"),
                    row["is_complete"], row["quality_status"],
                )
                for row in rows
            ],
        )
        connection.execute(
            "COPY bars TO ? (FORMAT PARQUET, COMPRESSION ZSTD)",
            [str(partial)],
        )
        os.replace(partial, destination)
        readback_count = int(
            connection.execute("SELECT COUNT(*) FROM read_parquet(?)", [str(destination)]).fetchone()[0]
        )
    finally:
        connection.close()
        partial.unlink(missing_ok=True)
    if readback_count != len(rows):
        destination.unlink(missing_ok=True)
        raise ExportError("Parquet read-back row count mismatch")

    relative_path = destination.relative_to(project_root()).as_posix()
    payload = {
        "created_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "filters": {
            "end_utc_exclusive": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "instrument_key": selected_key,
            "layer": selected_layer,
            "start_utc_inclusive": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
        },
        "parquet_relative_path": relative_path,
        "parquet_sha256": sha256_file(destination),
        "parquet_size_bytes": destination.stat().st_size,
        "read_role": "saxo_app_reader",
        "readback_row_count": readback_count,
        "row_count": len(rows),
        "status": "PASS",
    }
    write_json_atomic(manifest_path, payload)
    return payload


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export a bounded read-only bar range to Parquet")
    parser.add_argument("--instrument-key", required=True)
    parser.add_argument("--layer", choices=("1h", "4h", "1d"), required=True)
    parser.add_argument("--start", required=True)
    parser.add_argument("--end", required=True)
    parser.add_argument("--output")
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = export_bars(
            instrument_key=args.instrument_key,
            layer=args.layer,
            start=parse_utc(args.start, "start"),
            end=parse_utc(args.end, "end"),
            name=args.output,
        )
    except (ExportError, ValueError, OSError) as exc:
        print(json.dumps({"status": "FAILED", "error_code": str(exc)}, sort_keys=True), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"status": "FAILED", "error_code": type(exc).__name__}, sort_keys=True), file=sys.stderr)
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
