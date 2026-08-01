"""Build, validate and append C2 external-data receipts without market-data writes."""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from psycopg.rows import dict_row

from .connection import MARKET_DB, connect, project_root
from .strategy_external_contract import (
    MANIFEST_RELATIVE_PATH,
    canonical_json_sha256,
    finalize_strategy_external_receipt,
    load_strategy_external_contract,
    validate_strategy_external_receipt,
)


CALENDAR_ID = "ARCX_XNAS_COMMON_REGULAR_2026"
CALENDAR_VERSION = "arcx_xnas_common_2026_v1"
NY = ZoneInfo("America/New_York")
HOLIDAYS_2026 = {
    date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16),
    date(2026, 4, 3), date(2026, 5, 25), date(2026, 6, 19),
    date(2026, 7, 3), date(2026, 9, 7), date(2026, 11, 26),
    date(2026, 12, 25),
}
EARLY_CLOSES_2026 = {date(2026, 11, 27), date(2026, 12, 24)}
NYSE_URL = "https://www.nyse.com/trade/hours-calendars"
NASDAQ_URL = "https://www.nasdaqtrader.com/trader.aspx?id=Calendar"
SAXO_FEE_SCHEDULE_URL = (
    "https://www.home.saxo/rates-and-conditions/commissions-charges-and-margin-schedule"
)
SAXO_DOCS = {
    "accounts": "https://www.developer.saxo/openapi/referencedocs/port/v1/accounts",
    "balance": "https://www.developer.saxo/openapi/referencedocs/port/v1/balances/get__port__me",
    "capabilities": "https://www.developer.saxo/openapi/referencedocs/root/v1/sessions/capabilities",
    "transactions": "https://www.developer.saxo/openapi/referencedocs/hist/v1/transactions/get__hist",
    "info_prices": "https://www.developer.saxo/openapi/referencedocs/trade/v1/infoprices",
}
ISSUER_SOURCES = {
    "eem": "https://www.ishares.com/us/products/239637/ishares-msci-emerging-markets-etf",
    "efa": "https://www.ishares.com/us/products/239623/ishares-msci-eafe-etf",
    "gld": "https://www.ssga.com/us/en/individual/etfs/spdr-gold-shares-gld",
    "ief": "https://www.ishares.com/us/products/239456/ishares-710-year-treasury-bond-etf",
    "iwm": "https://www.ishares.com/us/products/239710/ishares-russell-2000-etf",
    "lqd": "https://www.ishares.com/us/products/239566/ishares-iboxx-investment-grade-corporate-bond-etf",
    "shy": "https://www.ishares.com/us/products/239452/ishares-1-3-year-treasury-bond-etf",
    "spy": "https://www.ssga.com/us/en/individual/etfs/state-street-spdr-sp-500-etf-trust-spy",
    "tip": "https://www.ishares.com/us/products/239467/ishares-tips-bond-etf",
    "tlt": "https://www.ishares.com/us/products/239454/TLT",
    "vnq": "https://investor.vanguard.com/investment-products/etfs/profile/vnq",
}


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _calendar_sessions() -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    day = date(2026, 1, 1)
    end = date(2027, 1, 1)
    while day < end:
        if day.weekday() < 5 and day not in HOLIDAYS_2026:
            close_time = time(13, 0) if day in EARLY_CLOSES_2026 else time(16, 0)
            open_utc = datetime.combine(day, time(9, 30), NY).astimezone(timezone.utc)
            close_utc = datetime.combine(day, close_time, NY).astimezone(timezone.utc)
            selected.append(
                {
                    "session_date": day.isoformat(),
                    "open_utc": _utc_text(open_utc),
                    "close_utc": _utc_text(close_utc),
                    "session_state": "SHORT_SESSION" if day in EARLY_CLOSES_2026 else "OPEN",
                    "early_close": day in EARLY_CLOSES_2026,
                    "venues": ["ARCX", "XNAS"],
                }
            )
        day += timedelta(days=1)
    return selected


