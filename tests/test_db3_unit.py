from __future__ import annotations

import json
import inspect
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import market_db.incremental_update as incremental_update
import market_db.raw_artifacts as raw_artifacts
from market_db.acquire_pages import fetch_chart_pages
from market_db.incremental_update import (
    S6V5A_PRIORITY_INSTRUMENT_KEYS,
    _failed_instrument_context,
    _error_code,
    _quarantined_row_evidence,
    _record_revision_warning,
    _records_quality_event,
    _revision_bar_content,
    _validate_full_refetch_quarantine,
    compare_revision_sample,
    reconcile_incremental,
    oauth_reconcile_runners,
    select_instruments,
)
from market_db.instrument_registry import (
    InstrumentDriftError,
    load_canonical_instruments,
    load_research_candidate_instruments,
    validate_detail,
)
from market_db.normalize_bars import (
    BarQualityError,
    CrossedQuoteViolation,
    RejectedBar,
    mark_terminal_session_bar_complete,
    merge_pages,
    normalize_chart_page,
    normalize_chart_page_quarantining_fx_extrema,
)
from market_db.raw_artifacts import RunArtifacts, canonical_json_bytes
from market_db.saxo_client import HTTPResponse, SIM_BASE_URL, SaxoAPIError, SaxoClient
from market_db.saxo_auth import SaxoAuthError
from market_db.session_calendar import generate_equity_sessions
from market_db.validate import db3_manifest_baseline_is_valid


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, timeout):
        self.calls.append((method, url, headers, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


def test_candidate_dataset_metadata_is_stable_across_singleton_runs(
    monkeypatch, tmp_path
):
    spec = tmp_path / "candidate.json"
    spec.write_text("{}", encoding="utf-8")

    class Cursor:
        def execute(self, _query, params):
            self.params = params

    cursor = Cursor()
    monkeypatch.setattr(incremental_update, "project_root", lambda: tmp_path)

    incremental_update._ensure_dataset(
        cursor,
        dataset_id=incremental_update.CANDIDATE_DATASET_ID,
        spec_relative_path=spec.relative_to(tmp_path),
        dataset_name="candidate",
        research_eligibility="SIM_RESEARCH_CANDIDATE",
        instrument_count=1,
    )

    metadata = cursor.params[-1].obj
    assert metadata == {
        "instrument_count": 3,
        "horizon_minutes": 60,
        "write_endpoints": 0,
        "research_warning_policy_id": (
            "fx_research_candidate_user_approved_warnings_v1"
        ),
        "consumer_availability_status": "AVAILABLE_WITH_WARNINGS",
        "value_repair": False,
        "interpolation": False,
    }


def test_oauth_reconcile_refreshes_before_each_full_refetch(monkeypatch):
    calls: list[bool] = []

    class FakeOAuthManager:
        def access_token(self, *, force_refresh: bool = False) -> str:
            calls.append(force_refresh)
            return f"access-{len(calls)}"

    def fake_incremental(*, client_factory):
        return {"status": "PASS", "client": client_factory()}

    def fake_full_refetch(instrument_key, *, client_factory):
        return {
            "status": "PASS",
            "instrument_key": instrument_key,
            "client": client_factory(),
        }

    monkeypatch.setattr(incremental_update, "run_incremental", fake_incremental)
    monkeypatch.setattr(incremental_update, "run_full_refetch", fake_full_refetch)
    normal, full_refetch = oauth_reconcile_runners(FakeOAuthManager())

    normal_result = normal()
    first_refetch = full_refetch("spy")
    second_refetch = full_refetch("iwm")

    assert calls == [False, True, True]
    assert normal_result["client"]._access_token == "access-1"
    assert first_refetch["client"]._access_token == "access-2"
    assert second_refetch["client"]._access_token == "access-3"


def test_keychain_full_refetch_is_single_instrument_and_force_refreshes(
    monkeypatch, capsys
):
    observed: dict[str, object] = {}

    class FakeConfig:
        @staticmethod
        def from_environment(*, callback_port: int):
            observed["callback_port"] = callback_port
            return object()

    class FakeOAuthManager:
        def __init__(self, _config):
            pass

        def access_token(self, *, force_refresh: bool = False) -> str:
            observed["force_refresh"] = force_refresh
            return "memory-only-test-token"

    def fake_full_refetch(instrument_key, *, client_factory):
        client = client_factory()
        observed["instrument_key"] = instrument_key
        observed["client_token"] = client._access_token
        return {"status": "PASS", "instrument_key": instrument_key}

    monkeypatch.setattr(incremental_update, "OAuthConfig", FakeConfig)
    monkeypatch.setattr(incremental_update, "SaxoOAuthManager", FakeOAuthManager)
    monkeypatch.setattr(incremental_update, "run_full_refetch", fake_full_refetch)

    status = incremental_update.main(
        [
            "full-refetch",
            "--instrument-key",
            "eurusd",
            "--auth-mode",
            "keychain",
            "--callback-port",
            "8765",
        ]
    )

    assert status == 0
    assert observed == {
        "callback_port": 8765,
        "force_refresh": True,
        "instrument_key": "eurusd",
        "client_token": "memory-only-test-token",
    }
    assert "memory-only-test-token" not in capsys.readouterr().out


def test_saxo_auth_error_keeps_operational_classification():
    error = SaxoAuthError("AUTH_LOGIN_REQUIRED")
    assert _error_code(error) == "AUTH_LOGIN_REQUIRED"
    assert not _records_quality_event(_error_code(error))


def chart_payload(times, *, fx=False, data_version=10):
    data = []
    for index, value in enumerate(times):
        if fx:
            data.append(
                {
                    "Time": value,
                    "OpenBid": "1.1000",
                    "HighBid": "1.1020",
                    "LowBid": "1.0990",
                    "CloseBid": "1.1010",
                    "OpenAsk": "1.1002",
                    "HighAsk": "1.1022",
                    "LowAsk": "1.0992",
                    "CloseAsk": "1.1012",
                }
            )
        else:
            data.append(
                {
                    "Time": value,
                    "Open": str(100 + index),
                    "High": str(102 + index),
                    "Low": str(99 + index),
                    "Close": str(101 + index),
                    "Volume": index,
                }
            )
    return {"ChartInfo": {"Horizon": 60, "DelayedByMinutes": 0}, "DataVersion": data_version, "Data": data}


def test_revision_sample_is_counts_only_and_does_not_decide_or_apply_repair():
    instrument = next(item for item in load_canonical_instruments() if item.key == "spy")
    payload = chart_payload(
        ["2026-07-27T17:30:00Z", "2026-07-27T18:30:00Z"],
        data_version=20,
    )
    provider = normalize_chart_page(
        instrument,
        payload,
        retrieved_at_utc=datetime(2026, 7, 27, 20, tzinfo=timezone.utc),
        payload_sha256="a" * 64,
        artifact_relative_path="data/acquisition/test.json",
    )
    stored = {
        provider[0].time_utc: (_revision_bar_content(provider[0]), 19),
    }

    result = compare_revision_sample(provider, stored)

    assert result["provider_rows"] == 2
    assert result["matched_rows"] == 1
    assert result["version_only_rows"] == 1
    assert result["content_difference_rows"] == 0
    assert result["new_rows"] == 1
    assert "decision" not in result
    assert "affected_from_utc" not in result


def test_revision_warning_writer_cannot_mutate_accepted_or_derived_data():
    source = inspect.getsource(_record_revision_warning)
    for forbidden in (
        "staging.market_bar",
        "raw.market_bar_revision",
        "curated.market_bar",
        "ops.watermark",
        "derived.market_bar_4h",
        "derived.market_bar_1d",
        "prepare_bounded_revision",
        "prepare_full_refetch",
    ):
        assert forbidden not in source
    assert "revision_warning_recorded_no_curated_change" in source
    assert "WARNING_RECORDED" in source
    assert "ops.ingestion_run_instrument_scope\n                    WHERE ingestion_run_id=%s AND instrument_key=%s\n                    FOR UPDATE" not in source


def rejected_bar(time_utc, *, field="Low", bid="1.11749", ask="1.11726"):
    return RejectedBar(
        time_utc=time_utc,
        error_code="FX_BID_ABOVE_ASK",
        violations=(CrossedQuoteViolation(field, Decimal(bid), Decimal(ask)),),
        data_version=29_732_293,
        payload_sha256="f" * 64,
        artifact_relative_path="data/acquisition/runs/example/instruments/eurusd/chart_0053.json",
    )


def test_saxo_client_is_sim_get_only_redacted_and_retries_429():
    token = "session-secret-token"
    transport = FakeTransport(
        [
            HTTPResponse(429, {"X-RateLimit-Reset": "1"}, b"{}"),
            HTTPResponse(200, {"X-RateLimit-Remaining": "119"}, b'{"ok":true}'),
        ]
    )
    waits = []
    client = SaxoClient(token, transport=transport, sleep=waits.append)
    assert client.smoke_test()["body_saved"] is False
    assert waits == [1.0]
    assert client.request_count == 2
    assert client.write_request_count == 0
    assert token not in repr(client)
    assert all(call[0] == "GET" and call[1].startswith(SIM_BASE_URL) for call in transport.calls)
    assert all(token not in call[1] for call in transport.calls)


def test_saxo_client_retries_get_network_errors_with_a_finite_limit():
    waits = []
    transport = FakeTransport(
        [
            TimeoutError("sensitive transport detail"),
            OSError("sensitive transport detail"),
            HTTPResponse(200, {}, b'{"ok":true}'),
        ]
    )
    client = SaxoClient("secret", transport=transport, sleep=waits.append)
    assert client.smoke_test()["http_status"] == 200
    assert waits == [1.0, 2.0]
    assert len(transport.calls) == 3
    assert client.request_count == 1

    exhausted_waits = []
    exhausted = SaxoClient(
        "secret",
        transport=FakeTransport([TimeoutError("secret")] * 4),
        sleep=exhausted_waits.append,
    )
    with pytest.raises(SaxoAPIError, match="FAILED_NETWORK") as captured:
        exhausted.smoke_test()
    assert exhausted_waits == [1.0, 2.0, 4.0]
    assert "secret" not in str(captured.value)


def test_saxo_client_sanitizes_http_errors_and_blocks_non_sim():
    client = SaxoClient("secret", transport=FakeTransport([HTTPResponse(401, {}, b"secret details")]))
    with pytest.raises(SaxoAPIError, match="BLOCKED_TOKEN_EXPIRED") as captured:
        client.smoke_test()
    assert "secret details" not in str(captured.value)
    with pytest.raises(ValueError, match="only the Saxo SIM"):
        SaxoClient("secret", base_url="https://gateway.saxobank.com/openapi")


def test_saxo_external_contract_reads_are_get_only_and_allow_listed():
    transport = FakeTransport([HTTPResponse(200, {}, b'{"Data":[]}')] * 5)
    client = SaxoClient("secret", transport=transport)
    client.accounts_me()
    client.balances_me()
    client.session_capabilities()
    client.historical_transactions(
        from_date="2026-07-01", to_date="2026-07-31", uics=[36590],
        transaction_type="CorporateAction",
    )
    client.info_prices(uics=[36590], asset_type="Etf", amount=1)
    assert client.request_count == 5
    assert client.write_request_count == 0
    assert all(call[0] == "GET" for call in transport.calls)
    assert all(call[1].startswith(SIM_BASE_URL) for call in transport.calls)
    assert any("%24top=1000" in call[1] for call in transport.calls)
    assert any("DelayedByMinutes" not in call[1] for call in transport.calls)


def test_artifacts_are_atomic_relative_and_sensitive_fields_are_removed(tmp_path, monkeypatch):
    monkeypatch.setattr(raw_artifacts, "project_root", lambda: tmp_path)
    run = RunArtifacts("20260717T000000Z-deadbeef")
    record = run.write_json(
        "instruments/spy/chart.json",
        {"Data": [{"x": Decimal("1.25")}], "AccountKey": "drop", "nested": {"ClientKey": "drop"}},
        row_count=1,
    )
    payload = json.loads((tmp_path / record.relative_path).read_text(encoding="utf-8"))
    assert payload == {"Data": [{"x": "1.25"}], "nested": {}}
    assert not list(tmp_path.rglob("*.tmp"))
    with pytest.raises(ValueError, match="run-relative"):
        run.write_json("../escape.json", {}, row_count=0)
    assert b"authorization" not in canonical_json_bytes({"Authorization": "drop"}).lower()


def test_registry_drift_is_blocked_without_substitution():
    spy = load_canonical_instruments()[0]
    observed = validate_detail(
        spy,
        {"Identifier": spy.uic, "AssetType": spy.asset_type, "Symbol": spy.symbol, "CurrencyCode": "USD"},
    )
    assert observed["uic"] == spy.uic
    with pytest.raises(InstrumentDriftError, match="BLOCKED_INSTRUMENT_DRIFT"):
        validate_detail(
            spy,
            {"Identifier": spy.uic + 1, "AssetType": spy.asset_type, "Symbol": spy.symbol, "CurrencyCode": "USD"},
        )


def test_s6v5a_profile_is_exact_ordered_canonical_subset():
    selected = select_instruments(S6V5A_PRIORITY_INSTRUMENT_KEYS)
    assert tuple(item.key for item in selected) == (
        "spy", "iwm", "efa", "eem", "vnq", "eurusd"
    )
    assert tuple(item.price_basis for item in selected) == (
        "native_ohlc", "native_ohlc", "native_ohlc",
        "native_ohlc", "native_ohlc", "bid_ask_mid",
    )
    with pytest.raises(ValueError, match="unreviewed"):
        select_instruments(("spy", "replacement"))
    with pytest.raises(ValueError, match="unique"):
        select_instruments(("spy", "spy"))


def test_fx_research_candidate_registry_is_exact_and_cannot_mix_with_canonical():
    candidates = load_research_candidate_instruments()
    assert [(item.key, item.uic, item.asset_type) for item in candidates] == [
        ("audusd", 4, "FxSpot"),
        ("usdcad", 38, "FxSpot"),
        ("usdchf", 39, "FxSpot"),
    ]
    assert tuple(item.key for item in select_instruments(("audusd",))) == ("audusd",)
    with pytest.raises(ValueError, match="must run separately"):
        select_instruments(("eurusd", "audusd"))


def test_etf_and_fx_decimal_normalization_and_latest_incomplete():
    instruments = load_canonical_instruments()
    spy = instruments[0]
    eurusd = next(item for item in instruments if item.key == "eurusd")
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    times = ["2026-07-16T10:00:00Z", "2026-07-16T11:00:00Z"]
    etf = normalize_chart_page(
        spy, chart_payload(times), retrieved_at_utc=now, payload_sha256="0" * 64,
        artifact_relative_path="data/acquisition/a.json",
    )
    fx = normalize_chart_page(
        eurusd, chart_payload(times, fx=True), retrieved_at_utc=now, payload_sha256="1" * 64,
        artifact_relative_path="data/acquisition/b.json",
    )
    merged = merge_pages([etf])
    assert [bar.is_complete for bar in merged] == [True, False]
    assert fx[0].open == Decimal("1.1001")
    assert fx[0].open_bid == Decimal("1.1000")
    assert fx[0].open_ask == Decimal("1.1002")


def test_merge_pages_keeps_first_seen_complete_up_to_boundary_sample():
    spy = load_canonical_instruments()[0]
    now = datetime(2026, 7, 17, tzinfo=timezone.utc)
    newer_payload = chart_payload(
        [
            "2026-07-16T02:00:00Z",
            "2026-07-16T03:00:00Z",
            "2026-07-16T04:00:00Z",
        ]
    )
    older_payload = chart_payload(
        [
            "2026-07-16T00:00:00Z",
            "2026-07-16T01:00:00Z",
            "2026-07-16T02:00:00Z",
        ]
    )
    # Saxo can return a partial copy as the last row of the inclusive older
    # UpTo page.  Keep the first-seen full bar from the newer window.
    older_payload["Data"][-1].update(
        {"Open": "100", "High": "100.5", "Low": "99.5", "Close": "100.1"}
    )
    newer = normalize_chart_page(
        spy,
        newer_payload,
        retrieved_at_utc=now,
        payload_sha256="0" * 64,
        artifact_relative_path="data/acquisition/newer.json",
    )
    older = normalize_chart_page(
        spy,
        older_payload,
        retrieved_at_utc=now,
        payload_sha256="1" * 64,
        artifact_relative_path="data/acquisition/older.json",
    )

    merged = merge_pages([newer, older])
    boundary = next(bar for bar in merged if bar.time_utc.hour == 2)

    assert boundary.close == Decimal("101")
    assert boundary.artifact_relative_path == "data/acquisition/newer.json"
    assert boundary.is_complete is True


def test_terminal_session_bar_completes_only_after_close_plus_provider_delay():
    spy = load_canonical_instruments()[0]
    payload = chart_payload(["2026-07-31T19:30:00Z"])
    payload["ChartInfo"]["DelayedByMinutes"] = 15
    before_close = normalize_chart_page(
        spy,
        payload,
        retrieved_at_utc=datetime(2026, 7, 31, 20, 14, 59, tzinfo=timezone.utc),
        payload_sha256="a" * 64,
        artifact_relative_path="data/acquisition/terminal-before.json",
    )
    after_close = normalize_chart_page(
        spy,
        payload,
        retrieved_at_utc=datetime(2026, 7, 31, 20, 15, tzinfo=timezone.utc),
        payload_sha256="b" * 64,
        artifact_relative_path="data/acquisition/terminal-after.json",
    )

    before = mark_terminal_session_bar_complete(
        merge_pages([before_close]),
        session_open_utc=datetime(2026, 7, 31, 13, 30, tzinfo=timezone.utc),
        session_close_utc=datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc),
    )
    after_merged = merge_pages([after_close])
    after = mark_terminal_session_bar_complete(
        after_merged,
        session_open_utc=datetime(2026, 7, 31, 13, 30, tzinfo=timezone.utc),
        session_close_utc=datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc),
    )

    assert before[0].is_complete is False
    assert after[0].is_complete is True
    assert after[0].open == after_merged[0].open
    assert after[0].high == after_merged[0].high
    assert after[0].low == after_merged[0].low
    assert after[0].close == after_merged[0].close
    assert after[0].volume == after_merged[0].volume
    assert after[0].data_version == after_merged[0].data_version
    assert after[0].payload_sha256 == after_merged[0].payload_sha256


