"""Read-only MCP surface for explaining saxo_db products and managed series."""

from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from mcp.server.fastmcp import FastMCP

from .data_ui import inventory_series
from .instrument_reference import instrument_catalog_payload, reference_catalog_metadata, reference_for_key
from .read_api import DatabaseReader


SERVER_INSTRUCTIONS = """
saxo_dbが管理する市場商品の意味、価格系列、期間、品質、公式参照先を説明するための
読み取り専用サーバーです。商品定義とDBの現在状態を区別し、データがNOT_EVALUATED、
STALE、WARN、FAILの場合はPASSと言い換えないでください。投資助言、売買判断、シグナル、
注文処理は行わず、返却されたofficial_sourcesを根拠リンクとして示してください。
""".strip()

mcp = FastMCP("saxo_db", instructions=SERVER_INSTRUCTIONS)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    raise TypeError(f"unsupported value: {type(value).__name__}")


def _public(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=_json_default))


def _read_catalog() -> dict[str, Any]:
    reader = DatabaseReader()
    try:
        return _public(instrument_catalog_payload(reader, inventory_series(reader)))
    finally:
        reader.close()


def _select_instrument(instrument_key: str) -> dict[str, Any]:
    key = instrument_key.strip().lower()
    catalog = _read_catalog()
    for item in catalog["instruments"]:
        if item["instrument_key"] == key:
            return item
    raise ValueError("unknown instrument_key")


@mcp.tool()
def list_managed_instruments(category: str = "") -> dict[str, Any]:
    """管理対象商品を一覧化する。categoryはequity_reit, bond_credit, commodity, gold, fx。"""

    selected_category = category.strip().lower()
    allowed = {"", "equity_reit", "bond_credit", "commodity", "gold", "fx"}
    if selected_category not in allowed:
        raise ValueError("unknown category")
    catalog = _read_catalog()
    instruments = [
        {
            "instrument_key": item["instrument_key"],
            "short_name": item["short_name"],
            "display_name_ja": item["display_name_ja"],
            "instrument_type_ja": item["instrument_type_ja"],
            "category": item["category"],
            "summary_ja": item["summary_ja"],
            "managed_series": item["managed_series"],
        }
        for item in catalog["instruments"]
        if not selected_category or item["category"] == selected_category
    ]
    return {
        "catalog_id": catalog["catalog_id"],
        "scope_note_ja": catalog["scope_note_ja"],
        "instrument_count": len(instruments),
        "instruments": instruments,
    }


@mcp.tool()
def describe_instrument(instrument_key: str) -> dict[str, Any]:
    """商品内容、DB価格の意味、注意点、公式リンク、管理系列の要約を返す。"""

    return _select_instrument(instrument_key)


@mcp.tool()
def get_managed_series(instrument_key: str) -> dict[str, Any]:
    """指定商品の管理系列、足、期間、最新complete時刻、品質状態を返す。"""

    selected = _select_instrument(instrument_key)
    instrument_id = selected["managed_instrument"]["instrument_id"]
    reader = DatabaseReader()
    try:
        rows = [row for row in inventory_series(reader) if row.get("instrument_id") == instrument_id]
    finally:
        reader.close()
    public_rows = [
        {
            "series_id": row["series_id"],
            "role": row["role"],
            "layer": row.get("layer"),
            "layer_label": row["layer_label"],
            "price_basis": row.get("price_basis"),
            "source_dataset_id": row.get("source_dataset_id"),
            "row_count": row.get("row_count"),
            "min_time_utc": row.get("min_time_utc"),
            "latest_complete_time_utc": row.get("latest_complete_time_utc") or row.get("max_time_utc"),
            "quality_status": row.get("quality_status"),
            "freshness_status": row.get("freshness_status"),
            "status": row["status"],
        }
        for row in rows
    ]
    return _public(
        {
            "instrument_key": selected["instrument_key"],
            "symbol": selected["managed_instrument"]["symbol"],
            "series_count": len(public_rows),
            "series": public_rows,
        }
    )


@mcp.resource("saxo-db://instrument-catalog")
def instrument_catalog_resource() -> str:
    """Versioned product-definition catalog metadata and definitions."""

    return json.dumps(_read_catalog(), ensure_ascii=False, sort_keys=True)


@mcp.resource("saxo-db://instruments/{instrument_key}")
def instrument_resource(instrument_key: str) -> str:
    """One product definition plus its current managed-series summary."""

    return json.dumps(_select_instrument(instrument_key), ensure_ascii=False, sort_keys=True)


@mcp.prompt()
def explain_saxo_db_series(instrument_key: str, audience: str = "初心者") -> str:
    """商品とDB系列を誤解なく説明するための定型プロンプト。"""

    reference_for_key(instrument_key)
    if len(audience) > 40:
        raise ValueError("audience is too long")
    return (
        f"saxo_dbのdescribe_instrumentとget_managed_seriesを使い、instrument_key={instrument_key.lower()}を"
        f"{audience}向けの日本語で説明してください。商品そのもの、DB価格の意味、管理中の足・期間・"
        "最新complete時刻・品質状態、利用上の注意を分け、official_sourcesのリンクを示してください。"
        "投資助言、売買判断、将来予測はしないでください。"
    )


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
