from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json

import pytest

import market_db.fx_candidate_onboarding as onboarding
from market_db.fx_candidate_onboarding import (
    _ceil_hour,
    _validate_candidate_history_boundary,
    candidate_instrument,
    candidate_research_contract,
    run_candidate_acceptance,
)
from market_db.incremental_update import _validate_full_refetch_quarantine
from market_db.normalize_bars import (
    BarQualityError,
    CrossedQuoteViolation,
    RejectedBar,
)


def staging_state(*, passes=0, accepted_hour=12):
    return {
        "instrument_key": "audusd",
        "publication_status": "STAGING",
        "quality_status": "PASS",
        "coverage_status": "WARN",
        "freshness_status": "PASS",
        "blocker_code": None,
        "evidence_manifest_relative_path": "data/acquisition/runs/full/run_manifest.json",
        "evidence_manifest_sha256": "a" * 64,
        "last_accepted_complete_time_utc": datetime(
            2026, 7, 27, accepted_hour, tzinfo=timezone.utc
        ),
        "consecutive_normal_passes": passes,
        "last_evaluated_run_id": 900 if passes else None,
        "warning_metadata_json": {"values_modified": False},
    }


def test_candidate_identity_and_first_sample_hour_boundary_are_reviewed():
    assert (candidate_instrument("AUDUSD").uic, candidate_instrument("audusd").asset_type) == (
        4,
        "FxSpot",
    )


def test_candidate_research_contract_freezes_effective_coverage_and_audusd_exception():
    audusd = candidate_research_contract("audusd")
    usdcad = candidate_research_contract("usdcad")
    usdchf = candidate_research_contract("usdchf")

    assert audusd["approved_provider_anomaly"] == {
        **audusd["approved_provider_anomaly"],
        "unique_rows": 14,
        "content_sha256": "c4039ebdef6caadad6f70cdce3d5c909ed88cbc042e362ecd4e58ad42337196e",
        "values_modified": False,
        "exact_baseline_required_for_exception": True,
    }
    assert usdcad["coverage_contract"]["effective_coverage_start_utc"] == (
        "2010-06-18T00:00:00Z"
    )
    assert usdchf["coverage_contract"]["effective_coverage_start_utc"] == (
        "2010-06-18T00:00:00Z"
    )


def test_effective_coverage_contract_accepts_actual_boundary_and_rejects_drift():
    accepted = _validate_candidate_history_boundary(
        "usdcad",
        provider_advertised_start_utc=datetime(2002, 9, 25, 2, 40, tzinfo=timezone.utc),
        observed_start_utc=datetime(2010, 6, 18, tzinfo=timezone.utc),
    )
    assert accepted["pre_effective_history_synthesized"] is False
    assert accepted["effective_coverage_start_utc"] == datetime(
        2010, 6, 18, tzinfo=timezone.utc
    )

    with pytest.raises(BarQualityError, match="EFFECTIVE_HISTORY_TRUNCATED"):
        _validate_candidate_history_boundary(
            "usdcad",
            provider_advertised_start_utc=datetime(
                2002, 9, 25, 2, 40, tzinfo=timezone.utc
            ),
            observed_start_utc=datetime(2010, 6, 18, 1, tzinfo=timezone.utc),
        )