def test_terminal_session_completion_fails_closed_without_exact_evidence():
    spy = load_canonical_instruments()[0]
    payload = chart_payload(["2026-07-31T18:30:00Z"])
    payload["ChartInfo"].pop("DelayedByMinutes")
    bars = merge_pages([
        normalize_chart_page(
            spy,
            payload,
            retrieved_at_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
            payload_sha256="c" * 64,
            artifact_relative_path="data/acquisition/non-terminal.json",
        )
    ])

    completed = mark_terminal_session_bar_complete(
        bars,
        session_open_utc=datetime(2026, 7, 31, 13, 30, tzinfo=timezone.utc),
        session_close_utc=datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc),
    )

    assert completed[0].is_complete is False

    payload["ChartInfo"]["DelayedByMinutes"] = 0
    wrong_slot = merge_pages([
        normalize_chart_page(
            spy,
            payload,
            retrieved_at_utc=datetime(2026, 8, 1, tzinfo=timezone.utc),
            payload_sha256="d" * 64,
            artifact_relative_path="data/acquisition/wrong-terminal.json",
        )
    ])
    still_incomplete = mark_terminal_session_bar_complete(
        wrong_slot,
        session_open_utc=datetime(2026, 7, 31, 13, 30, tzinfo=timezone.utc),
        session_close_utc=datetime(2026, 7, 31, 20, 0, tzinfo=timezone.utc),
    )

    assert still_incomplete[0].is_complete is False


