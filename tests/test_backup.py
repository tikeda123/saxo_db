from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from market_db.backup import (
    BackupError,
    create_backup,
    ensure_backup_path,
    retention_decision,
)


class FakeRegistry:
    def __init__(self):
        self.events = []

    def start(self, database, relative_path):
        self.events.append(("start", database, relative_path))
        return 41

    def finish(self, *args):
        self.events.append(("finish", *args))


def test_backup_creates_atomic_dump_manifest_and_registry_pass(monkeypatch, tmp_path):
    monkeypatch.setattr("market_db.backup.project_root", lambda: tmp_path)
    registry = FakeRegistry()
    commands = []

    def runner(command, *, stdin_path=None, stdout_path=None):
        commands.append((tuple(command), stdin_path, stdout_path))
        if stdout_path is not None:
            stdout_path.write_bytes(b"PGDMP\x01verified")
            return subprocess.CompletedProcess(command, 0, stdout=None, stderr=b"")
        return subprocess.CompletedProcess(command, 0, stdout=b"; archive list\n", stderr=b"")

    result = create_backup(
        "saxo_market",
        now=datetime(2026, 7, 17, 1, 2, 3, tzinfo=timezone.utc),
        registry=registry,
        command_runner=runner,
    )

    dump = tmp_path / result["dump_relative_path"]
    manifest = dump.with_suffix(".manifest.json")
    assert dump.read_bytes() == b"PGDMP\x01verified"
    assert json.loads(manifest.read_text(encoding="utf-8")) == result
    assert not dump.with_suffix(".dump.partial").exists()
    assert commands[0][0][-5:] == ("-U", "postgres", "-d", "saxo_market", "-Fc")
    assert commands[1][0][-2:] == ("pg_restore", "--list")
    assert registry.events[0] == (
        "start",
        "saxo_market",
        "backups/postgres/saxo_market_20260717T010203Z.dump",
    )
    assert registry.events[1][0:3] == ("finish", 41, "PASS")
    assert registry.events[1][5:] == (True, None)


def test_backup_failure_never_marks_pass_or_leaves_partial(monkeypatch, tmp_path):
    monkeypatch.setattr("market_db.backup.project_root", lambda: tmp_path)
    registry = FakeRegistry()

    def runner(command, *, stdin_path=None, stdout_path=None):
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr=b"not exposed")

    with pytest.raises(BackupError, match="PG_DUMP"):
        create_backup(
            "saxo_market",
            now=datetime(2026, 7, 17, tzinfo=timezone.utc),
            registry=registry,
            command_runner=runner,
        )
    assert registry.events[-1] == ("finish", 41, "FAILED", None, None, False, "PG_DUMP")
    assert not list((tmp_path / "backups/postgres").glob("*.partial"))


def test_retention_keeps_seven_daily_and_four_weekly_generations():
    paths = [
        Path(f"saxo_market_202607{day:02d}T120000Z.dump")
        for day in range(1, 18)
    ]
    decision = retention_decision(paths)
    assert len(decision.keep) >= 7
    assert paths[-1] in decision.keep
    assert set(decision.keep).isdisjoint(decision.delete)
    assert set(decision.keep) | set(decision.delete) == set(paths)


def test_retention_ignores_old_artifacts_outside_the_naming_contract():
    ignored = Path("saxo_research_v13_db2.dump")
    selected = Path("saxo_market_20260717T120000Z.dump")
    decision = retention_decision([ignored, selected, Path("unrelated.txt")])
    assert decision.keep == (selected,)
    assert decision.delete == ()


def test_retention_is_calculated_independently_for_each_database():
    same_day = [
        Path(f"{database}_20260717T120000Z.dump")
        for database in ("saxo_market", "saxo_research_v13", "saxo_forward_v13")
    ]
    decision = retention_decision(same_day)
    assert set(decision.keep) == set(same_day)
    assert decision.delete == ()


def test_backup_path_rejects_root_escape(monkeypatch, tmp_path):
    monkeypatch.setattr("market_db.backup.project_root", lambda: tmp_path)
    allowed = tmp_path / "backups/postgres/saxo_market_20260717T120000Z.dump"
    assert ensure_backup_path(allowed) == allowed.resolve()
    with pytest.raises(BackupError):
        ensure_backup_path(tmp_path / "elsewhere/saxo_market_20260717T120000Z.dump")
