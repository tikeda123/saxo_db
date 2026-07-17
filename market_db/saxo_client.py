"""Strict SIM-only Saxo OpenAPI GET client with sanitized failures."""

from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Protocol


SIM_BASE_URL = "https://gateway.saxobank.com/sim/openapi"
CHART_PATH = "/chart/v3/charts"
SMOKE_PATH = "/port/v1/users/me"
MAX_CHART_COUNT = 1200
ALLOWED_ASSET_TYPES = frozenset({"Etf", "FxSpot"})
_DETAIL_PATH = re.compile(r"^/ref/v1/instruments/details/\d+/(Etf|FxSpot)$")
_SCHEDULE_PATH = re.compile(r"^/ref/v1/instruments/tradingschedule/\d+/(Etf|FxSpot)$")


@dataclass(frozen=True)
class HTTPResponse:
    status: int
    headers: Mapping[str, str]
    body: bytes


class Transport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HTTPResponse: ...


class UrllibTransport:
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        timeout: float,
    ) -> HTTPResponse:
        request = urllib.request.Request(url, headers=dict(headers), method=method)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return HTTPResponse(response.status, dict(response.headers.items()), response.read())
        except urllib.error.HTTPError as exc:
            return HTTPResponse(exc.code, dict(exc.headers.items()), exc.read())


class SaxoAPIError(RuntimeError):
    def __init__(self, code: str, status: int | None = None):
        super().__init__(code)
        self.code = code
        self.status = status

    def __repr__(self) -> str:
        return f"SaxoAPIError(code={self.code!r}, status={self.status!r})"


def sanitized_error_code(status: int) -> str:
    return {
        400: "BLOCKED_INVALID_REQUEST",
        401: "BLOCKED_TOKEN_EXPIRED",
        403: "BLOCKED_PERMISSION_OR_NETWORK_REPUTATION",
        404: "BLOCKED_INSTRUMENT_DRIFT",
        429: "BLOCKED_RATE_LIMIT",
        503: "FAILED_SERVICE_UNAVAILABLE",
    }.get(status, f"FAILED_HTTP_{status}")


def safe_rate_headers(headers: Mapping[str, str]) -> dict[str, str]:
    return {
        key: value
        for key, value in headers.items()
        if key.lower().startswith("x-ratelimit-")
        and any(part in key.lower() for part in ("-limit", "-remaining", "-reset"))
    }


def _allowed_path(path: str) -> bool:
    return path in {CHART_PATH, SMOKE_PATH, "/ref/v1/instruments"} or bool(
        _DETAIL_PATH.fullmatch(path) or _SCHEDULE_PATH.fullmatch(path)
    )


class SaxoClient:
    def __init__(
        self,
        access_token: str,
        *,
        base_url: str = SIM_BASE_URL,
        transport: Transport | None = None,
        sleep: Callable[[float], None] = time.sleep,
        timeout: float = 30.0,
    ):
        if base_url != SIM_BASE_URL:
            raise ValueError("only the Saxo SIM base URL is allowed")
        if not access_token:
            raise ValueError("SAXO_ACCESS_TOKEN is required")
        self._access_token = access_token
        self.base_url = base_url
        self._transport = transport or UrllibTransport()
        self._sleep = sleep
        self._timeout = timeout
        self.request_count = 0
        self.write_request_count = 0
        self.rate_limit_summary: dict[str, str] = {}

    @classmethod
    def from_environment(cls, **kwargs: Any) -> "SaxoClient":
        return cls(os.environ.get("SAXO_ACCESS_TOKEN", ""), **kwargs)

    def __repr__(self) -> str:
        return f"SaxoClient(base_url={self.base_url!r}, access_token=<redacted>)"

    def _get_json(self, path: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not _allowed_path(path):
            raise ValueError("Saxo endpoint is not allow-listed")
        query = urllib.parse.urlencode(params or {})
        url = self.base_url + path + (f"?{query}" if query else "")
        waits = (1.0, 2.0, 4.0)
        for attempt in range(4):
            try:
                response = self._transport.request(
                    "GET",
                    url,
                    headers={
                        "Accept": "application/json",
                        "Authorization": f"Bearer {self._access_token}",
                    },
                    timeout=self._timeout,
                )
            except (OSError, TimeoutError):
                if attempt < 3:
                    self._sleep(waits[attempt])
                    continue
                raise SaxoAPIError("FAILED_NETWORK") from None
            self.request_count += 1
            self.rate_limit_summary.update(safe_rate_headers(response.headers))
            if response.status == 429 and attempt < 3:
                reset = next(
                    (
                        value for key, value in response.headers.items()
                        if key.lower().endswith("-reset") and str(value).isdigit()
                    ),
                    None,
                )
                wait_seconds = max(waits[attempt], float(reset)) if reset is not None else waits[attempt]
                self._sleep(wait_seconds)
                continue
            if response.status != 200:
                raise SaxoAPIError(sanitized_error_code(response.status), response.status)
            try:
                payload = json.loads(response.body.decode("utf-8"), parse_float=Decimal)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise SaxoAPIError("FAILED_INVALID_JSON", response.status) from None
            if not isinstance(payload, dict):
                raise SaxoAPIError("FAILED_JSON_NOT_OBJECT", response.status)
            return payload
        raise SaxoAPIError("BLOCKED_RATE_LIMIT", 429)

    def smoke_test(self) -> dict[str, Any]:
        self._get_json(SMOKE_PATH)
        return {"endpoint_id": "users_me", "http_status": 200, "body_saved": False}

    def instrument_detail(self, uic: int, asset_type: str) -> dict[str, Any]:
        self._validate_instrument(uic, asset_type)
        return self._get_json(f"/ref/v1/instruments/details/{uic}/{asset_type}")

    def trading_schedule(self, uic: int, asset_type: str) -> dict[str, Any]:
        self._validate_instrument(uic, asset_type)
        return self._get_json(f"/ref/v1/instruments/tradingschedule/{uic}/{asset_type}")

    def chart(
        self,
        uic: int,
        asset_type: str,
        *,
        count: int = MAX_CHART_COUNT,
        mode: str | None = None,
        time_utc: str | None = None,
    ) -> dict[str, Any]:
        self._validate_instrument(uic, asset_type)
        if not 1 <= count <= MAX_CHART_COUNT:
            raise ValueError("chart Count must be between 1 and 1200")
        if (mode is None) != (time_utc is None):
            raise ValueError("chart Mode and Time must be supplied together")
        if mode is not None and mode not in {"From", "UpTo"}:
            raise ValueError("chart Mode must be From or UpTo")
        params: dict[str, Any] = {
            "Uic": uic,
            "AssetType": asset_type,
            "Horizon": 60,
            "Count": count,
            "FieldGroups": "Data,DisplayAndFormat,ChartInfo",
        }
        if mode is not None:
            params.update({"Mode": mode, "Time": time_utc})
        return self._get_json(CHART_PATH, params)

    @staticmethod
    def _validate_instrument(uic: int, asset_type: str) -> None:
        if not isinstance(uic, int) or uic <= 0:
            raise ValueError("UIC must be a positive integer")
        if asset_type not in ALLOWED_ASSET_TYPES:
            raise ValueError("AssetType must be Etf or FxSpot")
