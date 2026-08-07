"""Initial Saxo SIM market-data ingestion with a strict market-only endpoint profile.

The command is intentionally restricted to the isolated ``saxo_market_live``
database.  OAuth credentials are loaded from the documented macOS Keychain
route; access tokens remain in process memory and are never accepted on argv,
environment variables, files, or standard input.
"""

from __future__ import annotations

import argparse
import json
import secrets
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from .connection import LIVE_MARKET_DB, MARKET_DB, connect
from .incremental_update import (
    AcquiredInstrument,
    CANDIDATE_DATASET_ID,
    InstrumentState,
    _commit_acquired,
    _create_run,
    _dataset_contract,
    _error_code,
    _record_failure,
    _write_run_manifest,
    run_incremental,
)
from .instrument_registry import (
    CanonicalInstrument,
    load_canonical_instruments,
    load_research_candidate_instruments,
    validate_detail,
)
from .normalize_bars import BarQualityError, merge_pages, normalize_chart_page
from .raw_artifacts import RunArtifacts, utc_run_id
from .saxo_auth import OAuthConfig, SaxoAuthError, SaxoOAuthManager
from .saxo_client import MARKET_DATA_ONLY_ENDPOINT_PROFILE, SaxoClient
from .session_calendar import apply_calendars


SCOPE_SPEC = "all_except_usdjpy_with_fx_research_candidates_20260727"
EXCLUDED_KEYS = frozenset({"usdjpy"})
ALLOWED_ENDPOINT_IDS = ("instrument_detail", "trading_schedule", "chart")


def _assert_live_target() -> None:
    if MARKET_DB != LIVE_MARKET_DB:
        raise RuntimeError("BLOCKED_MARKET_DATA_ONLY_REQUIRES_ISOLATED_LIVE_DB")


def active_registry() -> tuple[CanonicalInstrument, ...]:
    canonical = tuple(
        item for item in load_canonical_instruments() if item.key not in EXCLUDED_KEYS
    )
    candidates = load_research_candidate_instruments()
    selected = (*canonical, *candidates)
    if len(selected) != 15 or len({item.key for item in selected}) != 15:
        raise RuntimeError("BLOCKED_MARKET_DATA_SCOPE_DRIFT")
    return selected


def bootstrap_catalog() -> dict[str, Any]:
    """Seed reviewed identities only into an otherwise empty isolated target."""

    _assert_live_target()
    registry = active_registry()
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_live_catalog") as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                cursor.execute("SELECT current_database()")
                if cursor.fetchone()[0] != LIVE_MARKET_DB:
                    raise RuntimeError("BLOCKED_LIVE_DATABASE_IDENTITY_MISMATCH")
                cursor.execute(
                    "SELECT (SELECT COUNT(*) FROM raw.market_bar_revision), "
                    "(SELECT COUNT(*) FROM curated.market_bar), "
                    "(SELECT COUNT(*) FROM catalog.source_dataset)"
                )
                raw_rows, curated_rows, datasets = (int(value) for value in cursor.fetchone())
                if raw_rows or curated_rows or datasets:
                    raise RuntimeError("BLOCKED_LIVE_DATABASE_NOT_EMPTY")
                for item in registry:
                    cursor.execute(
                        """
                        INSERT INTO catalog.instrument (
                            provider,environment,market_key,symbol,uic,asset_type,
                            category,currency,active_from_utc
                        ) VALUES ('Saxo OpenAPI','SIM',%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (provider,environment,uic,asset_type) DO UPDATE SET
                            market_key=EXCLUDED.market_key,symbol=EXCLUDED.symbol,
                            category=EXCLUDED.category,currency=EXCLUDED.currency,
                            active_to_utc=NULL
                        """,
                        (
                            item.key,
                            item.symbol,
                            item.uic,
                            item.asset_type,
                            item.category,
                            item.currency,
                            datetime(2002, 9, 25, tzinfo=timezone.utc),
                        ),
                    )
    calendar_result = apply_calendars()
    if int(calendar_result.get("instrument_assignments", -1)) != len(registry):
        raise RuntimeError("BLOCKED_MARKET_DATA_CALENDAR_ASSIGNMENT_MISMATCH")
    calendar_result = {
        **calendar_result,
        "status": "PASS_WITH_EXPLICIT_USDJPY_EXCLUSION",
    }
    return {
        "status": "PASS",
        "database": MARKET_DB,
        "scope_profile": SCOPE_SPEC,
        "instrument_count": len(registry),
        "excluded_instruments": sorted(EXCLUDED_KEYS),
        "market_rows": 0,
        "calendar_result": calendar_result,
        "orders": 0,
        "prechecks": 0,
        "account_endpoint_requests": 0,
    }


