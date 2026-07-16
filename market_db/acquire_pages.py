"""Bounded, boundary-aware pagination for the Saxo chart endpoint."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from .instrument_registry import CanonicalInstrument
from .normalize_bars import BarQualityError, parse_utc
from .saxo_client import MAX_CHART_COUNT, SaxoClient


MAX_PAGES = 10_000


@dataclass(frozen=True)
class ChartPage:
    page_number: int
    request_mode: str
    request_time_utc: str
    payload: dict[str, Any]


def _page_times(payload: dict[str, Any]) -> list[datetime]:
    data = payload.get("Data")
    if not isinstance(data, list):
        raise BarQualityError("MISSING_CHART_DATA")
    times = [parse_utc(str(sample.get("Time", ""))) for sample in data]
    if times != sorted(times) or len(times) != len(set(times)):
        raise BarQualityError("NON_MONOTONIC_CHART_PAGE")
    return times


def iso_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_chart_pages(
    client: SaxoClient,
    instrument: CanonicalInstrument,
    *,
    mode: str,
    time_utc: datetime,
    count: int = MAX_CHART_COUNT,
    max_pages: int = MAX_PAGES,
    on_page: Callable[[ChartPage], None] | None = None,
) -> list[ChartPage]:
    if mode not in {"From", "UpTo"}:
        raise ValueError("paging mode must be From or UpTo")
    if not 1 <= max_pages <= MAX_PAGES:
        raise ValueError("max_pages is outside the safe range")

    pages: list[ChartPage] = []
    cursor = time_utc.astimezone(timezone.utc)
    for number in range(1, max_pages + 1):
        request_time = iso_utc(cursor)
        payload = client.chart(
            instrument.uic,
            instrument.asset_type,
            count=count,
            mode=mode,
            time_utc=request_time,
        )
        page = ChartPage(number, mode, request_time, payload)
        pages.append(page)
        if on_page is not None:
            on_page(page)
        times = _page_times(payload)
        if not times or len(times) < count:
            return pages
        next_cursor = times[-1] if mode == "From" else times[0]
        advanced = next_cursor > cursor if mode == "From" else next_cursor < cursor
        if not advanced:
            raise BarQualityError("CHART_CURSOR_DID_NOT_ADVANCE")
        cursor = next_cursor
    raise BarQualityError("CHART_PAGE_LIMIT_EXCEEDED")

