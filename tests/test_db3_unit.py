from __future__ import annotations

import json
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

import market_db.raw_artifacts as raw_artifacts
from market_db.acquire_pages import fetch_chart_pages
from market_db.incremental_update import (
    _failed_instrument_context,
    _quarantined_row_evidence,
    _validate_full_refetch_quarantine,
    reconcile_incremental,
)
from market_db.instrument_registry import InstrumentDriftError, load_canonical_instruments, validate_detail
from market_db.normalize_bars import (
    BarQualityError,
    CrossedQuoteViolation,
    RejectedBar,
    merge_pages,
    normalize_chart_page,
    normalize_chart_page_quarantining_fx_extrema,
)
from market_db.raw_artifacts import RunArtifacts, canonical_json_bytes
from market_db.saxo_client import HTTPResponse, SIM_BASE_URL, SaxoAPIError, SaxoClient
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


def test_fx_bid_above_ask_and_wrong_horizon_fail_quality_gate():
    eurusd = next(item for item in load_canonical_instruments() if item.key == "eurusd")
    payload = chart_payload(["2026-07-16T10:00:00Z"], fx=True)
    payload["Data"][0]["OpenBid"] = "1.1003"
    with pytest.raises(BarQualityError, match="FX_BID_ABOVE_ASK"):
        normalize_chart_page(
            eurusd, payload, retrieved_at_utc=datetime.now(timezone.utc), payload_sha256="0" * 64,
            artifact_relative_path="data/acquisition/fail.json",
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
