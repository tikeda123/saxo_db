#!/usr/bin/env python3
"""Offline verification entrypoint for the Git-managed synthetic CSV seed."""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from market_db.bootstrap_seed import verify_seed


def main() -> int:
    result = verify_seed()
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
