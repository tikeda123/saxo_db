from __future__ import annotations

from market_db.total_return_update import (
    classify_provider_error,
    evaluate_total_return_batch,
    provider_gate,
    revision_keys,
)


def row(ticker="SPY", date="2026-07-23", **values):
    return {
        "ticker": ticker,
        "date": date,
        "close_unadjusted": "100",
        "adjusted_close": "99",
        "dividend_cash": "0",
        "split_factor": "1",
        "provider_revision": "revision-1",
        **values,
    }


def test_provider_gate_keeps_development_snapshot_out_of_current_operation():
    result = provider_gate()
    assert result["status"] == "BLOCKED_SOURCE_PROVIDER_NOT_CONFIGURED"
    assert result["scheduled"] is False
    assert result["operator_decision_required"] is True
    assert result["development_dataset_promoted"] is False


def test_total_return_batch_rejects_duplicate_and_date_reversal():
    result = evaluate_total_return_batch(
        [row(date="2026-07-23"), row(date="2026-07-22"), row(date="2026-07-22")]
    )
    assert result["status"] == "FAIL"
    assert result["duplicate_count"] == 1
    assert any(value.startswith("DATE_REVERSAL:") for value in result["errors"])


def test_total_return_batch_rejects_invalid_corporate_action_and_provider_revision():
    result = evaluate_total_return_batch(
        [row(dividend_cash="-1", split_factor="0", provider_revision="")]
    )
    assert result["status"] == "FAIL"
    assert "INVALID_DIVIDEND:0" in result["errors"]
    assert "INVALID_SPLIT_FACTOR:0" in result["errors"]
    assert "PROVIDER_REVISION_MISSING:0" in result["errors"]


def test_total_return_revision_is_explicit_and_ordered_hash_is_stable():
    before = [row(), row(ticker="IWM")]
    after = [row(), row(ticker="IWM", adjusted_close="98")]
    assert revision_keys(before, after) == ("IWM:2026-07-23",)
    assert evaluate_total_return_batch(before)["status"] == "PASS"
    assert (
        evaluate_total_return_batch(before)["ordered_content_sha256"]
        == evaluate_total_return_batch(list(before))["ordered_content_sha256"]
    )


def test_total_return_provider_error_is_not_misclassified_as_data_quality():
    auth = classify_provider_error(401)
    unavailable = classify_provider_error(503)
    assert auth["error_domain"] == "interface_auth"
    assert unavailable["error_domain"] == "interface_operational"
    assert auth["quality_status"] == unavailable["quality_status"] == "NOT_EVALUATED"
    assert auth["publish_current_dataset"] is False
