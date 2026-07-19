from __future__ import annotations

import pytest

from market_db.operate import LEGACY_DMI1_REVIEWS, build_parser, main


def test_operate_parser_exposes_only_allow_listed_procedure_commands():
    parser = build_parser()
    for command in (
        "acknowledge-quality", "resolve-quality", "record-quality-scope", "review-quality",
        "reconcile-dmi1-legacy", "start-backup", "finish-backup",
    ):
        parsed = parser.parse_args(
            {
                "acknowledge-quality": [command, "1", "--operator", "operator"],
                "resolve-quality": [command, "1", "--operator", "operator"],
                "record-quality-scope": [
                    command, "1", "--scope-kind", "INSTRUMENT", "--operator", "operator"
                ],
                "review-quality": [
                    command, "1", "--applicability", "UNKNOWN", "--operator", "operator"
                ],
                "reconcile-dmi1-legacy": [command, "--operator", "operator"],
                "start-backup": [command, "saxo_market", "backups/test.dump"],
                "finish-backup": [command, "1", "FAILED"],
            }[command]
        )
        assert parsed.command == command
    with pytest.raises(SystemExit):
        parser.parse_args(["sql", "SELECT 1"])


def test_legacy_reconciliation_plan_is_complete_and_has_explicit_recovery_runs():
    assert len(LEGACY_DMI1_REVIEWS) == 22
    assert len({item.event_id for item in LEGACY_DMI1_REVIEWS}) == 22
    assert sum(item.applicability == "CURRENT" for item in LEGACY_DMI1_REVIEWS) == 5
    assert sum(item.applicability == "HISTORICAL" for item in LEGACY_DMI1_REVIEWS) == 17
    assert all(
        item.superseded_by_run_id is not None
        for item in LEGACY_DMI1_REVIEWS if item.applicability == "HISTORICAL"
    )


def test_operate_output_does_not_echo_private_note(monkeypatch, capsys):
    monkeypatch.setattr(
        "market_db.operate.run",
        lambda args: {"command": args.command, "quality_event_id": args.quality_event_id, "status": "completed"},
    )
    assert main(["resolve-quality", "7", "--operator", "operator"]) == 0
    output = capsys.readouterr().out
    assert "resolution_note" not in output
