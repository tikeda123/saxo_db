from __future__ import annotations

from datetime import datetime, timezone

from market_db.fx_gap_report import build_summary, classify_gap, normalize_gap, render_markdown


def gap(**overrides):
    row = {
        "instrument_key": "eurusd",
        "time_utc": datetime(2024, 1, 8, 12, tzinfo=timezone.utc),
        "raw_present": False,
        "curated_rejected": False,
        "quarantined": False,
        "covered_by_successful_raw_run": True,
    }
    row.update(overrides)
    return row


def test_gap_classification_uses_retained_evidence_without_price_synthesis():
    assert classify_gap(gap(quarantined=True)) == "QUARANTINED_VALUE_ANOMALY"
    assert classify_gap(gap(raw_present=True)) == "RAW_PRESENT_CURATED_REJECTED"
    assert classify_gap(
        gap(
            raw_present=True,
            raw_sample_run_id=100,
            covering_successful_run_id=200,
        )
    ) == "SAXO_RAW_NO_SAMPLE"
    assert classify_gap(
        gap(
            raw_present=True,
            raw_sample_run_id=200,
            covering_successful_run_id=200,
        )
    ) == "RAW_PRESENT_CURATED_REJECTED"
    assert classify_gap(gap(covered_by_successful_raw_run=True)) == "SAXO_RAW_NO_SAMPLE"
    assert classify_gap(gap(covered_by_successful_raw_run=False)) == "ACQUISITION_RUN_MISSED"
    # Sunday evening in New York is a valid Monday FX session, not a guessed closure.
    assert classify_gap(gap(time_utc=datetime(2024, 1, 7, 23, tzinfo=timezone.utc))) == "SAXO_RAW_NO_SAMPLE"
    assert classify_gap(gap(closed_session=True)) == "WEEKEND_OR_HOLIDAY_CLOSURE"


def test_normalized_gap_records_superseded_raw_lineage():
    observed = normalize_gap(
        gap(
            raw_present=True,
            raw_sample_run_id=100,
            covering_successful_run_id=200,
        )
    )
    assert observed["cause_code"] == "SAXO_RAW_NO_SAMPLE"
    assert observed["raw_sample_superseded_by_run_id"] == 200
    assert observed["blocking"] is False


def test_gap_summary_accounts_for_common_and_pair_only_slots():
    common_time = datetime(2024, 1, 8, 12, tzinfo=timezone.utc)
    normalized = [
        normalize_gap(gap(instrument_key="eurusd", time_utc=common_time)),
        normalize_gap(gap(instrument_key="usdjpy", time_utc=common_time)),
        normalize_gap(gap(instrument_key="usdjpy", time_utc=datetime(2024, 1, 8, 13, tzinfo=timezone.utc))),
    ]
    summary = build_summary(normalized)

    assert summary["per_instrument"]["eurusd"]["missing_rows"] == 1
    assert summary["per_instrument"]["usdjpy"]["missing_rows"] == 2
    assert summary["cross_instrument"] == {
        "common_missing_rows": 1,
        "only_rows": {"eurusd": 0, "usdjpy": 1},
    }
    assert summary["interpolation_performed"] is False
    assert summary["orders_or_prechecks_sent"] == 0
    markdown = render_markdown(summary, "a" * 64)
    assert "Price interpolation: **not performed**" in markdown
    assert "EURUSD | 1" in markdown


def test_single_candidate_summary_and_uncovered_slot_fail_closed():
    normalized = [normalize_gap(gap(
        instrument_key="audusd",
        covered_by_successful_raw_run=False,
    ))]
    summary = build_summary(normalized, ("audusd",))

    assert summary["cross_instrument"] == {
        "common_missing_rows": 1,
        "only_rows": {"audusd": 1},
    }
    assert summary["per_instrument"]["audusd"]["blocking_rows"] == 1
    assert normalized[0]["cause_code"] == "ACQUISITION_RUN_MISSED"
    assert normalized[0]["blocking"] is True
