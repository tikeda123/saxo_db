from __future__ import annotations

import json

import pytest

from market_db.connection import FORWARD_DB, MARKET_DB, RESEARCH_DB
from market_db.inspect import QUERY_SPECS, QuerySpec, has_alert, main, query_spec, render_json, render_table


def test_query_allow_list_has_only_reader_roles():
    assert len(QUERY_SPECS) == 12
    assert all("reader" in spec.role for spec in QUERY_SPECS.values())
    with pytest.raises(ValueError):
        query_spec(FORWARD_DB, "inventory")
    with pytest.raises(ValueError):
        query_spec(RESEARCH_DB, "freshness")


def test_renderers_have_stable_empty_semantics():
    assert render_table([]) == "(0 rows)"
    payload = json.loads(render_json(MARKET_DB, "inventory", []))
    assert payload == {"command": "inventory", "database": MARKET_DB, "row_count": 0, "rows": []}


def test_alert_evaluation():
    spec = QuerySpec("reader", "view", "id", "status", frozenset({"FAIL"}))
    assert has_alert(spec, [{"status": "FAIL"}])
    assert not has_alert(spec, [{"status": "NOT_EVALUATED"}])
    quality = QUERY_SPECS[(MARKET_DB, "quality")]
    assert has_alert(quality, [{"severity": "CRITICAL", "current_blocker": True}])
    assert not has_alert(
        quality,
        [{"severity": "CRITICAL", "applicability": "HISTORICAL", "current_blocker": False}],
    )


def test_main_renders_mocked_json(monkeypatch, capsys):
    monkeypatch.setattr("market_db.inspect.fetch_rows", lambda database, command, limit: [])
    assert main(["inventory", "--format", "json"]) == 0
    assert json.loads(capsys.readouterr().out)["row_count"] == 0
