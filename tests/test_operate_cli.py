from __future__ import annotations

import pytest

from market_db.operate import build_parser, main


def test_operate_parser_exposes_only_four_procedure_commands():
    parser = build_parser()
    for command in ("acknowledge-quality", "resolve-quality", "start-backup", "finish-backup"):
        parsed = parser.parse_args(
            {
                "acknowledge-quality": [command, "1", "--operator", "operator"],
                "resolve-quality": [command, "1", "--operator", "operator"],
                "start-backup": [command, "saxo_market", "backups/test.dump"],
                "finish-backup": [command, "1", "FAILED"],
            }[command]
        )
        assert parsed.command == command
    with pytest.raises(SystemExit):
        parser.parse_args(["sql", "SELECT 1"])


def test_operate_output_does_not_echo_private_note(monkeypatch, capsys):
    monkeypatch.setattr(
        "market_db.operate.run",
        lambda args: {"command": args.command, "quality_event_id": args.quality_event_id, "status": "completed"},
    )
    assert main(["resolve-quality", "7", "--operator", "operator"]) == 0
    output = capsys.readouterr().out
    assert "resolution_note" not in output
