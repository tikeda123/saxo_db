"""Manual SIM credential smoke test that never persists the response body."""

from __future__ import annotations

import json

from .saxo_client import SaxoAPIError, SaxoClient


def main() -> int:
    try:
        client = SaxoClient.from_environment()
        result = client.smoke_test()
        result.update(
            {
                "base_url": client.base_url,
                "request_count": client.request_count,
                "write_request_count": client.write_request_count,
                "status": "PASS",
            }
        )
    except (ValueError, SaxoAPIError) as exc:
        result = {
            "body_saved": False,
            "error_code": str(exc),
            "status": "BLOCKED",
            "token_saved": False,
        }
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
