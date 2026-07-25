from __future__ import annotations

import market_db.mcp_server as server
from market_db.connection import project_root


def sample_catalog():
    return {
        "catalog_id": "test",
        "scope_note_ja": "投資助言ではない",
        "instruments": [
            {
                "instrument_key": "spy",
                "short_name": "SPY",
                "display_name_ja": "SPDR S&P 500 ETF Trust",
                "instrument_type_ja": "米国上場ETF",
                "category": "equity_reit",
                "summary_ja": "米国大型株ETF",
                "official_sources": [{"label": "公式", "url": "https://example.invalid"}],
                "managed_instrument": {"instrument_id": 9, "symbol": "SPY:arcx"},
                "managed_series": {"series_count": 1, "layers": ["1H"]},
            }
        ],
    }


def test_mcp_list_and_description_are_grounded_in_local_catalog(monkeypatch):
    monkeypatch.setattr(server, "_read_catalog", sample_catalog)
    listing = server.list_managed_instruments("equity_reit")
    assert listing["instrument_count"] == 1
    assert listing["instruments"][0]["instrument_key"] == "spy"
    detail = server.describe_instrument("SPY")
    assert detail["managed_instrument"]["symbol"] == "SPY:arcx"


def test_mcp_prompt_requires_known_key_and_forbids_advice():
    prompt = server.explain_saxo_db_series("spy", "初心者")
    assert "describe_instrument" in prompt
    assert "get_managed_series" in prompt
    assert "投資助言" in prompt
    assert "official_sources" in prompt


def test_project_mcp_configuration_uses_local_stdio_and_no_model_api_key():
    config = (project_root() / ".codex/config.toml").read_text(encoding="utf-8")
    assert "[mcp_servers.saxo_db]" in config
    assert 'args = ["-m", "market_db.mcp_server"]' in config
    assert "OPENAI_API_KEY" not in config
    assert "SAXO_ACCESS_TOKEN" not in config
