from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from market_db.data_version_reconcile import (
    StoredBar,
    _bar_content,
    _insert_revision_event,
    compare_revision_window,
)
from market_db.incremental_update import InstrumentState
from market_db.instrument_registry import CanonicalInstrument
from market_db.normalize_bars import NormalizedBar
from market_db.raw_artifacts import ArtifactRecord


def bar(index: int, *, value: str = "100", version: int = 2, complete: bool = True) -> NormalizedBar:
    selected = Decimal(value)
    return NormalizedBar(
        time_utc=datetime(2026, 7, 1, tzinfo=timezone.utc) + timedelta(hours=index),
        open=selected,
        high=selected + Decimal("1"),
        low=selected - Decimal("1"),
        close=selected,
        open_bid=None,
        high_bid=None,
        low_bid=None,
        close_bid=None,
        open_ask=None,
        high_ask=None,
        low_ask=None,
        close_ask=None,
        volume=Decimal("10"),
        market_trading_state="Automated",
        price_basis="native_ohlc",
        is_complete=complete,
        data_version=version,
        delayed_by_minutes=0,
        retrieved_at_utc=datetime(2026, 7, 28, tzinfo=timezone.utc),
        payload_sha256="a" * 64,
        artifact_relative_path="data/acquisition/test.json",
    )


def stored(provider_bars, *, version: int = 1):
    return {
        item.time_utc: StoredBar(item.time_utc, _bar_content(item), version)
        for item in provider_bars
    }


def test_recent_change_with_stable_anchor_is_bounded():
    provider = [bar(index) for index in range(22)]
    existing = stored(provider[:21])
    changed = replace(provider[20], close=Decimal("100.5"), is_complete=False)
    provider[20] = changed

    result = compare_revision_window(
        provider,
        existing,
        old_data_version=1,
        requested_count=96,
    )

    assert result.decision == "READY_TO_APPLY"
    assert result.reason_code == "REVISION_BOUNDED_RANGE_IDENTIFIED"
    assert result.content_difference_rows == 1
    assert result.new_rows == 1
    assert result.version_only_rows == 20
    assert result.stable_anchor_rows == 20
    assert result.affected_from_utc == provider[20].time_utc
    assert result.affected_to_utc == provider[21].time_utc


def test_change_at_oldest_boundary_expands_then_fails_closed_at_limit():
    provider = [bar(index) for index in range(96)]
    existing = stored(provider)
    provider[0] = replace(provider[0], close=Decimal("100.5"))

    expand = compare_revision_window(
        provider,
        existing,
        old_data_version=1,
        requested_count=96,
    )
    blocked = compare_revision_window(
        provider,
        existing,
        old_data_version=1,
        requested_count=1200,
    )

    assert expand.decision == "EXPAND"
    assert blocked.decision == "BLOCKED_FULL_REFETCH"
    assert blocked.reason_code == "REVISION_COMPARISON_LIMIT_EXCEEDED"


def test_corporate_action_shape_requires_full_refetch():
    original = [bar(index) for index in range(96)]
    existing = stored(original)
    revised = [replace(item, open=item.open / 2, high=item.high / 2,
                       low=item.low / 2, close=item.close / 2) for item in original]

    result = compare_revision_window(
        revised,
        existing,
        old_data_version=1,
        requested_count=96,
    )

    assert result.decision == "BLOCKED_FULL_REFETCH"
    assert result.reason_code == "REVISION_WHOLE_HISTORY_CHANGE_SUSPECTED"


def test_no_content_change_retains_old_rows_and_advances_only_version_boundary():
    provider = [bar(index) for index in range(32)]
    result = compare_revision_window(
        provider,
        stored(provider),
        old_data_version=1,
        requested_count=96,
    )
    assert result.decision == "READY_TO_APPLY"
    assert result.reason_code == "REVISION_NO_CONTENT_CHANGE_WITH_STABLE_ANCHOR"
    assert result.content_difference_rows == 0
    assert result.version_only_rows == 32
    assert result.affected_from_utc is None


def test_bounded_discovery_reuses_the_persisted_detection_event():
    provider = [bar(index) for index in range(32)]
    comparison = compare_revision_window(
        provider,
        stored(provider),
        old_data_version=1,
        requested_count=96,
    )

    class Cursor:
        def __init__(self):
            self.executed = []
            self.steps = []
            self.selected = (77,)

        def execute(self, statement, params=()):
            self.executed.append((statement, params))

        def fetchone(self):
            return self.selected

        def executemany(self, statement, params):
            selected = list(params)
            assert all(statement.count("%s") == len(row) for row in selected)
            self.steps.extend(selected)

    cursor = Cursor()
    instrument = CanonicalInstrument(
        category="equity_reit",
        key="spy",
        symbol="SPY:arcx",
        uic=36590,
        asset_type="Etf",
        currency="USD",
    )
    record = ArtifactRecord("data/acquisition/window.json", "a" * 64, 100, 32)
    evidence = ArtifactRecord("data/acquisition/evidence.json", "b" * 64, 100, 1)

    event_id = _insert_revision_event(
        cursor,
        run_id=123,
        state=InstrumentState(9, provider[-1].time_utc, 1, "STALE_DATA_VERSION"),
        instrument=instrument,
        comparisons=(comparison,),
        step_artifacts=(record,),
        evidence=evidence,
        status="READY_TO_APPLY",
    )

    assert event_id == 77
    statements = [statement for statement, _ in cursor.executed]
    assert any("UPDATE ops.data_version_revision_event" in statement for statement in statements)
    assert not any("INSERT INTO ops.data_version_revision_event" in statement for statement in statements)
    assert not any("DELETE FROM ops.data_version_revision_step" in statement for statement in statements)
    assert cursor.steps[0][0] == 77


def test_approved_apply_requires_the_separately_reviewed_event_identity():
    provider = [bar(index) for index in range(32)]
    comparison = compare_revision_window(
        provider,
        stored(provider),
        old_data_version=1,
        requested_count=96,
    )

    class Cursor:
        rowcount = 1

        def __init__(self):
            self.executed = []
            self.steps = []

        def execute(self, statement, params=()):
            self.executed.append((statement, params))

        def fetchone(self):
            return (88,)

        def executemany(self, statement, params):
            self.steps.extend(list(params))

    cursor = Cursor()
    instrument = CanonicalInstrument(
        category="equity_reit",
        key="spy",
        symbol="SPY:arcx",
        uic=36590,
        asset_type="Etf",
        currency="USD",
    )
    record = ArtifactRecord("data/acquisition/window.json", "a" * 64, 100, 32)
    evidence = ArtifactRecord("data/acquisition/evidence.json", "b" * 64, 100, 1)

    event_id = _insert_revision_event(
        cursor,
        run_id=123,
        state=InstrumentState(9, provider[-1].time_utc, 1, "ACTIVE"),
        instrument=instrument,
        comparisons=(comparison,),
        step_artifacts=(record,),
        evidence=evidence,
        status="READY_TO_APPLY",
        approved_revision_event_id=88,
    )

    assert event_id == 88
    statements = [statement for statement, _ in cursor.executed]
    assert "review_status='APPLY_APPROVED'" in statements[0]
    assert any("UPDATE ops.data_version_revision_event" in item for item in statements)
    assert not any("INSERT INTO ops.data_version_revision_event" in item for item in statements)
    assert cursor.steps[0][0] == 88
