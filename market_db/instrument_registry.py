"""Canonical 13-instrument registry and drift checks without auto-substitution."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .connection import project_root


CANONICAL_SPEC = Path("specs/source_collection/v12_intraday_collection.json")


class InstrumentDriftError(RuntimeError):
    pass


@dataclass(frozen=True)
class CanonicalInstrument:
    category: str
    key: str
    symbol: str
    uic: int
    asset_type: str
    currency: str

    @property
    def price_basis(self) -> str:
        return "bid_ask_mid" if self.asset_type == "FxSpot" else "native_ohlc"

    @property
    def overlap_bars(self) -> int:
        return 72 if self.asset_type == "FxSpot" else 20


def load_canonical_instruments(path: Path | None = None) -> tuple[CanonicalInstrument, ...]:
    selected = path or project_root() / CANONICAL_SPEC
    payload = json.loads(selected.read_text(encoding="utf-8"))
    instruments = tuple(CanonicalInstrument(**item) for item in payload["instruments"])
    if len(instruments) != 13:
        raise RuntimeError("canonical instrument registry must contain exactly 13 instruments")
    keys = {(item.uic, item.asset_type) for item in instruments}
    if len(keys) != len(instruments):
        raise RuntimeError("canonical instrument registry has duplicate UIC/AssetType")
    return instruments


def validate_detail(instrument: CanonicalInstrument, detail: dict[str, Any]) -> dict[str, Any]:
    observed = {
        "uic": detail.get("Identifier", detail.get("Uic")),
        "asset_type": detail.get("AssetType"),
        "symbol": detail.get("Symbol"),
        "currency": detail.get("CurrencyCode", detail.get("Currency")),
        "exchange_id": detail.get("ExchangeId"),
    }
    mismatches: list[str] = []
    if observed["uic"] is not None and int(observed["uic"]) != instrument.uic:
        mismatches.append("UIC")
    if observed["asset_type"] != instrument.asset_type:
        mismatches.append("AssetType")
    if str(observed["symbol"] or "").casefold() != instrument.symbol.casefold():
        mismatches.append("Symbol")
    if str(observed["currency"] or "").upper() != instrument.currency:
        mismatches.append("Currency")
    if mismatches:
        raise InstrumentDriftError("BLOCKED_INSTRUMENT_DRIFT:" + ",".join(mismatches))
    return observed