def _base_receipt(
    *,
    observed: datetime,
    contract_id: str,
    dataset_role: str,
    suffix: str,
    availability_state: str,
    freshness_state: str,
    quality_state: str,
    blocker_ids: list[str],
    warning_ids: list[str] | None = None,
    provider_id: str | None = None,
    payload: Mapping[str, Any] | None = None,
    dataset_id: str | None = None,
    provider_data_version: str | None = None,
    lineage_id: str | None = None,
    ordered_content_sha256: str | None = None,
    calendar_id: str | None = None,
    accepted: bool = False,
    cost_confidence: str = "NOT_APPLICABLE",
    supersedes_receipt_id: str | None = None,
) -> dict[str, Any]:
    stamp = observed.strftime("%Y%m%dT%H%M%SZ")
    return finalize_strategy_external_receipt(
        {
            "schema_version": 1,
            "receipt_id": f"c2-{stamp}-{suffix}",
            "contract_id": contract_id,
            "dataset_role": dataset_role,
            "availability_state": availability_state,
            "dataset_id": dataset_id,
            "provider_id": provider_id,
            "provider_data_version": provider_data_version,
            "lineage_id": lineage_id,
            "manifest_sha256": load_strategy_external_contract()[1],
            "ordered_content_sha256": ordered_content_sha256,
            "calendar_id": calendar_id,
            "source_as_of": observed.date().isoformat(),
            "source_observed_at_utc": _utc_text(observed),
            "available_at_utc": _utc_text(observed),
            "accepted_at_utc": _utc_text(observed) if accepted else None,
            "expected_by_utc": None,
            "published_at_utc": None,
            "freshness_state": freshness_state,
            "quality_state": quality_state,
            "revision_state": "CURRENT_ACCEPTED" if accepted else "NOT_EVALUATED",
            "cost_confidence": cost_confidence,
            "warning_ids": warning_ids or [],
            "blocker_ids": blocker_ids,
            "values_modified": False,
            "interpolation_performed": False,
            "account_fingerprint": None,
            "payload": dict(payload or {}),
            "supersedes_receipt_id": supersedes_receipt_id,
        }
    )