def _instrument_id(instrument: CanonicalInstrument) -> int:
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_live_identity") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT instrument_id FROM catalog.instrument
                WHERE provider='Saxo OpenAPI' AND environment='SIM'
                  AND uic=%s AND asset_type=%s AND lower(market_key)=%s
                  AND active_to_utc IS NULL
                """,
                (instrument.uic, instrument.asset_type, instrument.key),
            )
            row = cursor.fetchone()
    if row is None:
        raise RuntimeError("BLOCKED_RUN_SCOPE_INSTRUMENT_MISMATCH")
    return int(row[0])


def _redact_reference(value: Any) -> Any:
    """Drop account/client/token-bearing fields before reference persistence."""

    forbidden_parts = ("account", "clientkey", "token", "authorization", "cookie")
    if isinstance(value, Mapping):
        return {
            str(key): _redact_reference(item)
            for key, item in value.items()
            if not any(part in str(key).replace("_", "").lower() for part in forbidden_parts)
        }
    if isinstance(value, list):
        return [_redact_reference(item) for item in value]
    return value


def _run_one(client: SaxoClient, instrument: CanonicalInstrument, *, count: int) -> dict[str, Any]:
    run_id = utc_run_id(secrets.token_hex(4))
    artifacts = RunArtifacts(run_id)
    db_run_id = _create_run(run_id, (instrument,), trigger="manual_market_data_only_initial")
    chart_artifacts = []
    all_artifacts = []
    dataset_id, spec_path, dataset_name, eligibility = _dataset_contract((instrument,))
    before_requests = client.request_count
    before_endpoint_counts = dict(client.endpoint_counts)
    error_code: str | None = None
    result: dict[str, Any] = {}
    try:
        detail = client.instrument_detail(instrument.uic, instrument.asset_type)
        validate_detail(instrument, detail)
        all_artifacts.append(
            artifacts.write_json(
                f"instruments/{instrument.key}/detail.json",
                _redact_reference(detail),
                row_count=1,
            )
        )
        schedule = client.trading_schedule(instrument.uic, instrument.asset_type)
        if not isinstance(schedule.get("Sessions"), list):
            raise BarQualityError("INVALID_TRADING_SCHEDULE")
        all_artifacts.append(
            artifacts.write_json(
                f"instruments/{instrument.key}/trading_schedule.json",
                _redact_reference(schedule),
                row_count=len(schedule["Sessions"]),
            )
        )
        payload = client.chart(
            instrument.uic,
            instrument.asset_type,
            count=count,
            mode="UpTo",
            time_utc=datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        )
        chart_record = artifacts.write_json(
            f"instruments/{instrument.key}/chart_0001.json",
            payload,
            row_count=len(payload.get("Data") or []),
        )
        chart_artifacts.append(chart_record)
        all_artifacts.append(chart_record)
        bars = tuple(
            merge_pages(
                [
                    normalize_chart_page(
                        instrument,
                        payload,
                        retrieved_at_utc=datetime.now(timezone.utc),
                        payload_sha256=chart_record.sha256,
                        artifact_relative_path=chart_record.relative_path,
                    )
                ]
            )
        )
        complete = tuple(bar for bar in bars if bar.is_complete)
        if len(bars) < 2 or not complete:
            raise BarQualityError("INSUFFICIENT_INITIAL_CHART_DATA")
        versions = {bar.data_version for bar in bars if bar.data_version is not None}
        if len(versions) > 1:
            raise BarQualityError("MULTIPLE_DATA_VERSIONS_IN_RUN")
        state = InstrumentState(
            instrument_id=_instrument_id(instrument),
            latest_complete_time_utc=max(bar.time_utc for bar in complete),
            data_version=None,
            data_status="INITIAL",
        )
        result = _commit_acquired(
            db_run_id,
            [AcquiredInstrument(instrument, state, bars)],
            chart_artifacts,
            dataset_id=dataset_id,
            bootstrap_watermark=True,
        )
        status = "PASS"
    except Exception as exc:
        error_code = _error_code(exc)
        _record_failure(
            db_run_id,
            error_code,
            chart_artifacts,
            (instrument.key,),
            dataset_id=dataset_id,
            spec_relative_path=spec_path,
            dataset_name=dataset_name,
            research_eligibility=eligibility,
        )
        status = "BLOCKED" if error_code.startswith("BLOCKED") else "FAILED"
    manifest = _write_run_manifest(
        artifacts,
        db_run_id=db_run_id,
        status=status,
        error_code=error_code,
        smoke_result=None,
        successful_series=1 if status == "PASS" else 0,
        client=client,
        all_artifacts=all_artifacts,
        result=result,
        failed_instrument_key=None if status == "PASS" else instrument.key,
    )
    endpoint_delta = {
        key: client.endpoint_counts.get(key, 0) - before_endpoint_counts.get(key, 0)
        for key in ALLOWED_ENDPOINT_IDS
    }
    return {
        "instrument_key": instrument.key,
        "status": status,
        "error_code": error_code,
        "database_ingestion_run_id": db_run_id,
        "manifest_relative_path": manifest.relative_path,
        "request_count": client.request_count - before_requests,
        "endpoint_counts": endpoint_delta,
        "write_request_count": 0,
        "orders": 0,
        "prechecks": 0,
        **result,
    }


def run_initial(
    client: SaxoClient,
    *,
    count: int = 1200,
    instrument_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    _assert_live_target()
    if client.endpoint_profile != MARKET_DATA_ONLY_ENDPOINT_PROFILE:
        raise RuntimeError("BLOCKED_MARKET_DATA_ENDPOINT_PROFILE")
    if not 2 <= count <= 1200:
        raise ValueError("count must be between 2 and 1200")
    registry = active_registry()
    if instrument_keys is not None:
        requested = tuple(str(key).lower() for key in instrument_keys)
        by_key = {item.key: item for item in registry}
        if not requested or len(requested) != len(set(requested)) or any(key not in by_key for key in requested):
            raise ValueError("instrument keys must be a unique active-scope subset")
        registry = tuple(by_key[key] for key in requested)
    results = [_run_one(client, instrument, count=count) for instrument in registry]
    passed = sum(item["status"] == "PASS" for item in results)
    return {
        "status": "PASS" if passed == len(results) else "PASS_WITH_FAILURES" if passed else "FAILED",
        "database": MARKET_DB,
        "scope_profile": SCOPE_SPEC,
        "requested_instruments": [item.key for item in registry],
        "successful_instruments": passed,
        "failed_instruments": len(results) - passed,
        "request_count": client.request_count,
        "endpoint_counts": dict(sorted(client.endpoint_counts.items())),
        "allow_list": list(ALLOWED_ENDPOINT_IDS),
        "write_request_count": client.write_request_count,
        "orders": 0,
        "prechecks": 0,
        "account_endpoint_requests": sum(
            client.endpoint_counts.get(key, 0)
            for key in ("users_me", "accounts_me", "balances_me", "session_capabilities")
        ),
        "results": results,
    }


def run_update(
    client: SaxoClient,
    *,
    instrument_keys: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Run the reviewed incremental body without the legacy users/me smoke."""

    _assert_live_target()
    if client.endpoint_profile != MARKET_DATA_ONLY_ENDPOINT_PROFILE:
        raise RuntimeError("BLOCKED_MARKET_DATA_ENDPOINT_PROFILE")
    registry = active_registry()
    if instrument_keys is not None:
        requested = tuple(str(key).lower() for key in instrument_keys)
        by_key = {item.key: item for item in registry}
        if not requested or len(requested) != len(set(requested)) or any(key not in by_key for key in requested):
            raise ValueError("instrument keys must be a unique active-scope subset")
        registry = tuple(by_key[key] for key in requested)
    results = [
        run_incremental(
            client,
            instrument_keys=(instrument.key,),
            trigger="manual_market_data_only_update",
            perform_smoke_test=False,
        )
        for instrument in registry
    ]
    passed = sum(item["status"] == "PASS" for item in results)
    return {
        "status": "PASS" if passed == len(results) else "PASS_WITH_FAILURES" if passed else "FAILED",
        "database": MARKET_DB,
        "scope_profile": SCOPE_SPEC,
        "requested_instruments": [item.key for item in registry],
        "successful_instruments": passed,
        "failed_instruments": len(results) - passed,
        "request_count": client.request_count,
        "endpoint_counts": dict(sorted(client.endpoint_counts.items())),
        "allow_list": list(ALLOWED_ENDPOINT_IDS),
        "write_request_count": client.write_request_count,
        "orders": 0,
        "prechecks": 0,
        "account_endpoint_requests": sum(
            client.endpoint_counts.get(key, 0)
            for key in ("users_me", "accounts_me", "balances_me", "session_capabilities")
        ),
        "results": results,
    }


