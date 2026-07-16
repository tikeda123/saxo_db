from __future__ import annotations

import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

import market_db.raw_artifacts as raw_artifacts
from market_db.acquire_pages import fetch_chart_pages
from market_db.instrument_registry import InstrumentDriftError, load_canonical_instruments, validate_detail
from market_db.normalize_bars import BarQualityError, merge_pages, normalize_chart_page
from market_db.raw_artifacts import RunArtifacts, canonical_json_bytes
from market_db.saxo_client import HTTPResponse, SIM_BASE_URL, SaxoAPIError, SaxoClient
from market_db.session_calendar import generate_equity_sessions


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, method, url, *, headers, timeout):
        self.calls.append((method, url, headers, timeout))
        return self.responses.pop(0)


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
    payload["Data"][0]["OpenBid"] = "2"
    with pytest.raises(BarQualityError, match="FX_BID_ABOVE_ASK"):
        normalize_chart_page(
            eurusd, payload, retrieved_at_utc=datetime.now(timezone.utc), payload_sha256="0" * 64,
            artifact_relative_path="data/acquisition/fail.json",
        )


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
