from pathlib import Path


def test_read_api_openapi_contract_covers_stable_v1_endpoints_and_cursor_errors():
    path = Path(__file__).parents[1] / "specs/read_api_v1_openapi.yaml"
    text = path.read_text(encoding="utf-8")
    assert "openapi: 3.0.3" in text
    for route in (
        "/api/v1/bars:",
        "/api/v1/snapshots/{snapshot_id}/bars:",
        "/api/v1/total-return:",
        "/api/v1/series-status:",
        "/api/v1/manifests:",
        "/api/v1/layer-counts:",
        "/api/v1/operations/{command}:",
    ):
        assert route in text
    assert "name: cursor" in text
    assert "CURSOR_INVALID" in text
    assert "CURSOR_QUERY_MISMATCH" in text
    assert "CURSOR_EXPIRED" in text
    assert "next_cursor" in text
    assert "current-data bar page; cursor is not required" in text
