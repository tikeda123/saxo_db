"""Idempotent DB2 import of the immutable 69-file legacy bundle."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator

from psycopg.types.json import Jsonb

from .connection import MARKET_DB, connect, project_root


INVENTORY_PATH = Path("manifests/import_file_inventory.csv")
EXPECTED_FILES = 69
EXPECTED_ROWS = 781_808
EXPECTED_SIZE_BYTES = 160_403_659


@dataclass(frozen=True)
class InventoryRecord:
    group: str
    relative_path: str
    row_count: int
    size_bytes: int
    sha256: str

    @property
    def path(self) -> Path:
        return project_root() / self.relative_path


@dataclass(frozen=True)
class DatasetDefinition:
    source_dataset_id: str
    dataset_name: str
    provider: str
    dataset_kind: str
    price_basis: str
    canonical_horizon_minutes: int | None
    authoritative_layer: str
    research_eligibility: str
    manifest_relative_path: str


DATASETS = {
    "saxo_intraday": DatasetDefinition(
        "v12shortterm_saxo_sim_intraday_raw_v1",
        "Saxo SIM intraday 60m and raw 240m archive",
        "Saxo OpenAPI",
        "raw_market",
        "native_ohlc",
        60,
        "raw",
        "development_cutoff_only_completed_60m",
        "manifests/dataset_manifest.json",
    ),
    "saxo_multi_asset_daily": DatasetDefinition(
        "saxo_multi_asset_daily_20260711T142448Z",
        "Saxo SIM multi-asset legacy daily reference",
        "Saxo OpenAPI",
        "raw_market",
        "native_ohlc",
        1440,
        "raw",
        "legacy_reference_only",
        "manifests/import_file_inventory.csv",
    ),
    "saxo_ETF_daily_raw": DatasetDefinition(
        "saxo_etf_daily_raw_20260712T132427Z",
        "Saxo SIM ETF raw daily reference",
        "Saxo OpenAPI",
        "raw_market",
        "native_ohlc",
        1440,
        "raw",
        "legacy_reference_only",
        "manifests/saxo_etf_daily_dataset_metadata.json",
    ),
    "ETF11_external_sources": DatasetDefinition(
        "etf11_external_20260712T135236Z",
        "ETF11 external total-return and macro source rows",
        "Yahoo Finance and FRED",
        "external_reference",
        "external_total_return_and_macro",
        None,
        "raw",
        "source_reference_only",
        "manifests/etf11_source_dataset_manifest.json",
    ),
    "ETF11_curated_total_return": DatasetDefinition(
        "20260712T135236Z",
        "ETF11 curated total-return daily",
        "Yahoo Finance and FRED",
        "total_return",
        "etf_total_return",
        1440,
        "curated",
        "development_cutoff_only",
        "manifests/etf11_source_dataset_manifest.json",
    ),
    "RA0_analysis_baseline": DatasetDefinition(
        "v13_ra0_20260716_v1",
        "V13 RA0 analysis reproduction baseline",
        "saxo_api research artifact",
        "analysis_baseline",
        "research_metadata",
        None,
        "research_metadata",
        "reproduction_only_not_profitability_proof",
        "manifests/ra0_analysis_manifest.json",
    ),
}


CATEGORY_BY_MARKET = {
    "eem": "equity_reit",
    "efa": "equity_reit",
    "iwm": "equity_reit",
    "spy": "equity_reit",
    "us500": "equity_reit",
    "vnq": "equity_reit",
    "gld": "gold",
    "gold": "gold",
    "ief": "bond_credit",
    "lqd": "bond_credit",
    "shy": "bond_credit",
    "tip": "bond_credit",
    "tlt": "bond_credit",
    "us_treasury": "bond_credit",
    "eurusd": "fx",
    "usdjpy": "fx",
    "icom": "commodity",
    "wti": "commodity",
}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def load_inventory(path: Path | None = None) -> list[InventoryRecord]:
    selected = path or project_root() / INVENTORY_PATH
    records: list[InventoryRecord] = []
    with selected.open(newline="", encoding="utf-8") as stream:
        for row in csv.DictReader(stream):
            records.append(
                InventoryRecord(
                    group=row["group"],
                    relative_path=row["target_relative_path"],
                    row_count=int(row["row_count"]),
                    size_bytes=int(row["size_bytes"]),
                    sha256=row["copied_sha256"],
                )
            )
    return records


def classify(record: InventoryRecord) -> str:
    path = Path(record.relative_path)
    if record.group == "ETF11_curated_total_return":
        return "curated_total_return"
    if record.group == "saxo_intraday" and "normalized" in path.parts:
        return "raw_market_bar"
    if record.group in {"saxo_multi_asset_daily", "saxo_ETF_daily_raw"} and path.name.endswith("_daily.csv"):
        return "raw_market_bar"
    return "reference_observation"


def verify_inventory(records: list[InventoryRecord]) -> dict[str, Any]:
    errors: list[str] = []
    class_files: dict[str, int] = {}
    class_rows: dict[str, int] = {}
    for record in records:
        selected_class = classify(record)
        class_files[selected_class] = class_files.get(selected_class, 0) + 1
        class_rows[selected_class] = class_rows.get(selected_class, 0) + record.row_count
        if not record.path.is_file():
            errors.append(f"missing:{record.relative_path}")
            continue
        if record.path.stat().st_size != record.size_bytes:
            errors.append(f"size:{record.relative_path}")
        if sha256_file(record.path) != record.sha256:
            errors.append(f"sha256:{record.relative_path}")
    total_rows = sum(record.row_count for record in records)
    total_size = sum(record.size_bytes for record in records)
    if len(records) != EXPECTED_FILES:
        errors.append("inventory_file_count")
    if total_rows != EXPECTED_ROWS:
        errors.append("inventory_row_count")
    if total_size != EXPECTED_SIZE_BYTES:
        errors.append("inventory_size_bytes")
    return {
        "class_files": class_files,
        "class_rows": class_rows,
        "errors": errors,
        "files": len(records),
        "rows": total_rows,
        "size_bytes": total_size,
        "status": "PASS" if not errors else "FAIL",
    }


def iter_csv(record: InventoryRecord) -> Iterator[dict[str, str]]:
    with record.path.open(newline="", encoding="utf-8-sig") as stream:
        yield from csv.DictReader(stream)


def optional(value: str | None) -> str | None:
    return None if value is None or value == "" else value


def parse_bool(value: str) -> bool:
    lowered = value.strip().lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    raise ValueError(f"invalid boolean value: {value!r}")


def canonical_payload(row: dict[str, str]) -> tuple[dict[str, str], str]:
    payload = dict(row)
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return payload, hashlib.sha256(encoded).hexdigest()


def reference_key(row: dict[str, str]) -> str | None:
    for key in ("market_key", "ticker", "source_series", "category", "pair"):
        if row.get(key):
            return row[key]
    return None


def observation_time(row: dict[str, str]) -> str | None:
    if row.get("time_utc"):
        return row["time_utc"]
    if row.get("date"):
        return f"{row['date']}T00:00:00Z"
    return None


def _manifest_sha(definition: DatasetDefinition) -> str:
    return sha256_file(project_root() / definition.manifest_relative_path)


def _ensure_datasets(cursor: Any) -> None:
    for group, definition in DATASETS.items():
        cursor.execute(
            """
            INSERT INTO catalog.source_dataset (
                source_dataset_id, dataset_name, provider, environment, dataset_kind,
                price_basis, canonical_horizon_minutes, expected_update_interval_seconds,
                freshness_grace_seconds, authoritative_layer, research_eligibility,
                active, source_manifest_relative_path, source_manifest_sha256, metadata_json
            ) VALUES (%s,%s,%s,'SIM',%s,%s,%s,NULL,NULL,%s,%s,TRUE,%s,%s,%s)
            ON CONFLICT (source_dataset_id) DO UPDATE SET
                dataset_name = EXCLUDED.dataset_name,
                source_manifest_relative_path = EXCLUDED.source_manifest_relative_path,
                source_manifest_sha256 = EXCLUDED.source_manifest_sha256,
                metadata_json = EXCLUDED.metadata_json
            """,
            (
                definition.source_dataset_id,
                definition.dataset_name,
                definition.provider,
                definition.dataset_kind,
                definition.price_basis,
                definition.canonical_horizon_minutes,
                definition.authoritative_layer,
                definition.research_eligibility,
                definition.manifest_relative_path,
                _manifest_sha(definition),
                Jsonb({"import_bundle_group": group, "phase": "DB2"}),
            ),
        )


def collect_instruments(records: list[InventoryRecord]) -> list[dict[str, Any]]:
    instruments: dict[tuple[int, str], dict[str, Any]] = {}
    for record in records:
        if classify(record) != "raw_market_bar":
            continue
        for row in iter_csv(record):
            key = (int(row["uic"]), row["asset_type"])
            active_from = row["time_utc"]
            category = row.get("category") or CATEGORY_BY_MARKET.get(row["market_key"], "legacy_reference")
            existing = instruments.get(key)
            candidate = {
                "provider": "Saxo OpenAPI",
                "environment": "SIM",
                "market_key": row["market_key"],
                "symbol": row["symbol"],
                "uic": key[0],
                "asset_type": key[1],
                "category": category,
                "currency": row["currency"],
                "exchange_id": optional(row.get("exchange_id")),
                "active_from_utc": active_from,
            }
            if existing is None:
                instruments[key] = candidate
            else:
                existing["active_from_utc"] = min(existing["active_from_utc"], active_from)
                if existing["category"] == "legacy_reference" and category != "legacy_reference":
                    existing["category"] = category
    return list(instruments.values())


def _ensure_instruments(cursor: Any, instruments: list[dict[str, Any]]) -> dict[tuple[int, str], int]:
    for item in instruments:
        cursor.execute(
            """
            INSERT INTO catalog.instrument (
                provider, environment, market_key, symbol, uic, asset_type, category,
                currency, exchange_id, active_from_utc
            ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (provider, environment, uic, asset_type) DO UPDATE SET
                market_key = EXCLUDED.market_key,
                symbol = EXCLUDED.symbol,
                category = EXCLUDED.category,
                currency = EXCLUDED.currency,
                exchange_id = COALESCE(EXCLUDED.exchange_id, catalog.instrument.exchange_id),
                active_from_utc = LEAST(catalog.instrument.active_from_utc, EXCLUDED.active_from_utc)
            """,
            (
                item["provider"], item["environment"], item["market_key"], item["symbol"],
                item["uic"], item["asset_type"], item["category"], item["currency"],
                item["exchange_id"], item["active_from_utc"],
            ),
        )
    cursor.execute(
        "SELECT instrument_id, uic, asset_type FROM catalog.instrument WHERE provider='Saxo OpenAPI' AND environment='SIM'"
    )
    return {(int(uic), str(asset_type)): int(instrument_id) for instrument_id, uic, asset_type in cursor.fetchall()}


RAW_COLUMNS = """
    ingestion_run_id, source_file_id, instrument_id, horizon_minutes, time_utc,
    open, high, low, close, open_bid, high_bid, low_bid, close_bid,
    open_ask, high_ask, low_ask, close_ask, volume, market_trading_state,
    price_basis, is_complete, data_version, delayed_by_minutes, retrieved_at_utc,
    payload_sha256
"""


def _raw_row(
    row: dict[str, str], ingestion_run_id: int, source_file_id: int, instrument_ids: dict[tuple[int, str], int]
) -> tuple[Any, ...]:
    _, payload_sha = canonical_payload(row)
    instrument_id = instrument_ids[(int(row["uic"]), row["asset_type"])]
    return (
        ingestion_run_id, source_file_id, instrument_id,
        int(row.get("horizon_minutes") or 1440), row["time_utc"],
        row["open"], row["high"], row["low"], row["close"],
        optional(row.get("open_bid")), optional(row.get("high_bid")),
        optional(row.get("low_bid")), optional(row.get("close_bid")),
        optional(row.get("open_ask")), optional(row.get("high_ask")),
        optional(row.get("low_ask")), optional(row.get("close_ask")),
        optional(row.get("volume")), optional(row.get("market_trading_state")),
        row["price_basis"], parse_bool(row["is_complete"]),
        optional(row.get("data_version")), optional(row.get("delayed_by_minutes")),
        row["retrieved_at_utc"], payload_sha,
    )


def _copy_raw(cursor: Any, record: InventoryRecord, run_id: int, file_id: int, instruments: dict[tuple[int, str], int]) -> int:
    count = 0
    with cursor.copy(f"COPY raw.market_bar_revision ({RAW_COLUMNS}) FROM STDIN") as copy:
        for row in iter_csv(record):
            copy.write_row(_raw_row(row, run_id, file_id, instruments))
            count += 1
    return count


CURATED_COLUMNS = """
    instrument_id, horizon_minutes, time_utc, open, high, low, close,
    open_bid, high_bid, low_bid, close_bid, open_ask, high_ask, low_ask,
    close_ask, volume, market_trading_state, price_basis, is_complete,
    data_version, latest_ingestion_run_id, retrieved_at_utc, quality_status
"""


def _copy_curated_1h(
    cursor: Any, record: InventoryRecord, run_id: int, instruments: dict[tuple[int, str], int]
) -> int:
    count = 0
    with cursor.copy(f"COPY curated.market_bar ({CURATED_COLUMNS}) FROM STDIN") as copy:
        for row in iter_csv(record):
            horizon = int(row["horizon_minutes"])
            if horizon != 60:
                continue
            complete = parse_bool(row["is_complete"])
            copy.write_row(
                (
                    instruments[(int(row["uic"]), row["asset_type"])], horizon, row["time_utc"],
                    row["open"], row["high"], row["low"], row["close"],
                    optional(row.get("open_bid")), optional(row.get("high_bid")),
                    optional(row.get("low_bid")), optional(row.get("close_bid")),
                    optional(row.get("open_ask")), optional(row.get("high_ask")),
                    optional(row.get("low_ask")), optional(row.get("close_ask")),
                    optional(row.get("volume")), optional(row.get("market_trading_state")),
                    row["price_basis"], complete, optional(row.get("data_version")), run_id,
                    row["retrieved_at_utc"], "PASS" if complete else "NOT_EVALUATED",
                )
            )
            count += 1
    return count


REFERENCE_COLUMNS = """
    source_file_id, row_number, reference_kind, reference_key, layer,
    observation_time_utc, payload_json, payload_sha256
"""


def _copy_reference(cursor: Any, record: InventoryRecord, file_id: int) -> int:
    count = 0
    layer = "research_metadata" if record.group == "RA0_analysis_baseline" else "raw"
    with cursor.copy(f"COPY raw.reference_observation ({REFERENCE_COLUMNS}) FROM STDIN") as copy:
        for count, row in enumerate(iter_csv(record), start=1):
            payload, payload_sha = canonical_payload(row)
            copy.write_row(
                (file_id, count, record.group, reference_key(row), layer, observation_time(row), Jsonb(payload), payload_sha)
            )
    return count


TOTAL_RETURN_COLUMNS = """
    source_dataset_id, ticker, date, currency, open_unadjusted, high_unadjusted,
    low_unadjusted, close_unadjusted, adjusted_close, total_return_index,
    volume, dividend_cash, split_factor, source, quality_status, source_file_id
"""


def _total_return_quality(row: dict[str, str]) -> str:
    failed = (
        parse_bool(row["quality_missing_price"])
        or not parse_bool(row["quality_positive_price"])
        or not parse_bool(row["quality_valid_ohlc"])
        or not parse_bool(row["quality_valid_split"])
    )
    if failed:
        return "FAIL"
    return "WARN" if parse_bool(row["quality_return_outlier"]) else "PASS"


def _copy_total_return(cursor: Any, record: InventoryRecord, file_id: int) -> int:
    count = 0
    expected_dataset = DATASETS[record.group].source_dataset_id
    with cursor.copy(f"COPY curated.etf_total_return_daily ({TOTAL_RETURN_COLUMNS}) FROM STDIN") as copy:
        for row in iter_csv(record):
            if row["source_dataset_id"] != expected_dataset:
                raise ValueError("curated total-return source_dataset_id mismatch")
            copy.write_row(
                (
                    expected_dataset, row["ticker"], row["date"], row["currency"],
                    optional(row["open_unadjusted"]), optional(row["high_unadjusted"]),
                    optional(row["low_unadjusted"]), optional(row["close_unadjusted"]),
                    row["adjusted_close"], row["total_return_index"], optional(row["volume"]),
                    optional(row["dividend_cash"]), optional(row["split_factor"]), row["source"],
                    _total_return_quality(row), file_id,
                )
            )
            count += 1
    return count


def _record_quality_events(
    cursor: Any,
    record: InventoryRecord,
    run_id: int,
    instruments: dict[tuple[int, str], int],
) -> int:
    if Path(record.relative_path).name != "collection_summary.csv" or record.group not in {
        "saxo_intraday", "saxo_ETF_daily_raw"
    }:
        return 0
    count = 0
    for row in iter_csv(record):
        if row.get("quality_status") != "FAIL":
            continue
        instrument_id = instruments.get((int(row["uic"]), row["asset_type"]))
        observed = {
            key: row.get(key)
            for key in (
                "row_count", "completed_row_count", "duplicate_count", "missing_ohlc_count",
                "ohlc_violation_count", "nonpositive_ohlc_count", "fx_bid_ask_missing_count",
                "crossed_bid_ask_count", "quality_status",
            )
            if row.get(key) not in (None, "")
        }
        cursor.execute(
            """
            INSERT INTO quality.event (
                ingestion_run_id, instrument_id, horizon_minutes, rule_id,
                severity, observed_value, action, status
            ) VALUES (%s,%s,%s,'source_series_quality_gate','ERROR',%s,'RAW_ARCHIVE_ONLY_DB2','OPEN')
            """,
            (run_id, instrument_id, int(row.get("horizon_minutes") or 1440), Jsonb(observed)),
        )
        count += 1
    return count


def _existing_source(cursor: Any, record: InventoryRecord) -> tuple[int, str, int] | None:
    cursor.execute(
        "SELECT source_file_id, sha256, row_count FROM ops.source_file WHERE relative_path=%s ORDER BY source_file_id",
        (record.relative_path,),
    )
    rows = cursor.fetchall()
    if not rows:
        return None
    if len(rows) != 1:
        raise RuntimeError(f"multiple source-file registrations: {record.relative_path}")
    file_id, sha256, row_count = rows[0]
    if str(sha256).strip() != record.sha256 or int(row_count) != record.row_count:
        raise RuntimeError(f"immutable source registration mismatch: {record.relative_path}")
    return int(file_id), str(sha256).strip(), int(row_count)


def _import_one(
    conn: Any,
    record: InventoryRecord,
    inventory_sha: str,
    instruments: dict[tuple[int, str], int],
) -> dict[str, Any]:
    with conn.cursor() as cursor:
        existing = _existing_source(cursor, record)
        if existing is not None:
            return {"path": record.relative_path, "rows": record.row_count, "status": "skipped"}

    with conn.transaction():
        with conn.cursor() as cursor:
            definition = DATASETS[record.group]
            cursor.execute(
                """
                INSERT INTO ops.ingestion_run (
                    trigger, environment, status, requested_series, source_manifest_sha256
                ) VALUES ('DB2_LEGACY_IMPORT','SIM','RUNNING',%s,%s)
                RETURNING ingestion_run_id
                """,
                (Jsonb([record.relative_path]), inventory_sha),
            )
            run_id = int(cursor.fetchone()[0])
            cursor.execute(
                """
                INSERT INTO ops.source_file (
                    ingestion_run_id, relative_path, sha256, size_bytes, row_count, source_dataset_id
                ) VALUES (%s,%s,%s,%s,%s,%s)
                RETURNING source_file_id
                """,
                (
                    run_id, record.relative_path, record.sha256, record.size_bytes,
                    record.row_count, definition.source_dataset_id,
                ),
            )
            file_id = int(cursor.fetchone()[0])
            selected_class = classify(record)
            curated_rows = 0
            if selected_class == "raw_market_bar":
                inserted = _copy_raw(cursor, record, run_id, file_id, instruments)
                if "normalized" in Path(record.relative_path).parts:
                    curated_rows = _copy_curated_1h(cursor, record, run_id, instruments)
            elif selected_class == "curated_total_return":
                inserted = _copy_total_return(cursor, record, file_id)
                curated_rows = inserted
            else:
                inserted = _copy_reference(cursor, record, file_id)
            if inserted != record.row_count:
                raise RuntimeError(f"row-count mismatch while importing {record.relative_path}")
            quality_events = _record_quality_events(cursor, record, run_id, instruments)
            cursor.execute(
                """
                UPDATE ops.ingestion_run SET
                    status='PASS', finished_at_utc=clock_timestamp(), successful_series=1,
                    inserted_rows=%s, rejected_rows=0
                WHERE ingestion_run_id=%s
                """,
                (inserted, run_id),
            )
    return {
        "class": classify(record), "curated_rows": curated_rows, "path": record.relative_path,
        "quality_events": quality_events, "rows": record.row_count, "status": "imported",
    }


def import_status() -> dict[str, int]:
    relations = (
        "catalog.source_dataset", "catalog.instrument", "ops.ingestion_run", "ops.source_file",
        "raw.market_bar_revision", "raw.reference_observation", "curated.market_bar",
        "curated.etf_total_return_daily", "quality.event",
    )
    result: dict[str, int] = {}
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_db2_status") as conn:
        with conn.cursor() as cursor:
            for relation in relations:
                cursor.execute(f"SELECT COUNT(*) FROM {relation}")
                result[relation] = int(cursor.fetchone()[0])
    return result


def run_import() -> dict[str, Any]:
    records = load_inventory()
    verification = verify_inventory(records)
    if verification["status"] != "PASS":
        raise RuntimeError("source inventory verification failed")
    inventory_sha = sha256_file(project_root() / INVENTORY_PATH)
    instrument_candidates = collect_instruments(records)
    results: list[dict[str, Any]] = []
    with connect(
        "saxo_ingest", MARKET_DB, autocommit=True, application_name="saxo_db_db2_import"
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SELECT pg_advisory_lock(hashtext('saxo_db_db2_legacy_import'))")
        try:
            with conn.transaction():
                with conn.cursor() as cursor:
                    _ensure_datasets(cursor)
                    instruments = _ensure_instruments(cursor, instrument_candidates)
            for record in records:
                results.append(_import_one(conn, record, inventory_sha, instruments))
        finally:
            with conn.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(hashtext('saxo_db_db2_legacy_import'))")
    return {
        "imported_files": sum(item["status"] == "imported" for item in results),
        "imported_source_rows": sum(item["rows"] for item in results if item["status"] == "imported"),
        "quality_events_created": sum(item.get("quality_events", 0) for item in results),
        "skipped_files": sum(item["status"] == "skipped" for item in results),
        "status": import_status(),
        "verification": verification,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import the immutable DB2 legacy bundle")
    parser.add_argument("command", choices=("verify", "import", "status"), nargs="?", default="verify")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "verify":
        result: Any = verify_inventory(load_inventory())
    elif args.command == "status":
        result = import_status()
    else:
        result = run_import()
    print(json.dumps(result, sort_keys=True))
    return 0 if not isinstance(result, dict) or result.get("status") != "FAIL" else 1


if __name__ == "__main__":
    raise SystemExit(main())
