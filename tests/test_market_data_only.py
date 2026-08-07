from __future__ import annotations

import json

import pytest

from market_db.market_data_only import ALLOWED_ENDPOINT_IDS, active_registry
from market_db.saxo_client import (
    HTTPResponse,
    MARKET_DATA_ONLY_ENDPOINT_PROFILE,
    SaxoClient,
)


class FakeTransport:
    def __init__(self) -> None:
        self.urls: list[str] = []

    def request(self, method, url, *, headers, timeout):
        self.urls.append(url)
        return HTTPResponse(200, {}, json.dumps({"ok": True}).encode())


def test_market_data_profile_blocks_identity_and_account_endpoints_before_transport():
    transport = FakeTransport()
    client = SaxoClient(
        "memory-only-token",
        transport=transport,
        endpoint_profile=MARKET_DATA_ONLY_ENDPOINT_PROFILE,
    )

    for operation in (
        client.smoke_test,
        client.accounts_me,
        client.balances_me,
        client.session_capabilities,
    ):
        with pytest.raises(ValueError, match="not allow-listed"):
            operation()

    assert transport.urls == []
    assert client.request_count == 0
    assert client.write_request_count == 0


def test_market_data_profile_allows_only_detail_schedule_and_chart():
    transport = FakeTransport()
    client = SaxoClient(
        "memory-only-token",
        transport=transport,
        endpoint_profile=MARKET_DATA_ONLY_ENDPOINT_PROFILE,
    )

    client.instrument_detail(21, "FxSpot")
    client.trading_schedule(21, "FxSpot")
    client.chart(21, "FxSpot", count=2)

    assert client.endpoint_counts == {
        "instrument_detail": 1,
        "trading_schedule": 1,
        "chart": 1,
    }
    assert tuple(client.endpoint_counts) == ALLOWED_ENDPOINT_IDS
    assert client.request_count == 3
    assert client.write_request_count == 0


def test_active_market_data_scope_excludes_usdjpy_and_contains_fifteen_series():
    selected = active_registry()
    assert len(selected) == 15
    assert "usdjpy" not in {item.key for item in selected}
    assert {"audusd", "usdcad", "usdchf"} <= {item.key for item in selected}


def test_incremental_market_only_entrypoint_can_disable_legacy_smoke():
    from inspect import signature

    from market_db.incremental_update import run_incremental

    parameter = signature(run_incremental).parameters["perform_smoke_test"]
    assert parameter.default is True
