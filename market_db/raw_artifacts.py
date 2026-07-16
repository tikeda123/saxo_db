"""Atomic, relative-path-only acquisition artifacts with sensitive-field redaction."""

from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

from .connection import project_root


_RUN_ID = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
_DROP_KEYS = frozenset({"accountkey", "clientkey", "authorization", "cookie", "tradableon"})


def utc_run_id(suffix: str) -> str:
    if not re.fullmatch(r"[0-9a-f]{8}", suffix):
        raise ValueError("run suffix must be eight lowercase hex characters")
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ-") + suffix


def sanitize_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): sanitize_payload(item)
            for key, item in value.items()
            if str(key).replace("_", "").lower() not in _DROP_KEYS
        }
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    return value


def _json_default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    raise TypeError(f"unsupported artifact value type: {type(value).__name__}")


def canonical_json_bytes(payload: Any) -> bytes:
    return (
        json.dumps(
            sanitize_payload(payload),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
        + "\n"
    ).encode("utf-8")


@dataclass(frozen=True)
class ArtifactRecord:
    relative_path: str
    sha256: str
    size_bytes: int
    row_count: int


class RunArtifacts:
    def __init__(self, run_id: str):
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("invalid acquisition run id")
        self.run_id = run_id
        self.relative_root = Path("data") / "acquisition" / "runs" / run_id
        self.root = project_root() / self.relative_root

    def write_json(self, relative_name: str, payload: Any, *, row_count: int) -> ArtifactRecord:
        selected = Path(relative_name)
        if selected.is_absolute() or ".." in selected.parts:
            raise ValueError("artifact path must be run-relative")
        target = self.root / selected
        target.parent.mkdir(parents=True, exist_ok=True)
        data = canonical_json_bytes(payload)
        temporary = target.with_suffix(target.suffix + ".tmp")
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        relative_path = str(target.relative_to(project_root()))
        return ArtifactRecord(relative_path, hashlib.sha256(data).hexdigest(), len(data), row_count)

    def write_manifest(self, payload: dict[str, Any]) -> ArtifactRecord:
        secured = {
            **payload,
            "orders_or_prechecks_sent": 0,
            "access_token_saved": False,
            "account_identifier_saved": False,
        }
        return self.write_json("run_manifest.json", secured, row_count=0)