def test_fx_bid_above_ask_and_wrong_horizon_fail_quality_gate():
    eurusd = next(item for item in load_canonical_instruments() if item.key == "eurusd")
    payload = chart_payload(["2026-07-16T10:00:00Z"], fx=True)
    payload["Data"][0]["OpenBid"] = "1.1003"
    with pytest.raises(BarQualityError, match="FX_BID_ABOVE_ASK"):
        normalize_chart_page(
            eurusd, payload, retrieved_at_utc=datetime.now(timezone.utc), payload_sha256="0" * 64,
            artifact_relative_path="data/acquisition/fail.json",
        )


def test_fx_side_ohlc_violation_is_rejected_even_when_midpoint_ohlc_is_valid():
    eurusd = next(item for item in load_canonical_instruments() if item.key == "eurusd")
    payload = chart_payload(["2026-07-16T10:00:00Z"], fx=True)
    payload["Data"][0].update({
        "HighBid": "1.1005",
        "CloseBid": "1.1010",
        "HighAsk": "1.1035",
        "CloseAsk": "1.1012",
    })
    with pytest.raises(BarQualityError, match="OHLC_VIOLATION"):
        normalize_chart_page(
            eurusd,
            payload,
            retrieved_at_utc=datetime.now(timezone.utc),
            payload_sha256="0" * 64,
            artifact_relative_path="data/acquisition/side-invalid.json",
        )