def build_public_and_blocker_receipts(
    *, nyse_path: Path, nasdaq_path: Path, observed: datetime
) -> list[dict[str, Any]]:
    nyse = nyse_path.read_text(encoding="utf-8")
    nasdaq = nasdaq_path.read_text(encoding="utf-8")
    required_markers = (
        "2026", "November 27", "December 24", "1:00",
    )
    if any(marker not in nyse or marker not in nasdaq for marker in required_markers):
        raise RuntimeError("OFFICIAL_CALENDAR_SOURCE_MARKERS_MISSING")
    sessions = _calendar_sessions()
    session_hash = canonical_json_sha256(sessions)
    source_hashes = {
        "nyse_sha256": _file_sha256(nyse_path),
        "nasdaq_sha256": _file_sha256(nasdaq_path),
    }
    tzdb_version = "system-zoneinfo-rule-hash-" + canonical_json_sha256(
        [{"date": row["session_date"], "open": row["open_utc"], "close": row["close_utc"]} for row in sessions]
    )[:16]
    calendar_payload = {
        "calendar_id": CALENDAR_ID,
        "calendar_version": CALENDAR_VERSION,
        "tzdb_version": tzdb_version,
        "published_at_utc": None,
        "valid_from": "2026-01-01",
        "valid_to": "2026-12-31",
        "source_urls": [NYSE_URL, NASDAQ_URL],
        "source_sha256": source_hashes,
        "normalization": "intersection of ARCX and XNAS regular sessions",
        "normalized_sha256": session_hash,
        "sessions": sessions,
    }
    receipts = [
        _base_receipt(
            observed=observed,
            contract_id="c2_edc03_common_regular_session_calendar_v1",
            dataset_role="COMMON_REGULAR_SESSION_CALENDAR",
            suffix="calendar-2026",
            availability_state="AVAILABLE_WITH_WARNINGS",
            freshness_state="CURRENT",
            quality_state="PASS_WITH_WARNINGS",
            blocker_ids=[],
            warning_ids=["SOURCE_PUBLISHED_AT_NOT_EXPOSED"],
            provider_id="NYSE_NASDAQ_OFFICIAL_PUBLICATIONS",
            payload=calendar_payload,
            dataset_id=f"{CALENDAR_ID}:{CALENDAR_VERSION}",
            provider_data_version=f"nyse:{source_hashes['nyse_sha256']};nasdaq:{source_hashes['nasdaq_sha256']}",
            lineage_id="official-pages-to-normalized-common-calendar-v1",
            ordered_content_sha256=session_hash,
            calendar_id=CALENDAR_ID,
            accepted=True,
        )
    ]
    blocked = (
        ("c2_edc04_distribution_declaration_v1", "DISTRIBUTION_DECLARATION", "distribution-source", "BLOCKED_EXTERNAL_CONTRACT_ISSUER_REVISION_MONITOR_NOT_VERIFIED", {"source_kind": "issuer official publications", "coverage_required": 11, "revision_monitor_verified": False}),
        ("c2_edc05_distribution_cash_transaction_v1", "DISTRIBUTION_CASH_TRANSACTION", "cash-auth", "BLOCKED_INTERFACE_OPERATIONAL_AUTH_NOT_READY", {"endpoint": "GET /hist/v1/transactions", "required_permission": "Personal: Read", "official_documentation": SAXO_DOCS["transactions"]}),
        ("c2_edc06_instrument_reference_v1", "INSTRUMENT_REFERENCE", "instrument-auth", "BLOCKED_INTERFACE_OPERATIONAL_AUTH_NOT_READY", {"endpoints": ["GET instrument details", "GET accounts/me", "GET balances/me", "GET session capabilities"], "official_documentation": [SAXO_DOCS["accounts"], SAXO_DOCS["balance"], SAXO_DOCS["capabilities"]], "instrument_count": 11}),
        ("c2_edc07_proposal_price_snapshot_v1", "PROPOSAL_PRICE_SNAPSHOT", "quote-auth", "BLOCKED_INTERFACE_OPERATIONAL_AUTH_NOT_READY", {"endpoint": "GET /trade/v1/infoprices/list", "snapshot_is_tradable": False, "delay_must_be_observed": True, "official_documentation": SAXO_DOCS["info_prices"]}),
        ("c2_edc08_fee_estimate_and_actual_v1", "FEE_ESTIMATE_AND_ACTUAL", "fees", "BLOCKED_EXTERNAL_CONTRACT_ACCOUNT_FEE_SCHEDULE_AND_AUTH", {"estimate_source": "account-specific official fee schedule not supplied", "actual_source": "GET /hist/v1/transactions", "info_price_commission_accepted": False}),
        ("c2_edc09_currency_and_amount_unit_v1", "CURRENCY_AND_AMOUNT_UNIT", "quantum-auth", "BLOCKED_INTERFACE_OPERATIONAL_AUTH_NOT_READY", {"sources": ["GET /port/v1/accounts/me", "GET /port/v1/balances/me", "GET instrument details"], "repository_currency_mapping_is_not_account_evidence": True}),
        ("c2_edc10_revision_and_latency_state_v1", "REVISION_AND_LATENCY_STATE", "sla", "BLOCKED_EXTERNAL_CONTRACT_UPSTREAM_RECEIPTS_INCOMPLETE", {"derivation": "accepted role receipts", "independent_provider": False}),
    )
    for contract_id, role, suffix, blocker, payload in blocked:
        receipts.append(
            _base_receipt(
                observed=observed,
                contract_id=contract_id,
                dataset_role=role,
                suffix=suffix,
                availability_state=(
                    "BLOCKED_INTERFACE_OPERATIONAL"
                    if "INTERFACE_OPERATIONAL" in blocker
                    else "BLOCKED_EXTERNAL_CONTRACT"
                ),
                freshness_state=(
                    "BLOCKED_INTERFACE_OPERATIONAL"
                    if "INTERFACE_OPERATIONAL" in blocker
                    else "NOT_EVALUATED_SLA"
                ),
                quality_state="NOT_EVALUATED",
                blocker_ids=[blocker],
                provider_id="SAXO_OPENAPI_SIM" if "AUTH" in blocker else None,
                payload=payload,
                cost_confidence="UNKNOWN" if role == "FEE_ESTIMATE_AND_ACTUAL" else "NOT_APPLICABLE",
            )
        )
    return receipts


