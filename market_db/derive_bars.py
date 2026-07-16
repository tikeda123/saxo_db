"""Calendar-aware 4-hour and daily derivation from accepted completed 1h bars."""

from __future__ import annotations

import argparse
import json
from typing import Any, Iterable

from .connection import MARKET_DB, connect


DERIVATION_VERSION = "db3_accepted_1h_calendar_v1"


def rebuild(cursor: Any, *, derivation_version: str = DERIVATION_VERSION) -> dict[str, int]:
    """Rebuild one deterministic version inside the caller's transaction."""

    cursor.execute(
        "DELETE FROM derived.market_bar_4h WHERE derivation_version=%s",
        (derivation_version,),
    )
    deleted_4h = cursor.rowcount
    cursor.execute(
        "DELETE FROM derived.market_bar_1d_risk WHERE derivation_version=%s",
        (derivation_version,),
    )
    deleted_1d = cursor.rowcount

    cursor.execute(
        """
        WITH eligible AS (
            SELECT
                b.instrument_id,
                b.time_utc,
                b.price_basis,
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                b.latest_ingestion_run_id,
                si.open_time_utc,
                si.close_time_utc,
                FLOOR(EXTRACT(EPOCH FROM (b.time_utc - si.open_time_utc)) / 14400)::INTEGER AS bucket_4h,
                CEIL(EXTRACT(EPOCH FROM (si.close_time_utc - si.open_time_utc)) / 3600)::INTEGER AS session_slots,
                c.metadata_json->>'verification_status' AS verification_status
            FROM curated.market_bar b
            JOIN catalog.instrument i ON i.instrument_id=b.instrument_id
            JOIN catalog.session_calendar c ON c.session_calendar_id=i.session_calendar_id
            JOIN catalog.session_interval si
              ON si.session_calendar_id=i.session_calendar_id
             AND si.session_status <> 'HOLIDAY'
             AND b.time_utc >= si.open_time_utc
             AND b.time_utc < si.close_time_utc
            WHERE b.horizon_minutes=60
              AND b.is_complete
              AND b.quality_status='PASS'
              AND MOD(EXTRACT(EPOCH FROM (b.time_utc - si.open_time_utc))::BIGINT, 3600)=0
        ), grouped AS (
            SELECT
                instrument_id,
                price_basis,
                open_time_utc + make_interval(hours => bucket_4h * 4) AS bucket_time_utc,
                (ARRAY_AGG(open ORDER BY time_utc))[1] AS open,
                MAX(high) AS high,
                MIN(low) AS low,
                (ARRAY_AGG(close ORDER BY time_utc DESC))[1] AS close,
                SUM(volume) AS volume,
                COUNT(*)::INTEGER AS actual_slots,
                LEAST(4, session_slots - bucket_4h * 4)::INTEGER AS expected_slots,
                MIN(latest_ingestion_run_id) AS first_run,
                MAX(latest_ingestion_run_id) AS last_run,
                verification_status
            FROM eligible
            GROUP BY instrument_id, price_basis, open_time_utc, bucket_4h,
                     session_slots, verification_status
        )
        INSERT INTO derived.market_bar_4h (
            derivation_version, instrument_id, time_utc, price_basis,
            open, high, low, close, volume, is_complete,
            source_first_ingestion_run_id, source_last_ingestion_run_id,
            quality_status
        )
        SELECT
            %s, instrument_id, bucket_time_utc, price_basis,
            open, high, low, close, volume,
            actual_slots=expected_slots,
            first_run, last_run,
            CASE
                WHEN verification_status <> 'VERIFIED' THEN 'NOT_EVALUATED'
                WHEN actual_slots=expected_slots THEN 'PASS'
                ELSE 'WARN'
            END
        FROM grouped
        """,
        (derivation_version,),
    )
    inserted_4h = cursor.rowcount

    cursor.execute(
        """
        WITH eligible AS (
            SELECT
                b.instrument_id,
                si.session_date,
                b.time_utc,
                b.price_basis,
                b.open,
                b.high,
                b.low,
                b.close,
                b.volume,
                b.latest_ingestion_run_id,
                CEIL(EXTRACT(EPOCH FROM (si.close_time_utc - si.open_time_utc)) / 3600)::INTEGER AS expected_slots,
                c.metadata_json->>'verification_status' AS verification_status
            FROM curated.market_bar b
            JOIN catalog.instrument i ON i.instrument_id=b.instrument_id
            JOIN catalog.session_calendar c ON c.session_calendar_id=i.session_calendar_id
            JOIN catalog.session_interval si
              ON si.session_calendar_id=i.session_calendar_id
             AND si.session_status <> 'HOLIDAY'
             AND b.time_utc >= si.open_time_utc
             AND b.time_utc < si.close_time_utc
            WHERE b.horizon_minutes=60
              AND b.is_complete
              AND b.quality_status='PASS'
              AND MOD(EXTRACT(EPOCH FROM (b.time_utc - si.open_time_utc))::BIGINT, 3600)=0
        ), grouped AS (
            SELECT
                instrument_id,
                session_date,
                price_basis,
                (ARRAY_AGG(open ORDER BY time_utc))[1] AS open,
                MAX(high) AS high,
                MIN(low) AS low,
                (ARRAY_AGG(close ORDER BY time_utc DESC))[1] AS close,
                SUM(volume) AS volume,
                COUNT(*)::INTEGER AS actual_slots,
                expected_slots,
                MIN(latest_ingestion_run_id) AS first_run,
                MAX(latest_ingestion_run_id) AS last_run,
                verification_status
            FROM eligible
            GROUP BY instrument_id, session_date, price_basis,
                     expected_slots, verification_status
        )
        INSERT INTO derived.market_bar_1d_risk (
            derivation_version, instrument_id, session_date, price_basis,
            open, high, low, close, volume, is_complete,
            source_first_ingestion_run_id, source_last_ingestion_run_id,
            quality_status
        )
        SELECT
            %s, instrument_id, session_date, price_basis,
            open, high, low, close, volume,
            actual_slots=expected_slots,
            first_run, last_run,
            CASE
                WHEN verification_status <> 'VERIFIED' THEN 'NOT_EVALUATED'
                WHEN actual_slots=expected_slots THEN 'PASS'
                ELSE 'WARN'
            END
        FROM grouped
        """,
        (derivation_version,),
    )
    inserted_1d = cursor.rowcount
    return {
        "deleted_4h": deleted_4h,
        "deleted_1d": deleted_1d,
        "inserted_4h": inserted_4h,
        "inserted_1d": inserted_1d,
    }


def rebuild_database() -> dict[str, Any]:
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_derive") as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                counts = rebuild(cursor)
    return {"derivation_version": DERIVATION_VERSION, "status": "PASS", **counts}


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Rebuild DB3 derived bars")
    parser.parse_args(list(argv) if argv is not None else None)
    result = rebuild_database()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