def test_full_refetch_quarantines_only_crossed_fx_high_or_low_without_correction():
    eurusd = next(item for item in load_canonical_instruments() if item.key == "eurusd")
    times = ["2016-08-29T06:00:00Z", "2016-08-29T07:00:00Z", "2016-08-29T08:00:00Z"]
    payload = chart_payload(times, fx=True, data_version=29_732_293)
    payload["Data"][1].update(
        {
            "OpenBid": "1.1180",
            "OpenAsk": "1.1182",
            "HighBid": "1.1190",
            "HighAsk": "1.1192",
            "CloseBid": "1.1185",
            "CloseAsk": "1.1187",
        }
    )
    payload["Data"][1]["LowBid"] = "1.11749"
    payload["Data"][1]["LowAsk"] = "1.11726"

    accepted, rejected = normalize_chart_page_quarantining_fx_extrema(
        eurusd,
        payload,
        retrieved_at_utc=datetime(2026, 7, 17, tzinfo=timezone.utc),
        payload_sha256="f" * 64,
        artifact_relative_path="data/acquisition/runs/example/instruments/eurusd/chart_0053.json",
    )

    assert [bar.time_utc.hour for bar in accepted] == [6, 8]
    assert len(rejected) == 1
    assert rejected[0].time_utc == datetime(2016, 8, 29, 7, tzinfo=timezone.utc)
    assert rejected[0].violations == (
        CrossedQuoteViolation("Low", Decimal("1.11749"), Decimal("1.11726")),
    )
    with pytest.raises(BarQualityError, match="FX_BID_ABOVE_ASK"):
        normalize_chart_page(
            eurusd,
            payload,
            retrieved_at_utc=datetime(2026, 7, 17, tzinfo=timezone.utc),
            payload_sha256="f" * 64,
            artifact_relative_path="data/acquisition/runs/example/instruments/eurusd/chart_0053.json",
        )


