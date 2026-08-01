from __future__ import annotations

from datetime import datetime, timezone

import pytest

from market_db.strategy_external_contract import (
    StrategyExternalContractError,
    validate_strategy_external_receipt,
)
from market_db.strategy_external_receipt_ops import (
    CALENDAR_ID,
    ISSUER_SOURCES,
    SAXO_FEE_SCHEDULE_URL,
    build_fee_probe_receipt,
    build_issuer_probe_receipt,
    build_public_and_blocker_receipts,
)


def _bundle(tmp_path):
    markers = "2026 November 27 December 24 1:00"
    nyse = tmp_path / "nyse.html"
    nasdaq = tmp_path / "nasdaq.html"
    nyse.write_text(markers, encoding="utf-8")
    nasdaq.write_text(markers, encoding="utf-8")
    return build_public_and_blocker_receipts(
        nyse_path=nyse,
        nasdaq_path=nasdaq,
        observed=datetime(2026, 7, 31, 0, 0, tzinfo=timezone.utc),
    )


def test_receipt_builder_accepts_calendar_and_keeps_auth_roles_blocked(tmp_path):
    receipts = _bundle(tmp_path)
    assert len(receipts) == 8
    for receipt in receipts:
        validate_strategy_external_receipt(receipt)
    calendar = receipts[0]
    assert calendar["calendar_id"] == CALENDAR_ID
    assert calendar["availability_state"] == "AVAILABLE_WITH_WARNINGS"
    assert calendar["quality_state"] == "PASS_WITH_WARNINGS"
    assert calendar["values_modified"] is False
    assert calendar["interpolation_performed"] is False
    sessions = calendar["payload"]["sessions"]
    assert len(sessions) == 251
    assert not any(row["session_date"] == "2026-07-03" for row in sessions)
    early = {row["session_date"] for row in sessions if row["early_close"]}
    assert early == {"2026-11-27", "2026-12-24"}
    auth_roles = {
        "DISTRIBUTION_CASH_TRANSACTION", "INSTRUMENT_REFERENCE",
        "PROPOSAL_PRICE_SNAPSHOT", "CURRENCY_AND_AMOUNT_UNIT",
    }
    assert all(
        receipt["availability_state"] == "BLOCKED_INTERFACE_OPERATIONAL"
        for receipt in receipts if receipt["dataset_role"] in auth_roles
    )


def test_receipt_validation_rejects_secret_keys_and_hash_drift(tmp_path):
    receipt = _bundle(tmp_path)[0]
    receipt["payload"]["access_token"] = "never-store"
    with pytest.raises(StrategyExternalContractError):
        validate_strategy_external_receipt(receipt)

    receipt = _bundle(tmp_path)[0]
    receipt["payload"]["TradableOn"] = ["never-store-account-key"]
    with pytest.raises(StrategyExternalContractError):
        validate_strategy_external_receipt(receipt)

    receipt = _bundle(tmp_path)[0]
    receipt["payload"]["calendar_version"] = "tampered"
    with pytest.raises(
        StrategyExternalContractError, match="STRATEGY_EXTERNAL_RECEIPT_HASH_INVALID"
    ):
        validate_strategy_external_receipt(receipt)


def test_issuer_probe_is_complete_but_remains_blocked_without_revision_semantics(tmp_path):
    for instrument_key in ISSUER_SOURCES:
        (tmp_path / f"c2_issuer_{instrument_key}.html").write_bytes(
            f"{instrument_key} official distribution page".encode()
        )
    receipt = build_issuer_probe_receipt(
        source_dir=tmp_path,
        observed=datetime(2026, 7, 31, 1, 0, tzinfo=timezone.utc),
    )
    validate_strategy_external_receipt(receipt)
    assert receipt["availability_state"] == "BLOCKED_EXTERNAL_CONTRACT"
    assert receipt["payload"]["instrument_count"] == 11
    assert receipt["payload"]["all_sources_reachable"] is True
    assert receipt["payload"]["structured_correction_history_verified"] is False
    assert receipt["payload"]["values_extracted_or_accepted"] is False


def test_fee_probe_records_public_source_but_keeps_account_costs_blocked(tmp_path):
    source = tmp_path / "fees.html"
    source.write_text("Official commissions and charges", encoding="utf-8")
    receipt = build_fee_probe_receipt(
        source_path=source,
        observed=datetime(2026, 7, 31, 2, 0, tzinfo=timezone.utc),
    )
    validate_strategy_external_receipt(receipt)
    assert receipt["availability_state"] == "BLOCKED_EXTERNAL_CONTRACT"
    assert receipt["cost_confidence"] == "UNKNOWN"
    assert receipt["payload"]["public_url"] == SAXO_FEE_SCHEDULE_URL
    assert receipt["payload"]["generic_schedule_reachable"] is True
    assert receipt["payload"]["account_specific_applicability_verified"] is False
    assert receipt["payload"]["actual_transaction_read_verified"] is False
    assert receipt["payload"]["values_extracted_or_accepted"] is False