def test_audusd_approved_extrema_exception_requires_exact_unmodified_evidence():
    rows = tuple(
        RejectedBar(
            time_utc=datetime(2013, 1, 1, hour, tzinfo=timezone.utc),
            error_code="FX_BID_ABOVE_ASK",
            violations=(CrossedQuoteViolation("High", Decimal("1.2"), Decimal("1.1")),),
            data_version=7,
            payload_sha256="a" * 64,
            artifact_relative_path="data/raw.json",
        )
        for hour in range(14)
    )
    evidence = [
        {
            "time_utc": row.time_utc.isoformat().replace("+00:00", "Z"),
            "violations": [
                {"field": item.field, "bid": str(item.bid), "ask": str(item.ask)}
                for item in row.violations
            ],
        }
        for row in rows
    ]
    approval = {
        "policy_id": "audusd_test",
        "unique_rows": 14,
        "affected_from_utc": evidence[0]["time_utc"],
        "affected_to_utc": evidence[-1]["time_utc"],
        "allowed_fields": ["High", "Low"],
        "content_sha256": hashlib.sha256(
            json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
        "values_modified": False,
        "exact_baseline_required_for_exception": True,
    }
    accepted = _validate_full_refetch_quarantine(
        (datetime(2014, 1, 1, tzinfo=timezone.utc),),
        rows,
        approved_exception=approval,
    )
    assert len(accepted) == 14

    changed = {**approval, "content_sha256": "0" * 64}
    with pytest.raises(BarQualityError, match="APPROVED_EXCEPTION_MISMATCH"):
        _validate_full_refetch_quarantine(
            (datetime(2014, 1, 1, tzinfo=timezone.utc),),
            rows,
            approved_exception=changed,
        )
    assert _ceil_hour(datetime(2002, 9, 25, 2, 40, tzinfo=timezone.utc)) == datetime(
        2002, 9, 25, 3, 0, tzinfo=timezone.utc
    )


def test_candidate_acceptance_requires_two_isolated_normal_passes(monkeypatch):
    calls = []
    publications = []
    run_ids = iter((901, 902))
    state = staging_state()
    observed_hours = iter((13, 14))
    monkeypatch.setattr(onboarding, "_publication_snapshot", lambda _key: dict(state))
    monkeypatch.setattr(
        onboarding,
        "_publication_watermark",
        lambda _key: {
            "latest_complete_time_utc": datetime(
                2026, 7, 27, next(observed_hours), tzinfo=timezone.utc
            )
        },
    )

    def fake_run_incremental(*, client, instrument_keys, trigger):
        calls.append((instrument_keys, trigger, client))
        return {
            "status": "PASS",
            "error_code": None,
            "database_ingestion_run_id": next(run_ids),
            "orders_or_prechecks_sent": 0,
        }

    monkeypatch.setattr(onboarding, "run_incremental", fake_run_incremental)
    def update(key, **kwargs):
        publications.append((key, kwargs))
        state.update({
            "publication_status": kwargs["publication_status"],
            "consecutive_normal_passes": kwargs["consecutive_normal_passes"],
            "last_accepted_complete_time_utc": kwargs["last_accepted_complete_time_utc"],
            "last_evaluated_run_id": kwargs["run_id"],
            "warning_metadata_json": kwargs["warning_metadata"],
        })

    monkeypatch.setattr(onboarding, "_update_publication", update)

    first = run_candidate_acceptance("audusd", client_factory=lambda: object())
    second = run_candidate_acceptance("audusd", client_factory=lambda: object())

    assert first["status"] == "PASS"
    assert first["publication_status"] == "STAGING"
    assert first["normal_pass_run_ids"] == [901]
    assert second["status"] == "PASS"
    assert second["publication_status"] == "PUBLISHED"
    assert second["normal_pass_run_ids"] == [901, 902]
    assert publications[1][1]["warning_metadata"]["normal_acceptance_runs"] == [
        {
            "database_ingestion_run_id": 901,
            "accepted_complete_time_utc": "2026-07-27T13:00:00Z",
        },
        {
            "database_ingestion_run_id": 902,
            "accepted_complete_time_utc": "2026-07-27T14:00:00Z",
        },
    ]
    assert all(call[0] == ("audusd",) for call in calls)
    assert all("usdjpy" not in call[0] for call in calls)
    assert [row[1]["publication_status"] for row in publications] == [
        "STAGING", "PUBLISHED",
    ]
    assert [row[1]["consecutive_normal_passes"] for row in publications] == [1, 2]
    assert second["orders_or_prechecks_sent"] == 0


def test_candidate_acceptance_failure_blocks_only_selected_pair(monkeypatch):
    publications = []
    monkeypatch.setattr(onboarding, "_publication_snapshot", lambda _key: staging_state())
    monkeypatch.setattr(
        onboarding,
        "run_incremental",
        lambda **_kwargs: {
            "status": "BLOCKED",
            "error_code": "BLOCKED_FULL_REFETCH_REQUIRED",
            "database_ingestion_run_id": 903,
        },
    )
    monkeypatch.setattr(
        onboarding,
        "_update_publication",
        lambda key, **kwargs: publications.append((key, kwargs)),
    )

    result = run_candidate_acceptance("audusd", client_factory=lambda: object())

    assert result["status"] == "BLOCKED"
    assert publications[0][0] == "audusd"
    assert publications[0][1]["publication_status"] == "BLOCKED"
    assert publications[0][1]["consecutive_normal_passes"] == 0
    assert result["orders_or_prechecks_sent"] == 0


def test_candidate_acceptance_without_new_complete_bar_is_data_not_ready(monkeypatch):
    initial = staging_state()
    monkeypatch.setattr(onboarding, "_publication_snapshot", lambda _key: initial)
    monkeypatch.setattr(
        onboarding,
        "run_incremental",
        lambda **_kwargs: {"status": "PASS", "database_ingestion_run_id": 904},
    )
    monkeypatch.setattr(
        onboarding,
        "_publication_watermark",
        lambda _key: {
            "latest_complete_time_utc": initial["last_accepted_complete_time_utc"]
        },
    )
    updates = []
    monkeypatch.setattr(onboarding, "_update_publication", lambda *_args, **_kwargs: updates.append(1))

    result = run_candidate_acceptance("audusd", client_factory=lambda: object())

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "DATA_NOT_READY_CANDIDATE_WATERMARK_NOT_ADVANCED"
    assert result["publication_status"] == "STAGING"
    assert result["consecutive_normal_passes"] == 0
    assert updates == []


def test_cli_reports_missing_auth_as_sanitized_operational_block(monkeypatch, capsys):
    class MissingAuth(onboarding.SaxoAuthError):
        pass

    monkeypatch.setattr(
        onboarding,
        "_client_factory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(MissingAuth("AUTH_CONFIG_MISSING")),
    )

    status = onboarding.main(["onboard", "--instrument-key", "audusd", "--auth-mode", "keychain"])
    payload = json.loads(capsys.readouterr().out)

    assert status == 1
    assert payload == {
        "error_code": "AUTH_CONFIG_MISSING",
        "error_domain": "interface_operational",
        "instrument_key": "audusd",
        "orders_or_prechecks_sent": 0,
        "status": "BLOCKED",
        "token_values_exposed": False,
    }
