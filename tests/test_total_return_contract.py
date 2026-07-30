from __future__ import annotations

import json
from datetime import date

import pytest

from market_db.connection import project_root
from market_db.total_return_contract import (
    TotalReturnContractError,
    load_total_return_research_contract,
    load_total_return_research_contracts,
    validate_total_return_research_contract,
)
from market_db.total_return_history import load_full_history_series, select_full_history_rows


def test_repository_fixed_window_contract_is_complete_and_hash_bound():
    contract = load_total_return_research_contract()
    assert contract["contract_id"] == "etf11_fixed_window_20260712_v1"
    assert contract["source_dataset_id"] == "20260712T135236Z"
    assert contract["window"] == {
        "first_session_date": "2004-11-18",
        "last_session_date": "2024-06-28",
        "rows_per_instrument": 4935,
        "freshness_required": False,
        "freshness_status": "NOT_APPLICABLE_FIXED_WINDOW",
    }
    assert len(contract["instruments"]) == 11
    assert contract["instruments"]["EEM"]["availability"] == "AVAILABLE_WITH_WARNINGS"
    assert contract["instruments"]["EEM"]["warning_evidence"] == {
        "quality_warn_rows": 2,
        "automatic_corrections": 0,
    }


def test_contract_rejects_unknown_total_return_definition():
    path = project_root() / "specs/total_return_research_contract_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["total_return_definition"]["definition_id"] = ""
    with pytest.raises(TotalReturnContractError, match="TOTAL_RETURN_DEFINITION_UNKNOWN"):
        validate_total_return_research_contract(payload, root=project_root())


def test_contract_rejects_warning_without_fixed_evidence_count():
    path = project_root() / "specs/total_return_research_contract_v1.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["instruments"]["EEM"]["warning_evidence"]["quality_warn_rows"]
    with pytest.raises(
        TotalReturnContractError,
        match="TOTAL_RETURN_RESEARCH_CONTRACT_WARNING_INVALID",
    ):
        validate_total_return_research_contract(payload, root=project_root())


def test_repository_full_history_contract_is_hash_bound_and_common_to_etf11():
    fixed, full = load_total_return_research_contracts()
    assert fixed["contract_id"] == "etf11_fixed_window_20260712_v1"
    assert full["contract_id"] == "etf11_full_history_20260712_v1"
    assert full["usage_mode"] == "full_history_research"
    assert full["window"] == {
        "first_session_date": "2004-11-18",
        "last_session_date": "2026-07-10",
        "rows_per_instrument": 5443,
        "freshness_required": False,
        "freshness_status": "NOT_APPLICABLE_FROZEN_RESEARCH_SOURCE",
    }
    assert full["range_query"]["strategy_manifest_owns_experiment_boundaries"] is True
    assert full["range_query"]["holdout_specific_contract_required"] is False
    assert len(full["instruments"]) == 11
    for ticker in full["instruments"]:
        history = load_full_history_series(full, ticker)
        assert history["row_count"] == 5443
        assert history["duplicate_count"] == 0
        assert history["null_or_nonpositive_count"] == 0
        assert history["ordered_time_status"] == "PASS"


def test_full_history_contract_supports_strategy_selected_date_range():
    full = load_total_return_research_contracts()[1]
    history = load_full_history_series(full, "SPY")
    rows = select_full_history_rows(
        history,
        start=date.fromisoformat("2024-07-01"),
        end=date.fromisoformat("2026-07-01"),
        after_date=None,
    )
    assert len(rows) == 501
    assert rows[0]["session_date"] == "2024-07-01"
    assert rows[-1]["session_date"] == "2026-06-30"
