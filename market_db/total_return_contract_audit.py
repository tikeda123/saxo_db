"""Read-only audit of repository-bound ETF11 total-return research contracts."""

from __future__ import annotations

import argparse
import json
from typing import Sequence

from .read_api import DatabaseReader, json_value, total_return_status_payload
from .total_return_contract import load_total_return_research_contracts


def run(contract_id: str | None = None) -> dict[str, object]:
    contracts = load_total_return_research_contracts()
    selected_id = contract_id or contracts[0]["contract_id"]
    contract = next(
        (item for item in contracts if item["contract_id"] == selected_id), None
    )
    if contract is None:
        raise ValueError("TOTAL_RETURN_RESEARCH_CONTRACT_NOT_FOUND")
    reader = DatabaseReader()
    try:
        series = {
            ticker: total_return_status_payload(
                reader,
                instrument_key=ticker.lower(),
                research_contract_id=contract["contract_id"],
            )
            for ticker in contract["instruments"]
        }
    finally:
        reader.close()
    statuses = {
        ticker: payload["state"]["availability_status"]
        for ticker, payload in series.items()
    }
    blocked = [ticker for ticker, status in statuses.items() if status == "BLOCKED"]
    return {
        "status": "PASS" if not blocked else "BLOCKED",
        "contract_id": contract["contract_id"],
        "source_dataset_id": contract["source_dataset_id"],
        "usage_mode": contract["usage_mode"],
        "series_count": len(series),
        "blocked_instruments": blocked,
        "acquisition_requests": 0,
        "orders_or_prechecks_sent": 0,
        "database_writes": 0,
        "series": series,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Audit one repository-bound ETF11 research contract read-only"
    )
    parser.add_argument("--full", action="store_true", help="include complete per-series contract evidence")
    parser.add_argument("--contract-id", help="audit one repository-bound research contract")
    args = parser.parse_args(argv)
    result = run(args.contract_id)
    output = result
    if not args.full:
        output = {
            key: value for key, value in result.items() if key != "series"
        }
        output["series"] = {
            ticker: {
                "availability_status": payload["state"]["availability_status"],
                "quality_status": payload["state"]["quality_status"],
                "coverage_status": payload["state"]["coverage_status"],
                "freshness_status": payload["state"]["freshness_status"],
                "current_blockers": payload["state"]["current_blockers"],
                "row_count": payload["evidence"]["row_count"],
                "first_session_date": payload["evidence"]["first_session_date"],
                "last_session_date": payload["evidence"]["last_session_date"],
                "quality_warn_count": payload["evidence"]["quality_warn_count"],
            }
            for ticker, payload in result["series"].items()
        }
    print(json.dumps(json_value(output), ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
