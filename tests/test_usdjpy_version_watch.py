from __future__ import annotations

import inspect

from market_db import usdjpy_version_watch as watch


def test_same_quarantined_version_does_not_retain_or_allow_refetch() -> None:
    result = watch.classify_version(29738069, accepted_data_version=29738065)

    assert result == {
        "status": "NO_CHANGE_QUARANTINE_MAINTAINED",
        "new_data_version_detected": False,
        "retain_probe_artifact": False,
        "guarded_full_refetch": "NOT_PERMITTED_SAME_QUARANTINED_VERSION",
    }


def test_new_version_requires_separate_operator_decision() -> None:
    result = watch.classify_version(29760000, accepted_data_version=29738065)

    assert result["status"] == "NEW_PROVIDER_DATA_VERSION_REVIEW_REQUIRED"
    assert result["retain_probe_artifact"] is True
    assert result["guarded_full_refetch"] == "ELIGIBLE_FOR_SEPARATE_OPERATOR_DECISION"


def test_repeated_pending_version_does_not_retain_duplicate_probe() -> None:
    result = watch.classify_version(
        29749254,
        accepted_data_version=29738065,
        last_observed_data_version=29749254,
    )

    assert result["status"] == "NO_CHANGE_REVISION_REVIEW_PENDING"
    assert result["retain_probe_artifact"] is False
    assert result["guarded_full_refetch"] == "ELIGIBLE_FOR_SEPARATE_OPERATOR_DECISION"


def test_probe_has_no_full_refetch_or_database_writer_path() -> None:
    source = inspect.getsource(watch.probe)

    assert "count=1" in source
    assert "run_full_refetch" not in source
    assert "saxo_ingest" not in source
    assert "run_bounded_revision_reconcile" not in source