def test_full_refetch_does_not_quarantine_crossed_fx_open_or_close():
    eurusd = next(item for item in load_canonical_instruments() if item.key == "eurusd")
    payload = chart_payload(["2016-08-29T07:00:00Z"], fx=True)
    payload["Data"][0]["OpenBid"] = "1.1003"
    payload["Data"][0]["OpenAsk"] = "1.1002"
    with pytest.raises(BarQualityError, match="FX_BID_ABOVE_ASK"):
        normalize_chart_page_quarantining_fx_extrema(
            eurusd,
            payload,
            retrieved_at_utc=datetime(2026, 7, 17, tzinfo=timezone.utc),
            payload_sha256="f" * 64,
            artifact_relative_path="data/acquisition/runs/example/instruments/eurusd/chart.json",
        )


def test_full_refetch_does_not_hide_other_quality_errors_on_crossed_extrema_row():
    eurusd = next(item for item in load_canonical_instruments() if item.key == "eurusd")
    payload = chart_payload(["2016-08-29T07:00:00Z"], fx=True)
    payload["Data"][0]["LowBid"] = "1.11749"
    payload["Data"][0]["LowAsk"] = "1.11726"
    payload["Data"][0]["HighBid"] = "1.0000"
    payload["Data"][0]["HighAsk"] = "1.0002"
    with pytest.raises(BarQualityError, match="OHLC_VIOLATION"):
        normalize_chart_page_quarantining_fx_extrema(
            eurusd,
            payload,
            retrieved_at_utc=datetime(2026, 7, 17, tzinfo=timezone.utc),
            payload_sha256="f" * 64,
            artifact_relative_path="data/acquisition/runs/example/instruments/eurusd/chart.json",
        )


