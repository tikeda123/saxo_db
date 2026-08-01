#!/usr/bin/env python3
"""Two-GET, no-persistence probe for the TIP/GLD 2026-07-29 gap.

Only sanitized identity, timestamp, quality, version, and content-hash evidence
is printed.  Provider prices, credentials, and account identifiers are neither
saved nor emitted.
"""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from market_db.connection import MARKET_DB, connect
from market_db.instrument_registry import load_canonical_instruments
from market_db.normalize_bars import normalize_chart_page
from market_db.saxo_auth import DEFAULT_CALLBACK_PORT, OAuthConfig, SaxoOAuthManager
from market_db.saxo_client import SaxoClient


TARGETS = ("tip", "gld")
SESSION_DATE = date(2026, 7, 29)


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _canonical_sha256(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=lambda value: str(value) if isinstance(value, Decimal) else value,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _expected_times() -> dict[str, tuple[datetime, ...]]:
    registry = {item.key: item for item in load_canonical_instruments()}
    result: dict[str, tuple[datetime, ...]] = {}
    with connect(
        "saxo_ingest", MARKET_DB,
        application_name="c2_tip_gld_gap_readonly_probe",
    ) as conn:
        with conn.transaction():
            conn.execute("SET TRANSACTION READ ONLY")
            for key in TARGETS:
                instrument = registry[key]
                row = conn.execute(
                    """
                    SELECT si.open_time_utc,si.close_time_utc
                    FROM catalog.instrument i
                    JOIN catalog.session_interval si
                      ON si.session_calendar_id=i.session_calendar_id
                    JOIN catalog.session_calendar c
                      ON c.session_calendar_id=si.session_calendar_id
                    WHERE i.provider='Saxo OpenAPI' AND i.environment='SIM'
                      AND i.uic=%s AND i.asset_type=%s
                      AND si.session_date=%s AND si.session_status <> 'HOLIDAY'
                      AND c.metadata_json->>'verification_status'='VERIFIED'
                    ORDER BY si.interval_sequence
                    LIMIT 1
                    """,
                    (instrument.uic, instrument.asset_type, SESSION_DATE),
                ).fetchone()
                if row is None:
                    raise RuntimeError(f"VERIFIED_SESSION_NOT_FOUND:{key}")
                open_time, close_time = row
                slots: list[datetime] = []
                cursor = open_time
                while cursor < close_time:
                    slots.append(cursor)
                    cursor += timedelta(hours=1)
                result[key] = tuple(slots)
    return result


def probe() -> dict[str, Any]:
    registry = {item.key: item for item in load_canonical_instruments()}
    expected = _expected_times()
    oauth = SaxoOAuthManager(
        OAuthConfig.from_local_configuration(callback_port=DEFAULT_CALLBACK_PORT)
    )
    client = SaxoClient(oauth.access_token())
    series: list[dict[str, Any]] = []
    for key in TARGETS:
        instrument = registry[key]
        requested_from = min(expected[key])
        payload = client.chart(
            instrument.uic,
            instrument.asset_type,
            count=10,
            mode="From",
            time_utc=_utc_text(requested_from),
        )
        retrieved_at = datetime.now(timezone.utc)
        response_sha256 = _canonical_sha256(payload)
        bars = normalize_chart_page(
            instrument,
            payload,
            retrieved_at_utc=retrieved_at,
            payload_sha256=response_sha256,
            artifact_relative_path="not_persisted/read_only_probe",
        )
        times = [bar.time_utc for bar in bars]
        expected_set = set(expected[key])
        observed_set = set(times)
        series.append(
            {
                "instrument_key": key,
                "data_version": bars[0].data_version if bars else payload.get("DataVersion"),
                "response_sha256": response_sha256,
                "provider_rows": len(bars),
                "timestamps_strict_unique": times == sorted(times) and len(times) == len(set(times)),
                "normalization_status": "PASS",
                "expected_session_slot_count": len(expected_set),
                "observed_expected_slot_count": len(expected_set & observed_set),
                "missing_expected_times_utc": [
                    _utc_text(item) for item in sorted(expected_set - observed_set)
                ],
                "session_gap_status": (
                    "PASS" if expected_set <= observed_set else "PROVIDER_ROWS_STILL_MISSING"
                ),
            }
        )
    return {
        "probe_id": "c2_tip_gld_20260729_readonly_probe_v1",
        "generated_at_utc": _utc_text(datetime.now(timezone.utc)),
        "environment": "SIM",
        "method": "GET",
        "endpoint": "/chart/v3/charts",
        "requested_instruments": list(TARGETS),
        "requested_session_date": SESSION_DATE.isoformat(),
        "saxo_get_request_count": client.request_count,
        "write_requests_to_saxo": client.write_request_count,
        "database_writes": 0,
        "raw_payload_persisted": False,
        "provider_values_emitted": False,
        "orders_or_prechecks_sent": 0,
        "usdjpy_touched": False,
        "series": series,
    }


def main() -> int:
    print(json.dumps(probe(), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