def build_issuer_probe_receipt(*, source_dir: Path, observed: datetime) -> dict[str, Any]:
    sources: list[dict[str, Any]] = []
    for instrument_key, url in sorted(ISSUER_SOURCES.items()):
        path = source_dir / f"c2_issuer_{instrument_key}.html"
        if not path.is_file() or path.is_symlink():
            raise RuntimeError(f"ISSUER_SOURCE_MISSING:{instrument_key}")
        content = path.read_bytes()
        lower = content.lower()
        sources.append(
            {
                "instrument_key": instrument_key,
                "url": url,
                "sha256": hashlib.sha256(content).hexdigest(),
                "size_bytes": len(content),
                "distribution_marker_present": (
                    b"distribution" in lower or b"dividend" in lower
                ),
            }
        )
    payload = {
        "source_kind": "issuer official product publications",
        "instrument_count": len(sources),
        "all_sources_reachable": len(sources) == 11,
        "sources": sources,
        "revision_identity": "HTTP response content SHA-256 at source_observed_at_utc",
        "published_at_utc_exposed": False,
        "structured_correction_history_verified": False,
        "negative_distribution_event_semantics_verified": False,
        "values_extracted_or_accepted": False,
    }
    return _base_receipt(
        observed=observed,
        contract_id="c2_edc04_distribution_declaration_v1",
        dataset_role="DISTRIBUTION_DECLARATION",
        suffix="issuer-source-probe",
        availability_state="BLOCKED_EXTERNAL_CONTRACT",
        freshness_state="NOT_EVALUATED_SLA",
        quality_state="NOT_EVALUATED",
        blocker_ids=["BLOCKED_EXTERNAL_CONTRACT_ISSUER_REVISION_MONITOR_NOT_VERIFIED"],
        provider_id="ETF_ISSUER_OFFICIAL_PUBLICATIONS",
        provider_data_version=canonical_json_sha256(sources),
        lineage_id="issuer-official-http-source-probe-v1",
        payload=payload,
        supersedes_receipt_id="c2-20260731T000955Z-distribution-source",
    )


def build_fee_probe_receipt(*, source_path: Path, observed: datetime) -> dict[str, Any]:
    if not source_path.is_file() or source_path.is_symlink():
        raise RuntimeError("FEE_SOURCE_MISSING")
    content = source_path.read_bytes()
    lower = content.lower()
    if b"commission" not in lower or b"charge" not in lower:
        raise RuntimeError("FEE_SOURCE_MARKERS_MISSING")
    source_sha256 = hashlib.sha256(content).hexdigest()
    payload = {
        "source_kind": "Saxo official public pricing publication",
        "public_url": SAXO_FEE_SCHEDULE_URL,
        "public_source_sha256": source_sha256,
        "public_source_size_bytes": len(content),
        "generic_schedule_reachable": True,
        "account_specific_applicability_verified": False,
        "estimate_accepted": False,
        "info_price_commission_supported": False,
        "actual_source": "GET /hist/v1/transactions",
        "actual_source_permission": "Personal: Read",
        "actual_transaction_read_verified": False,
        "actual_cost_fields": ["CostClass", "CostSubClass", "TotalCost"],
        "official_documentation": [
            SAXO_DOCS["transactions"],
            SAXO_DOCS["info_prices"],
        ],
        "values_extracted_or_accepted": False,
    }
    return _base_receipt(
        observed=observed,
        contract_id="c2_edc08_fee_estimate_and_actual_v1",
        dataset_role="FEE_ESTIMATE_AND_ACTUAL",
        suffix="fee-source-probe",
        availability_state="BLOCKED_EXTERNAL_CONTRACT",
        freshness_state="NOT_EVALUATED_SLA",
        quality_state="NOT_EVALUATED",
        blocker_ids=["BLOCKED_EXTERNAL_CONTRACT_ACCOUNT_FEE_SCHEDULE_AND_AUTH"],
        provider_id="SAXO_OFFICIAL_PUBLIC_PRICING",
        provider_data_version=source_sha256,
        lineage_id="saxo-official-public-fee-schedule-probe-v1",
        payload=payload,
        cost_confidence="UNKNOWN",
        supersedes_receipt_id="c2-20260731T000955Z-fees",
    )