def test_full_refetch_quarantine_enforces_absolute_and_rate_limits():
    start = datetime(2016, 8, 29, tzinfo=timezone.utc)
    accepted_at_limit = [start + timedelta(hours=index + 1) for index in range(9_999)]
    accepted = _validate_full_refetch_quarantine(accepted_at_limit, [rejected_bar(start)])
    assert len(accepted) == 1  # exactly 1 / 10,000 = 0.01%

    accepted_over_rate = [start + timedelta(hours=index + 1) for index in range(9_998)]
    with pytest.raises(BarQualityError, match="FX_EXTREMA_QUARANTINE_RATE_LIMIT_EXCEEDED"):
        _validate_full_refetch_quarantine(accepted_over_rate, [rejected_bar(start)])

    too_many = [rejected_bar(start + timedelta(hours=index)) for index in range(11)]
    with pytest.raises(BarQualityError, match="FX_EXTREMA_QUARANTINE_ROW_LIMIT_EXCEEDED"):
        _validate_full_refetch_quarantine([start + timedelta(hours=20)], too_many)


def test_full_refetch_quarantine_rejects_latest_or_conflicting_sample():
    start = datetime(2016, 8, 29, tzinfo=timezone.utc)
    with pytest.raises(BarQualityError, match="FX_EXTREMA_QUARANTINE_LATEST_SAMPLE_INELIGIBLE"):
        _validate_full_refetch_quarantine([start], [rejected_bar(start + timedelta(hours=1))])
    with pytest.raises(
        BarQualityError, match="FX_EXTREMA_QUARANTINE_ACCEPTED_REJECTED_CONFLICT"
    ):
        _validate_full_refetch_quarantine([start], [rejected_bar(start)])


