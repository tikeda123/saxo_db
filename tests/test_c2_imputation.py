from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from market_db.c2_imputation import (
    persist_c2_imputation_plan,
    plan_c2_session_imputation,
    refresh_c2_imputation_overlay,
)
from market_db.normalize_bars import NormalizedBar


UTC = timezone.utc
SESSION_DATE = date(2026, 7, 29)
OPEN = datetime(2026, 7, 29, 13, 30, tzinfo=UTC)
EXPECTED = tuple(OPEN + timedelta(hours=index) for index in range(7))


def bar(time_utc: datetime, close: str = "100", *, data_version: int = 77) -> NormalizedBar:
    value = Decimal(close)
    return NormalizedBar(
        time_utc=time_utc,
        open=value,
        high=value + Decimal("1"),
        low=value - Decimal("1"),
        close=value,
        open_bid=None,
        high_bid=None,
        low_bid=None,
        close_bid=None,
        open_ask=None,
        high_ask=None,
        low_ask=None,
        close_ask=None,
        volume=Decimal("10"),
        market_trading_state="Open",
        price_basis="native_ohlc",
        is_complete=True,
        data_version=data_version,
        delayed_by_minutes=15,
        retrieved_at_utc=datetime(2026, 7, 31, tzinfo=UTC),
        payload_sha256="a" * 64,
        artifact_relative_path="data/acquisition/runs/review/chart_0001.json",
    )


def test_tip_gld_shape_allows_two_session_open_gaps_with_explicit_warning():
    actual = [bar(value, str(100 + index)) for index, value in enumerate(EXPECTED[2:], 2)]
    previous = bar(datetime(2026, 7, 28, 19, 30, tzinfo=UTC), "99")

    plan = plan_c2_session_imputation(
        instrument_key="tip",
        session_date=SESSION_DATE,
        expected_times_utc=EXPECTED,
        actual_bars=actual,
        calendar_verified=True,
        previous_session_terminal_bar=previous,
        previous_session_terminal_time_utc=previous.time_utc,
    )

    assert plan.status == "PASS_WITH_IMPUTATION_WARNING"
    assert plan.warning_ids == ("C2_BOUNDED_IMPUTED_PREVIOUS_VALID",)
    assert [row.time_utc for row in plan.imputed_rows] == list(EXPECTED[:2])
    assert [row.consecutive_gap_index for row in plan.imputed_rows] == [1, 2]
    assert all(row.consecutive_gap_count == 2 for row in plan.imputed_rows)
    assert all(row.source_kind == "IMPUTED_PREVIOUS_VALID" for row in plan.imputed_rows)
    assert all(row.source_time_utc == previous.time_utc for row in plan.imputed_rows)
    assert all(row.open == row.high == row.low == row.close == Decimal("99") for row in plan.imputed_rows)
    assert all(row.volume is None for row in plan.imputed_rows)
    assert all(not row.official_close_claim for row in plan.imputed_rows)
    assert all(not row.total_return_claim for row in plan.imputed_rows)
    assert all(not row.execution_price_claim for row in plan.imputed_rows)


def test_internal_gap_uses_previous_actual_not_previous_imputation_recursively():
    actual = [bar(value, str(100 + index)) for index, value in enumerate(EXPECTED) if index != 3]
    plan = plan_c2_session_imputation(
        instrument_key="gld",
        session_date=SESSION_DATE,
        expected_times_utc=EXPECTED,
        actual_bars=actual,
        calendar_verified=True,
    )

    assert plan.status == "PASS_WITH_IMPUTATION_WARNING"
    assert len(plan.imputed_rows) == 1
    assert plan.imputed_rows[0].source_time_utc == EXPECTED[2]
    assert plan.imputed_rows[0].close == Decimal("102")
    assert plan.imputed_rows[0].reason == "PROVIDER_INTERNAL_SESSION_ROWS_MISSING"