COUNT_QUERY = """
SELECT
  (SELECT COUNT(*) FROM analytics.v_strategy_external_data_receipt) AS receipt_rows,
  (SELECT COUNT(*) FROM raw.market_bar_revision) AS raw_market_bar_revision_rows,
  (SELECT COUNT(*) FROM raw.reference_observation) AS raw_reference_observation_rows,
  (SELECT COUNT(*) FROM curated.market_bar) AS curated_market_bar_rows,
  (SELECT COUNT(*) FROM ops.ingestion_run) AS ingestion_run_rows,
  has_schema_privilege('saxo_ingest','ops','USAGE') AS ingest_ops_schema_usage,
  has_table_privilege('saxo_ingest','ops.strategy_external_data_receipt','INSERT') AS ingest_receipt_insert,
  has_table_privilege('saxo_app_reader','analytics.v_strategy_external_data_receipt','SELECT') AS reader_receipt_view_select,
  has_table_privilege('saxo_app_reader','ops.strategy_external_data_receipt','SELECT') AS reader_receipt_table_select,
  (SELECT COUNT(*) FROM pg_trigger
   WHERE tgrelid='ops.strategy_external_data_receipt'::regclass
     AND tgname='strategy_external_receipt_immutable'
     AND tgenabled <> 'D') AS append_only_trigger_count
"""


def audit_counts() -> dict[str, int | bool]:
    with connect("saxo_migrator", MARKET_DB, application_name="c2_receipt_audit") as conn:
        with conn.transaction():
            with conn.cursor(row_factory=dict_row) as cursor:
                cursor.execute("SET TRANSACTION READ ONLY")
                cursor.execute(COUNT_QUERY)
                return {
                    key: value if isinstance(value, bool) else int(value)
                    for key, value in dict(cursor.fetchone()).items()
                }


INSERT_SQL = """
INSERT INTO ops.strategy_external_data_receipt (
  receipt_id,contract_id,availability_state,dataset_id,provider_id,
  provider_data_version,lineage_id,manifest_sha256,ordered_content_sha256,
  calendar_id,source_as_of,source_observed_at_utc,available_at_utc,
  accepted_at_utc,expected_by_utc,published_at_utc,freshness_state,
  quality_state,revision_state,cost_confidence,warning_ids,blocker_ids,
  account_fingerprint,values_modified,interpolation_performed,receipt_json,
  receipt_sha256,supersedes_receipt_id
) VALUES (
  %(receipt_id)s,%(contract_id)s,%(availability_state)s,%(dataset_id)s,%(provider_id)s,
  %(provider_data_version)s,%(lineage_id)s,%(manifest_sha256)s,%(ordered_content_sha256)s,
  %(calendar_id)s,%(source_as_of)s,%(source_observed_at_utc)s,%(available_at_utc)s,
  %(accepted_at_utc)s,%(expected_by_utc)s,%(published_at_utc)s,%(freshness_state)s,
  %(quality_state)s,%(revision_state)s,%(cost_confidence)s,%(warning_ids)s,%(blocker_ids)s,
  %(account_fingerprint)s,%(values_modified)s,%(interpolation_performed)s,%(payload)s::jsonb,
  %(receipt_sha256)s,%(supersedes_receipt_id)s
)
"""