def test_quarantine_evidence_preserves_original_values_and_relative_source():
    observed = _quarantined_row_evidence(
        rejected_bar(datetime(2016, 8, 29, 7, tzinfo=timezone.utc))
    )
    assert observed["time_utc"] == "2016-08-29T07:00:00Z"
    assert observed["violations"] == [{"field": "Low", "bid": "1.11749", "ask": "1.11726"}]
    assert observed["artifact_relative_path"].startswith("data/acquisition/runs/")
    assert observed["payload_sha256"] == "f" * 64


def test_boundary_inclusive_forward_paging_deduplicates():
    times1 = ["2026-07-16T00:00:00Z", "2026-07-16T01:00:00Z", "2026-07-16T02:00:00Z"]
    times2 = ["2026-07-16T02:00:00Z", "2026-07-16T03:00:00Z"]
    responses = [
        HTTPResponse(200, {}, json.dumps(chart_payload(times1)).encode()),
        HTTPResponse(200, {}, json.dumps(chart_payload(times2)).encode()),
    ]
    client = SaxoClient("secret", transport=FakeTransport(responses))
    spy = load_canonical_instruments()[0]
    pages = fetch_chart_pages(
        client, spy, mode="From", time_utc=datetime(2026, 7, 16, tzinfo=timezone.utc), count=3
    )
    assert len(pages) == 2
    assert pages[1].request_time_utc == "2026-07-16T02:00:00Z"


def test_boundary_inclusive_backward_paging_advances_to_earliest_sample():
    times1 = ["2026-07-16T02:00:00Z", "2026-07-16T03:00:00Z", "2026-07-16T04:00:00Z"]
    times2 = ["2026-07-16T00:00:00Z", "2026-07-16T01:00:00Z", "2026-07-16T02:00:00Z"]
    times3 = ["2026-07-15T23:00:00Z", "2026-07-16T00:00:00Z"]
    responses = [
        HTTPResponse(200, {}, json.dumps(chart_payload(times1)).encode()),
        HTTPResponse(200, {}, json.dumps(chart_payload(times2)).encode()),
        HTTPResponse(200, {}, json.dumps(chart_payload(times3)).encode()),
    ]
    client = SaxoClient("secret", transport=FakeTransport(responses))
    spy = load_canonical_instruments()[0]
    pages = fetch_chart_pages(
        client, spy, mode="UpTo", time_utc=datetime(2026, 7, 16, 4, tzinfo=timezone.utc), count=3
    )
    assert len(pages) == 3
    assert pages[1].request_time_utc == "2026-07-16T02:00:00Z"
    assert pages[2].request_time_utc == "2026-07-16T00:00:00Z"


def test_equity_calendar_handles_dst_holidays_short_days_and_exceptional_closures():
    sessions = {item.session_date: item for item in generate_equity_sessions(date(2026, 3, 6), date(2026, 3, 9))}
    assert sessions[date(2026, 3, 6)].open_time_utc.hour == 14
    assert sessions[date(2026, 3, 9)].open_time_utc.hour == 13
    july = {item.session_date: item for item in generate_equity_sessions(date(2026, 7, 1), date(2026, 7, 6))}
    assert july[date(2026, 7, 2)].status == "SHORT_SESSION"
    assert july[date(2026, 7, 3)].status == "HOLIDAY"
    november = {item.session_date: item for item in generate_equity_sessions(date(2026, 11, 26), date(2026, 11, 27))}
    assert november[date(2026, 11, 27)].status == "SHORT_SESSION"
    exceptional = generate_equity_sessions(date(2012, 10, 29), date(2012, 10, 30))
    assert all(item.status == "HOLIDAY" for item in exceptional)
    new_year = generate_equity_sessions(date(2021, 12, 31), date(2021, 12, 31))
    assert new_year[0].status == "HOLIDAY"


def test_db3_manifest_baseline_does_not_freeze_live_row_counts():
    payload = {
        "derived": {
            "market_bar_4h_rows": 107_623,
            "market_bar_4h_analysis_eligible_rows": 78_232,
            "market_bar_1d_rows": 44_292,
            "market_bar_1d_analysis_eligible_rows": 39_336,
            "quality_fail_rows": 0,
        }
    }
    assert db3_manifest_baseline_is_valid(payload)
    payload["derived"]["market_bar_4h_rows"] = 0
    assert not db3_manifest_baseline_is_valid(payload)
    payload["derived"]["market_bar_4h_rows"] = 107_623
    payload["derived"]["quality_fail_rows"] = 1
    assert not db3_manifest_baseline_is_valid(payload)


def test_failed_instrument_context_is_only_emitted_for_non_pass_runs():
    assert _failed_instrument_context("BLOCKED", "iwm") == {"failed_instrument_key": "iwm"}
    assert _failed_instrument_context("FAILED", "iwm") == {"failed_instrument_key": "iwm"}
    assert _failed_instrument_context("PASS", "iwm") == {}
    assert _failed_instrument_context("BLOCKED", None) == {}