def test_daily_close_missing_is_never_imputed():
    plan = plan_c2_session_imputation(
        instrument_key="tip",
        session_date=SESSION_DATE,
        expected_times_utc=EXPECTED,
        actual_bars=[bar(value) for value in EXPECTED[:-1]],
        calendar_verified=True,
    )
    assert plan.status == "BLOCKED_DAILY_CLOSE_SOURCE_MISSING"
    assert plan.imputed_rows == ()


def test_unbounded_and_unanchored_gaps_fail_closed():
    too_many = plan_c2_session_imputation(
        instrument_key="tip",
        session_date=SESSION_DATE,
        expected_times_utc=EXPECTED,
        actual_bars=[bar(value) for value in EXPECTED[3:]],
        calendar_verified=True,
        previous_session_terminal_bar=bar(datetime(2026, 7, 28, 19, 30, tzinfo=UTC)),
        previous_session_terminal_time_utc=datetime(2026, 7, 28, 19, 30, tzinfo=UTC),
    )
    no_previous = plan_c2_session_imputation(
        instrument_key="gld",
        session_date=SESSION_DATE,
        expected_times_utc=EXPECTED,
        actual_bars=[bar(value) for value in EXPECTED[2:]],
        calendar_verified=True,
    )

    assert too_many.status == "BLOCKED_MISSING_PER_SESSION_LIMIT"
    assert no_previous.status == "BLOCKED_SESSION_START_WITHOUT_PREVIOUS_ACTUAL"


def test_calendar_version_and_actual_quality_must_be_proven():
    bars = [bar(value) for value in EXPECTED]
    calendar = plan_c2_session_imputation(
        instrument_key="tip",
        session_date=SESSION_DATE,
        expected_times_utc=EXPECTED,
        actual_bars=bars,
        calendar_verified=False,
    )
    mixed_bars = [bar(value) for index, value in enumerate(EXPECTED) if index != 3]
    mixed_bars[-1] = replace(mixed_bars[-1], data_version=78)
    mixed_version = plan_c2_session_imputation(
        instrument_key="tip",
        session_date=SESSION_DATE,
        expected_times_utc=EXPECTED,
        actual_bars=mixed_bars,
        calendar_verified=True,
    )

    assert calendar.status == "BLOCKED_CALENDAR_NOT_VERIFIED"
    assert mixed_version.status == "BLOCKED_DATA_VERSION_IDENTITY"


def test_persistence_is_append_only_and_parameterized():
    actual = [bar(value) for value in EXPECTED[2:]]
    previous = bar(datetime(2026, 7, 28, 19, 30, tzinfo=UTC), "99")
    plan = plan_c2_session_imputation(
        instrument_key="tip",
        session_date=SESSION_DATE,
        expected_times_utc=EXPECTED,
        actual_bars=actual,
        calendar_verified=True,
        previous_session_terminal_bar=previous,
        previous_session_terminal_time_utc=previous.time_utc,
    )

    class Cursor:
        rowcount = 1

        def __init__(self):
            self.calls = []

        def execute(self, statement, params=()):
            self.calls.append((statement, tuple(params)))

    cursor = Cursor()
    inserted = persist_c2_imputation_plan(
        cursor,
        instrument_id=9,
        session_calendar_id="XNYS_US_EQUITY",
        review_id="review-1",
        plan=plan,
        source_ingestion_run_ids={previous.time_utc: 123},
    )

    assert inserted == 2
    assert len(cursor.calls) == 2
    for statement, params in cursor.calls:
        assert statement.count("%s") == len(params)
        assert "ON CONFLICT" in statement
        assert "DO NOTHING" in statement
        assert "UPDATE" not in statement
        assert "DELETE" not in statement


def test_refresh_is_noop_until_migration_0036_is_applied():
    class Cursor:
        def execute(self, statement, params=()):
            assert statement == "SELECT to_regclass('derived.c2_market_bar_1h_imputation')"

        def fetchone(self):
            return (None,)

    result = refresh_c2_imputation_overlay(Cursor(), instrument_ids=(9,))
    assert result == {
        "status": "NOT_APPLIED_SCHEMA",
        "required_migration": "0036_c2_bounded_imputation_overlay.sql",
        "inserted_rows": 0,
        "plans": [],
    }