def target_status() -> dict[str, Any]:
    _assert_live_target()
    with connect("saxo_ingest", MARKET_DB, application_name="saxo_db_live_status") as conn:
        with conn.cursor() as cursor:
            cursor.execute("SET TRANSACTION READ ONLY")
            cursor.execute(
                """
                SELECT
                  (SELECT COUNT(*) FROM catalog.instrument WHERE provider='Saxo OpenAPI' AND environment='SIM'),
                  (SELECT COUNT(*) FROM raw.market_bar_revision),
                  (SELECT COUNT(*) FROM curated.market_bar),
                  (SELECT COUNT(*) FROM ops.watermark),
                  (SELECT COUNT(*) FROM ops.ingestion_run WHERE status='PASS')
                """
            )
            instruments, raw_rows, curated_rows, watermarks, pass_runs = (
                int(value) for value in cursor.fetchone()
            )
    return {
        "status": "PASS",
        "database": MARKET_DB,
        "instruments": instruments,
        "raw_rows": raw_rows,
        "curated_rows": curated_rows,
        "watermarks": watermarks,
        "pass_runs": pass_runs,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="isolated Saxo market-data-only ingestion")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("bootstrap-catalog")
    subparsers.add_parser("status")
    run_initial_parser = subparsers.add_parser("run-initial")
    run_initial_parser.add_argument("--count", type=int, default=1200)
    run_initial_parser.add_argument("--instrument", action="append", dest="instruments")
    run_initial_parser.add_argument("--ack-market-data-get-only", action="store_true")
    run_update_parser = subparsers.add_parser("run-update")
    run_update_parser.add_argument("--instrument", action="append", dest="instruments")
    run_update_parser.add_argument("--ack-market-data-get-only", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "bootstrap-catalog":
            result = bootstrap_catalog()
        elif args.command == "status":
            result = target_status()
        else:
            if not args.ack_market_data_get_only:
                raise RuntimeError("BLOCKED_MARKET_DATA_GET_ACK_REQUIRED")
            manager = SaxoOAuthManager(OAuthConfig.from_local_configuration(callback_port=8765))
            if manager.status().get("status") != "AUTH_READY":
                raise SaxoAuthError(str(manager.status().get("status") or "AUTH_NOT_READY"))
            client = SaxoClient(
                manager.access_token(),
                endpoint_profile=MARKET_DATA_ONLY_ENDPOINT_PROFILE,
            )
            if args.command == "run-initial":
                result = run_initial(client, count=args.count, instrument_keys=args.instruments)
            else:
                result = run_update(client, instrument_keys=args.instruments)
    except (RuntimeError, ValueError, SaxoAuthError) as exc:
        code = exc.code if isinstance(exc, SaxoAuthError) else str(exc)
        result = {
            "status": "BLOCKED" if code.startswith(("BLOCKED", "AUTH_")) else "FAILED",
            "error_code": code,
            "orders": 0,
            "prechecks": 0,
            "account_endpoint_requests": 0,
        }
    print(json.dumps(result, sort_keys=True, default=str))
    return 0 if result.get("status") in {"PASS", "PASS_WITH_FAILURES"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