def test_interface_and_not_ready_failures_do_not_become_content_quality_events():
    for code in (
        "BLOCKED_RATE_LIMIT",
        "BLOCKED_TOKEN_EXPIRED",
        "FAILED_NETWORK",
        "FAILED_HTTP_503",
        "FAILED_SERVICE_UNAVAILABLE",
        "INSUFFICIENT_INCREMENTAL_CHART_DATA",
        "BLOCKED_FULL_REFETCH_REQUIRED",
        "BLOCKED_BOUNDED_REVISION_REQUIRED",
        "BLOCKED_REVISION_SCOPE_UNRESOLVED_FULL_REFETCH_REQUIRED",
    ):
        assert not _records_quality_event(code)
    for code in (
        "BLOCKED_CANONICAL_WATERMARK_SET",
        "BLOCKED_INSTRUMENT_DRIFT",
        "FUTURE_COMPLETED_BAR",
    ):
        assert _records_quality_event(code)


def test_reconcile_refetches_drift_and_finishes_with_two_normal_passes():
    normal_results = iter(
        [
            {
                "status": "BLOCKED",
                "error_code": "BLOCKED_FULL_REFETCH_REQUIRED",
                "failed_instrument_key": "efa",
                "database_ingestion_run_id": 1,
            },
            {"status": "PASS", "error_code": None, "database_ingestion_run_id": 3},
            {"status": "PASS", "error_code": None, "database_ingestion_run_id": 4},
        ]
    )
    progress = []

    result = reconcile_incremental(
        normal_runner=lambda: next(normal_results),
        full_refetch_runner=lambda key: {
            "status": "PASS",
            "error_code": None,
            "instrument_key": key,
            "database_ingestion_run_id": 2,
        },
        stale_key_loader=lambda: (),
        on_step=progress.append,
    )

    assert result["status"] == "PASS"
    assert result["consecutive_normal_passes"] == 2
    assert result["normal_pass_run_ids"] == [3, 4]
    assert result["refetched_instruments"] == ["efa"]
    assert [step["operation"] for step in progress] == ["run", "full-refetch", "run", "run"]


def test_reconcile_blocks_repeated_version_change_for_same_instrument():
    normal_results = iter(
        [
            {"status": "BLOCKED", "error_code": "BLOCKED_FULL_REFETCH_REQUIRED", "failed_instrument_key": "efa"},
            {"status": "BLOCKED", "error_code": "BLOCKED_FULL_REFETCH_REQUIRED", "failed_instrument_key": "efa"},
        ]
    )
    full_refetch_calls = []

    result = reconcile_incremental(
        normal_runner=lambda: next(normal_results),
        full_refetch_runner=lambda key: full_refetch_calls.append(key) or {
            "status": "PASS", "error_code": None, "instrument_key": key
        },
        stale_key_loader=lambda: (),
    )

    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "BLOCKED_REPEATED_DATA_VERSION_CHANGE"
    assert full_refetch_calls == ["efa"]


def test_reconcile_propagates_non_dataversion_block_without_refetch():
    result = reconcile_incremental(
        normal_runner=lambda: {"status": "BLOCKED", "error_code": "BLOCKED_RATE_LIMIT"},
        full_refetch_runner=lambda key: pytest.fail(f"unexpected full refetch: {key}"),
        stale_key_loader=lambda: (),
    )
    assert result["status"] == "BLOCKED"
    assert result["error_code"] == "BLOCKED_RATE_LIMIT"
    assert result["refetched_instruments"] == []


def test_reconcile_recovers_watermark_already_stale_at_start():
    normal_results = iter(
        [
            {"status": "PASS", "error_code": None, "database_ingestion_run_id": 11},
            {"status": "PASS", "error_code": None, "database_ingestion_run_id": 12},
        ]
    )
    full_refetch_calls = []

    result = reconcile_incremental(
        normal_runner=lambda: next(normal_results),
        full_refetch_runner=lambda key: full_refetch_calls.append(key) or {
            "status": "PASS",
            "error_code": None,
            "instrument_key": key,
            "database_ingestion_run_id": 10,
        },
        stale_key_loader=lambda: ("eem",),
    )

    assert result["status"] == "PASS"
    assert result["normal_pass_run_ids"] == [11, 12]
    assert result["refetched_instruments"] == ["eem"]
    assert full_refetch_calls == ["eem"]