def register_receipts(receipts: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    selected = [dict(receipt) for receipt in receipts]
    for receipt in selected:
        validate_strategy_external_receipt(receipt)
    before = audit_counts()
    inserted = 0
    with connect("saxo_ingest", MARKET_DB, application_name="c2_receipt_register") as conn:
        with conn.transaction():
            with conn.cursor() as cursor:
                for receipt in selected:
                    params = dict(receipt)
                    params["payload"] = json.dumps(
                        receipt["payload"], ensure_ascii=False, sort_keys=True, separators=(",", ":")
                    )
                    cursor.execute(INSERT_SQL, params)
                    inserted += cursor.rowcount
    after = audit_counts()
    if after["receipt_rows"] - before["receipt_rows"] != inserted:
        raise RuntimeError("STRATEGY_EXTERNAL_RECEIPT_COUNT_MISMATCH")
    invariant_keys = {
        key for key in before
        if key not in {"receipt_rows", "ingest_ops_schema_usage", "ingest_receipt_insert"}
        and not key.startswith("reader_receipt_")
        and key != "append_only_trigger_count"
    }
    if any(before[key] != after[key] for key in invariant_keys):
        raise RuntimeError("MARKET_DATA_INVARIANCE_FAILED")
    return {"status": "PASS", "inserted_rows": inserted, "before": before, "after": after}


def _load_receipts(path: Path) -> list[dict[str, Any]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or not isinstance(value.get("receipts"), list):
        raise ValueError("receipt bundle must contain receipts")
    return [dict(item) for item in value["receipts"]]


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build")
    build.add_argument("--nyse-file", type=Path, required=True)
    build.add_argument("--nasdaq-file", type=Path, required=True)
    build.add_argument("--output", type=Path, required=True)
    build.add_argument("--observed-at", required=True)
    issuer = subparsers.add_parser("build-issuer-probe")
    issuer.add_argument("--source-dir", type=Path, required=True)
    issuer.add_argument("--output", type=Path, required=True)
    issuer.add_argument("--observed-at", required=True)
    fee = subparsers.add_parser("build-fee-probe")
    fee.add_argument("--source-file", type=Path, required=True)
    fee.add_argument("--output", type=Path, required=True)
    fee.add_argument("--observed-at", required=True)
    validate = subparsers.add_parser("validate")
    validate.add_argument("--input", type=Path, required=True)
    register = subparsers.add_parser("register")
    register.add_argument("--input", type=Path, required=True)
    subparsers.add_parser("audit")
    args = parser.parse_args()
    if args.command == "build":
        observed = datetime.fromisoformat(args.observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("observed-at must include timezone")
        receipts = build_public_and_blocker_receipts(
            nyse_path=args.nyse_file, nasdaq_path=args.nasdaq_file, observed=observed
        )
        bundle = {
            "schema_version": 1,
            "manifest_relative_path": MANIFEST_RELATIVE_PATH,
            "receipt_count": len(receipts),
            "receipts": receipts,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        result = {"status": "PASS", "receipt_count": len(receipts), "output": str(args.output)}
    elif args.command in {"build-issuer-probe", "build-fee-probe"}:
        observed = datetime.fromisoformat(args.observed_at.replace("Z", "+00:00"))
        if observed.tzinfo is None:
            raise ValueError("observed-at must include timezone")
        if args.command == "build-issuer-probe":
            receipt = build_issuer_probe_receipt(
                source_dir=args.source_dir, observed=observed
            )
        else:
            receipt = build_fee_probe_receipt(
                source_path=args.source_file, observed=observed
            )
        receipts = [receipt]
        bundle = {
            "schema_version": 1,
            "manifest_relative_path": MANIFEST_RELATIVE_PATH,
            "receipt_count": 1,
            "receipts": receipts,
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(bundle, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        result = {"status": "PASS", "receipt_count": 1, "output": str(args.output)}
    elif args.command == "validate":
        receipts = _load_receipts(args.input)
        for receipt in receipts:
            validate_strategy_external_receipt(receipt)
        result = {"status": "PASS", "receipt_count": len(receipts)}
    elif args.command == "register":
        result = register_receipts(_load_receipts(args.input))
    else:
        result = {"status": "PASS", "counts": audit_counts()}
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
