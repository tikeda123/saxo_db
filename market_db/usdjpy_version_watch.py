"""Minimal, non-publishing DataVersion watch for quarantined USDJPY.

The probe performs one read-only Saxo Chart GET with Count=1. A response with
the already quarantined DataVersion is not retained, so monitoring does not
accumulate duplicate raw history. A different DataVersion is retained as an
isolated provider-evidence artifact only; this module never starts a full
refetch, changes publication state, or writes curated/raw DB tables.
"""

from __future__ import annotations

import argparse
import json
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .connection import MARKET_DB, connect
from .instrument_registry import load_canonical_instruments
from .raw_artifacts import RunArtifacts, utc_run_id
from .saxo_auth import DEFAULT_CALLBACK_PORT, OAuthConfig, SaxoOAuthManager
from .saxo_client import SaxoClient


INSTRUMENT_KEY = "usdjpy"
KNOWN_QUARANTINED_DATA_VERSION = 29738069


def _instrument() -> Any:
    matches = [item for item in load_canonical_instruments() if item.key == INSTRUMENT_KEY]
    if len(matches) != 1:
        raise RuntimeError("USDJPY_CANONICAL_IDENTITY_MISSING")
    return matches[0]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def classify_version(
    observed_data_version: int,
    *,
    accepted_data_version: int,
    quarantined_data_version: int = KNOWN_QUARANTINED_DATA_VERSION,
    last_observed_data_version: int | None = None,
) -> dict[str, Any]:
    if observed_data_version == quarantined_data_version:
        return {
            "status": "NO_CHANGE_QUARANTINE_MAINTAINED",
            "new_data_version_detected": False,
            "retain_probe_artifact": False,
            "guarded_full_refetch": "NOT_PERMITTED_SAME_QUARANTINED_VERSION",
        }
    if observed_data_version == last_observed_data_version:
        return {
            "status": "NO_CHANGE_REVISION_REVIEW_PENDING",
            "new_data_version_detected": False,
            "retain_probe_artifact": False,
            "guarded_full_refetch": "ELIGIBLE_FOR_SEPARATE_OPERATOR_DECISION",
        }
    if observed_data_version == accepted_data_version:
        return {
            "status": "PROVIDER_VERSION_REVERSION_REVIEW_REQUIRED",
            "new_data_version_detected": False,
            "retain_probe_artifact": True,
            "guarded_full_refetch": "NOT_PERMITTED_WITHOUT_REVIEW",
        }
    return {
        "status": "NEW_PROVIDER_DATA_VERSION_REVIEW_REQUIRED",
        "new_data_version_detected": True,
        "retain_probe_artifact": True,
        "guarded_full_refetch": "ELIGIBLE_FOR_SEPARATE_OPERATOR_DECISION",
    }


def _last_retained_probe() -> tuple[int | None, str | None]:
    paths = sorted(
        Path("data/acquisition/runs").glob(
            "*/instruments/usdjpy/data_version_probe.json"
        )
    )
    for path in reversed(paths):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            value = payload.get("DataVersion")
            if isinstance(value, bool) or value is None:
                continue
            return int(value), str(path)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError):
            continue
    return None, None


def status_snapshot() -> dict[str, Any]:
    with connect(
        "saxo_app_reader", MARKET_DB, application_name="saxo_db_usdjpy_version_status"
    ) as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT last_accepted_data_version,last_accepted_complete_time_utc,
                       data_status
                FROM ops.v_series_revision_availability
                WHERE instrument_key='usdjpy' AND horizon_minutes=60
                  AND price_basis='bid_ask_mid'
                """
            )
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("USDJPY_WATERMARK_MISSING")
    last_observed, last_probe_path = _last_retained_probe()
    return {
        "status": "BLOCKED_PROVIDER_CONTENT_QUALITY",
        "instrument_key": INSTRUMENT_KEY,
        "accepted_data_version": int(row[0]),
        "known_quarantined_data_version": KNOWN_QUARANTINED_DATA_VERSION,
        "last_observed_provider_data_version": last_observed,
        "last_probe_artifact_relative_path": last_probe_path,
        "latest_accepted_complete_time_utc": row[1],
        "data_status": str(row[2]),
        "scheduler_included": False,
        "full_refetch_started": False,
        "database_mutations": 0,
        "orders_or_prechecks_sent": 0,
    }


def probe(*, callback_port: int = DEFAULT_CALLBACK_PORT) -> dict[str, Any]:
    baseline = status_snapshot()
    manager = SaxoOAuthManager(OAuthConfig.from_environment(callback_port=callback_port))
    access_token = manager.access_token(force_refresh=True)
    try:
        client = SaxoClient(access_token)
    finally:
        access_token = ""
    instrument = _instrument()
    payload = client.chart(instrument.uic, instrument.asset_type, count=1)
    value = payload.get("DataVersion")
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise RuntimeError("USDJPY_PROBE_DATA_VERSION_MISSING")
    try:
        observed = int(value)
    except ValueError:
        raise RuntimeError("USDJPY_PROBE_DATA_VERSION_INVALID") from None
    decision = classify_version(
        observed,
        accepted_data_version=int(baseline["accepted_data_version"]),
        last_observed_data_version=baseline["last_observed_provider_data_version"],
    )
    artifact: dict[str, Any] | None = None
    if decision["retain_probe_artifact"]:
        run_id = utc_run_id(secrets.token_hex(4))
        record = RunArtifacts(run_id).write_json(
            "instruments/usdjpy/data_version_probe.json",
            payload,
            row_count=len(payload.get("Data") or []),
        )
        artifact = {
            "relative_path": record.relative_path,
            "sha256": record.sha256,
            "size_bytes": record.size_bytes,
        }
    return {
        **baseline,
        **decision,
        "checked_at_utc": _utc_now(),
        "observed_data_version": observed,
        "provider_request_count": client.request_count,
        "provider_write_request_count": client.write_request_count,
        "probe_count": 1,
        "probe_artifact": artifact,
        "full_refetch_started": False,
        "database_mutations": 0,
        "orders_or_prechecks_sent": 0,
    }


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="USDJPY quarantined DataVersion watch")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status")
    selected = subparsers.add_parser("probe")
    selected.add_argument("--auth-mode", choices=("keychain",), default="keychain")
    selected.add_argument("--callback-port", type=int, default=DEFAULT_CALLBACK_PORT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    result = status_snapshot() if args.command == "status" else probe(callback_port=args.callback_port)
    print(json.dumps(result, ensure_ascii=False, default=str, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
