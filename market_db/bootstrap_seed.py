"""Verify and import the repository-owned synthetic clean-Mac smoke seed.

This module never downloads data.  The seed is deliberately artificial and
may only be imported into an otherwise empty database after migration 0018 and
before data-dependent migration 0019.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from psycopg.types.json import Jsonb

from .connection import MARKET_DB, connect, project_root


SEED_DIRECTORY = Path("bootstrap/seed")
SEED_MANIFEST = SEED_DIRECTORY / "manifest.json"
REQUIRED_MIGRATION = "0018"
FIRST_DATA_DEPENDENT_MIGRATION = "0019"
INTRADAY_DATASET_ID = "saxo_db_synthetic_bootstrap_1h_v1"
# 0019 is an immutable migration that validates this legacy dataset identity.
# In the synthetic smoke database the metadata and every row remain explicitly
# marked synthetic, inactive and ineligible for research/operation.
TOTAL_RETURN_DATASET_ID = "20260712T135236Z"
EXPECTED_INSTRUMENT_KEYS = {
    "spy", "iwm", "efa", "eem", "vnq", "shy", "ief", "tlt", "tip", "lqd", "gld"
}
FORBIDDEN_TEXT = re.compile(
    r"(?i)(authorization\s*:\s*bearer|access[_ -]?token|refresh[_ -]?token|"
    r"accountkey|clientkey|BEGIN [A-Z ]*PRIVATE KEY|eyJ[A-Za-z0-9_-]{12,}\.)"
)


class BootstrapSeedError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_csv(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames is None:
            raise BootstrapSeedError(f"missing CSV header: {path.name}")
        return list(reader.fieldnames), list(reader)


def _positive(value: str, *, field: str) -> Decimal:
    try:
        selected = Decimal(value)
    except InvalidOperation as exc:
        raise BootstrapSeedError(f"invalid decimal in {field}") from exc
    if not selected.is_finite() or selected <= 0:
        raise BootstrapSeedError(f"nonpositive decimal in {field}")
    return selected


def _parse_time(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BootstrapSeedError("invalid UTC timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise BootstrapSeedError("timestamp must include UTC offset")
    return parsed


def load_seed(root: Path | None = None) -> dict[str, Any]:
    base = root or project_root()
    manifest_path = base / SEED_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("research_or_operational_eligibility") != "SYNTHETIC_BOOTSTRAP_ONLY":
        raise BootstrapSeedError("seed eligibility marker mismatch")
    if manifest.get("contains_upstream_market_data") is not False:
        raise BootstrapSeedError("seed must declare no upstream market data")

    loaded: dict[str, Any] = {"manifest": manifest, "manifest_path": manifest_path}
    for name, expected in manifest.get("files", {}).items():
        path = base / SEED_DIRECTORY / name
        if not path.is_file():
            raise BootstrapSeedError(f"missing seed file: {name}")
        text = path.read_text(encoding="utf-8")
        if FORBIDDEN_TEXT.search(text):
            raise BootstrapSeedError(f"secret-like content in seed file: {name}")
        headers, rows = _read_csv(path)
        if path.stat().st_size != int(expected["size_bytes"]):
            raise BootstrapSeedError(f"size mismatch: {name}")
        if _sha256(path) != expected["sha256"]:
            raise BootstrapSeedError(f"sha256 mismatch: {name}")
        if len(rows) != int(expected["rows"]):
            raise BootstrapSeedError(f"row count mismatch: {name}")
        loaded[name] = {"headers": headers, "rows": rows, "path": path}
    return loaded


def verify_seed(root: Path | None = None) -> dict[str, Any]:
    loaded = load_seed(root)
    errors: list[str] = []

    instruments = loaded["instruments.csv"]
    bars = loaded["market_bars_1h.csv"]
    total_return = loaded["total_return_daily.csv"]
    if instruments["headers"] != [
        "instrument_key", "symbol", "uic", "asset_type", "category", "currency", "exchange_id"
    ]:
        errors.append("INSTRUMENT_HEADER")
    if bars["headers"] != [
        "instrument_key", "time_utc", "open", "high", "low", "close", "volume"
    ]:
        errors.append("BAR_HEADER")
    if total_return["headers"] != [
        "ticker", "date", "adjusted_close", "total_return_index"
    ]:
        errors.append("TOTAL_RETURN_HEADER")

    instrument_keys = [row["instrument_key"] for row in instruments["rows"]]
    if set(instrument_keys) != EXPECTED_INSTRUMENT_KEYS or len(instrument_keys) != len(set(instrument_keys)):
        errors.append("INSTRUMENT_UNIVERSE")
    uics = [row["uic"] for row in instruments["rows"]]
    if len(uics) != len(set(uics)):
        errors.append("DUPLICATE_UIC")
    if any(":SYNTHETIC" not in row["symbol"] for row in instruments["rows"]):
        errors.append("SYNTHETIC_SYMBOL_MARKER")

    bar_keys: set[tuple[str, str]] = set()
    bar_counts: Counter[str] = Counter()
    for row in bars["rows"]:
        try:
            if row["instrument_key"] not in EXPECTED_INSTRUMENT_KEYS:
                raise BootstrapSeedError("unknown bar instrument")
            _parse_time(row["time_utc"])
            open_value = _positive(row["open"], field="open")
            high = _positive(row["high"], field="high")
            low = _positive(row["low"], field="low")
            close = _positive(row["close"], field="close")
            _positive(row["volume"], field="volume")
            if high < max(open_value, low, close) or low > min(open_value, high, close):
                raise BootstrapSeedError("OHLC relation violation")
            key = (row["instrument_key"], row["time_utc"])
            if key in bar_keys:
                raise BootstrapSeedError("duplicate bar key")
            bar_keys.add(key)
            bar_counts[row["instrument_key"]] += 1
        except BootstrapSeedError:
            errors.append("BAR_DOMAIN_OR_DUPLICATE")
            break
    if set(bar_counts) != EXPECTED_INSTRUMENT_KEYS or set(bar_counts.values()) != {2}:
        errors.append("BAR_COVERAGE")

    tr_keys: set[tuple[str, str]] = set()
    tr_counts: Counter[str] = Counter()
    for row in total_return["rows"]:
        try:
            ticker = row["ticker"]
            if ticker.lower() not in EXPECTED_INSTRUMENT_KEYS:
                raise BootstrapSeedError("unknown total-return instrument")
            datetime.strptime(row["date"], "%Y-%m-%d")
            _positive(row["adjusted_close"], field="adjusted_close")
            _positive(row["total_return_index"], field="total_return_index")
            key = (ticker, row["date"])
            if key in tr_keys:
                raise BootstrapSeedError("duplicate total-return key")
            tr_keys.add(key)
            tr_counts[ticker.lower()] += 1
        except (BootstrapSeedError, ValueError):
            errors.append("TOTAL_RETURN_DOMAIN_OR_DUPLICATE")
            break
    if set(tr_counts) != EXPECTED_INSTRUMENT_KEYS or set(tr_counts.values()) != {2}:
        errors.append("TOTAL_RETURN_COVERAGE")

    manifest = loaded["manifest"]
    return {
        "schema_version": 1,
        "seed_id": manifest["seed_id"],
        "status": "PASS" if not errors else "FAIL",
        "eligibility": "SYNTHETIC_BOOTSTRAP_ONLY",
        "contains_upstream_market_data": False,
        "files": len(manifest["files"]),
        "rows": sum(int(item["rows"]) for item in manifest["files"].values()),
        "size_bytes": sum(int(item["size_bytes"]) for item in manifest["files"].values()),
        "errors": errors,
        "saxo_api_requests": 0,
        "orders_or_prechecks_sent": 0,
    }


def _canonical_sha(row: dict[str, str]) -> str:
    payload = json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _assert_import_boundary(cursor: Any) -> None:
    cursor.execute(
        "SELECT migration_number FROM ops.schema_migration "
        "WHERE target_database=%s ORDER BY migration_number",
        (MARKET_DB,),
    )
    applied = {str(row[0]) for row in cursor.fetchall()}
    if REQUIRED_MIGRATION not in applied:
        raise BootstrapSeedError("BLOCKED_MIGRATION_0018_NOT_APPLIED")
    if FIRST_DATA_DEPENDENT_MIGRATION in applied:
        raise BootstrapSeedError("BLOCKED_MIGRATION_0019_ALREADY_APPLIED")

    relations = (
        "catalog.source_dataset", "catalog.instrument", "ops.source_file",
        "raw.market_bar_revision", "raw.reference_observation",
        "curated.market_bar", "curated.etf_total_return_daily",
    )
    nonempty: list[str] = []
    for relation in relations:
        cursor.execute(f"SELECT COUNT(*) FROM {relation}")
        if int(cursor.fetchone()[0]) != 0:
            nonempty.append(relation)
    if nonempty:
        raise BootstrapSeedError("BLOCKED_NONEMPTY_DATABASE:" + ",".join(nonempty))


def run_import(root: Path | None = None) -> dict[str, Any]:
    verification = verify_seed(root)
    if verification["status"] != "PASS":
        raise BootstrapSeedError("seed verification failed")
    loaded = load_seed(root)
    base = root or project_root()
    manifest_sha = _sha256(base / SEED_MANIFEST)
    retrieved_at = "2020-01-04T00:00:00Z"

    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_synthetic_bootstrap") as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_xact_lock(hashtext('saxo_db_synthetic_bootstrap'))")
                _assert_import_boundary(cursor)
                metadata = Jsonb({
                    "synthetic_fixture": True,
                    "contains_upstream_market_data": False,
                    "must_not_be_used_for_research_or_operation": True,
                    "seed_id": verification["seed_id"],
                })
                cursor.execute(
                    """
                    INSERT INTO catalog.source_dataset (
                        source_dataset_id,dataset_name,provider,environment,dataset_kind,
                        price_basis,canonical_horizon_minutes,authoritative_layer,
                        research_eligibility,active,source_manifest_relative_path,
                        source_manifest_sha256,metadata_json
                    ) VALUES
                    (%s,'SYNTHETIC bootstrap 1H','saxo_db generated fixture','TEST',
                     'raw_market','native_ohlc',60,'curated','SYNTHETIC_BOOTSTRAP_ONLY',FALSE,%s,%s,%s),
                    (%s,'SYNTHETIC bootstrap total return','saxo_db generated fixture','TEST',
                     'total_return','etf_total_return',1440,'curated','SYNTHETIC_BOOTSTRAP_ONLY',FALSE,%s,%s,%s)
                    """,
                    (
                        INTRADAY_DATASET_ID, str(SEED_MANIFEST), manifest_sha, metadata,
                        TOTAL_RETURN_DATASET_ID, str(SEED_MANIFEST), manifest_sha, metadata,
                    ),
                )
                instrument_ids: dict[str, int] = {}
                for row in loaded["instruments.csv"]["rows"]:
                    cursor.execute(
                        """
                        INSERT INTO catalog.instrument (
                            provider,environment,market_key,symbol,uic,asset_type,
                            category,currency,exchange_id,active_from_utc
                        ) VALUES ('saxo_db generated fixture','TEST',%s,%s,%s,%s,%s,%s,%s,
                                  '2020-01-01T00:00:00Z')
                        RETURNING instrument_id
                        """,
                        (
                            row["instrument_key"], row["symbol"], int(row["uic"]),
                            row["asset_type"], row["category"], row["currency"], row["exchange_id"],
                        ),
                    )
                    instrument_ids[row["instrument_key"]] = int(cursor.fetchone()[0])

                cursor.execute(
                    """
                    INSERT INTO ops.ingestion_run (
                        trigger,environment,status,requested_series,source_manifest_sha256
                    ) VALUES ('SYNTHETIC_BOOTSTRAP_SEED','TEST','RUNNING',%s,%s)
                    RETURNING ingestion_run_id
                    """,
                    (Jsonb(sorted(EXPECTED_INSTRUMENT_KEYS)), manifest_sha),
                )
                run_id = int(cursor.fetchone()[0])
                source_ids: dict[str, int] = {}
                for name, dataset_id in (
                    ("instruments.csv", INTRADAY_DATASET_ID),
                    ("market_bars_1h.csv", INTRADAY_DATASET_ID),
                    ("total_return_daily.csv", TOTAL_RETURN_DATASET_ID),
                ):
                    entry = loaded["manifest"]["files"][name]
                    cursor.execute(
                        """
                        INSERT INTO ops.source_file (
                            ingestion_run_id,relative_path,sha256,size_bytes,row_count,source_dataset_id
                        ) VALUES (%s,%s,%s,%s,%s,%s) RETURNING source_file_id
                        """,
                        (
                            run_id, str(SEED_DIRECTORY / name), entry["sha256"],
                            entry["size_bytes"], entry["rows"], dataset_id,
                        ),
                    )
                    source_ids[name] = int(cursor.fetchone()[0])

                for row_number, row in enumerate(loaded["instruments.csv"]["rows"], start=1):
                    cursor.execute(
                        """
                        INSERT INTO raw.reference_observation (
                            source_file_id,row_number,reference_kind,reference_key,layer,
                            observation_time_utc,payload_json,payload_sha256
                        ) VALUES (%s,%s,'SYNTHETIC_INSTRUMENT',%s,'raw',NULL,%s,%s)
                        """,
                        (
                            source_ids["instruments.csv"], row_number, row["instrument_key"],
                            Jsonb(row), _canonical_sha(row),
                        ),
                    )

                for row in loaded["market_bars_1h.csv"]["rows"]:
                    values = (
                        run_id, source_ids["market_bars_1h.csv"],
                        instrument_ids[row["instrument_key"]], row["time_utc"],
                        row["open"], row["high"], row["low"], row["close"], row["volume"],
                        retrieved_at, _canonical_sha(row),
                    )
                    cursor.execute(
                        """
                        INSERT INTO raw.market_bar_revision (
                            ingestion_run_id,source_file_id,instrument_id,horizon_minutes,time_utc,
                            open,high,low,close,volume,price_basis,is_complete,retrieved_at_utc,payload_sha256
                        ) VALUES (%s,%s,%s,60,%s,%s,%s,%s,%s,%s,'native_ohlc',TRUE,%s,%s)
                        """,
                        values,
                    )
                    cursor.execute(
                        """
                        INSERT INTO curated.market_bar (
                            instrument_id,horizon_minutes,time_utc,open,high,low,close,volume,
                            price_basis,is_complete,latest_ingestion_run_id,retrieved_at_utc,quality_status
                        ) VALUES (%s,60,%s,%s,%s,%s,%s,%s,'native_ohlc',TRUE,%s,%s,'NOT_EVALUATED')
                        """,
                        (
                            instrument_ids[row["instrument_key"]], row["time_utc"],
                            row["open"], row["high"], row["low"], row["close"], row["volume"],
                            run_id, retrieved_at,
                        ),
                    )

                for row in loaded["total_return_daily.csv"]["rows"]:
                    cursor.execute(
                        """
                        INSERT INTO curated.etf_total_return_daily (
                            source_dataset_id,ticker,date,currency,adjusted_close,total_return_index,
                            source,quality_status,source_file_id
                        ) VALUES (%s,%s,%s,'USD',%s,%s,'SYNTHETIC_BOOTSTRAP_NOT_MARKET_DATA',
                                  'NOT_EVALUATED',%s)
                        """,
                        (
                            TOTAL_RETURN_DATASET_ID, row["ticker"], row["date"],
                            row["adjusted_close"], row["total_return_index"],
                            source_ids["total_return_daily.csv"],
                        ),
                    )

                cursor.execute(
                    """
                    UPDATE ops.ingestion_run SET status='PASS',finished_at_utc=clock_timestamp(),
                        successful_series=11,inserted_rows=55,rejected_rows=0
                    WHERE ingestion_run_id=%s
                    """,
                    (run_id,),
                )
    return {
        "status": "PASS_SYNTHETIC_BOOTSTRAP_ONLY",
        "seed_id": verification["seed_id"],
        "source_rows": 55,
        "curated_1h_rows": 22,
        "curated_total_return_rows": 22,
        "next_required_action": "APPLY_MIGRATIONS_0019_AND_LATER",
        "saxo_api_requests": 0,
        "orders_or_prechecks_sent": 0,
    }


def import_status() -> dict[str, Any]:
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_synthetic_bootstrap_status") as conn:
        with conn.cursor() as cursor:
            counts: dict[str, int] = {}
            queries = {
                "datasets": (
                    "SELECT COUNT(*) FROM catalog.source_dataset "
                    "WHERE metadata_json->>'synthetic_fixture'='true'"
                ),
                "instruments": (
                    "SELECT COUNT(*) FROM catalog.instrument "
                    "WHERE provider='saxo_db generated fixture' AND environment='TEST'"
                ),
                "raw_1h": (
                    "SELECT COUNT(*) FROM raw.market_bar_revision r JOIN catalog.instrument i "
                    "ON i.instrument_id=r.instrument_id WHERE i.environment='TEST'"
                ),
                "curated_1h": (
                    "SELECT COUNT(*) FROM curated.market_bar b JOIN catalog.instrument i "
                    "ON i.instrument_id=b.instrument_id WHERE i.environment='TEST'"
                ),
                "total_return": (
                    "SELECT COUNT(*) FROM curated.etf_total_return_daily "
                    "WHERE source='SYNTHETIC_BOOTSTRAP_NOT_MARKET_DATA'"
                ),
            }
            for key, statement in queries.items():
                cursor.execute(statement)
                counts[key] = int(cursor.fetchone()[0])
    complete = counts == {
        "datasets": 2, "instruments": 11, "raw_1h": 22,
        "curated_1h": 22, "total_return": 22,
    }
    return {
        "status": "PASS_SYNTHETIC_BOOTSTRAP_ONLY" if complete else "BLOCKED_INCOMPLETE_SEED",
        "eligibility": "SYNTHETIC_BOOTSTRAP_ONLY",
        "counts": counts,
        "saxo_api_requests": 0,
        "orders_or_prechecks_sent": 0,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the synthetic clean-Mac CSV smoke seed")
    parser.add_argument("command", choices=("verify", "import", "status"), nargs="?", default="verify")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "verify":
        result = verify_seed()
    elif args.command == "import":
        result = run_import()
    else:
        result = import_status()
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if str(result["status"]).startswith("PASS") else 2


if __name__ == "__main__":
    raise SystemExit(main())
