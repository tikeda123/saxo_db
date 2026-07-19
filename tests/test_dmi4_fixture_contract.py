from __future__ import annotations

import json
from pathlib import Path


FIXTURE = Path(__file__).parent / "fixtures/read_api_contract_v1/contract_cases.json"


def _key(row: dict, fields: list[str]) -> tuple[object, ...]:
    return tuple(row[field] for field in fields)


def test_dmi4_consumer_fixture_covers_page_parity_and_fail_closed_errors():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert payload["api_version"] == 1
    assert payload["contract_revision"] == "1.2"
    cases = {case["id"]: case for case in payload["cases"]}

    for case_id in ("snapshot_bars_two_page_parity", "total_return_two_page_parity"):
        case = cases[case_id]
        fields = case["key_fields"]
        rows = [row for page in case["pages"] for row in page["rows"]]
        direct = case["direct_rows"]
        keys = [_key(row, fields) for row in rows]
        direct_keys = [_key(row, fields) for row in direct]
        assert keys == direct_keys
        assert len(keys) - len(set(keys)) == case["expected"]["duplicates"]
        assert len(set(direct_keys) - set(keys)) == case["expected"]["missing"]
        assert sum(left > right for left, right in zip(keys, keys[1:])) == case["expected"]["order_reversal"]
        assert case["pages"][-1]["next_cursor"] is None

    assert {
        (case["response"]["http_status"], case["response"]["error_code"])
        for case in cases.values()
        if "response" in case
    } == {
        (400, "CURSOR_INVALID"),
        (409, "CURSOR_QUERY_MISMATCH"),
        (409, "CURSOR_EXPIRED"),
    }
