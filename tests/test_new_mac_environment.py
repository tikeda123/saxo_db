from __future__ import annotations

import importlib.util
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "verify_new_mac_environment.py"
SPEC = importlib.util.spec_from_file_location("verify_new_mac_environment", SCRIPT_PATH)
assert SPEC is not None and SPEC.loader is not None
module = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(module)


def _write_repository_contract(root: Path) -> None:
    for relative in module.REQUIRED_REPOSITORY_FILES:
        selected = root / relative
        selected.parent.mkdir(parents=True, exist_ok=True)
        selected.write_text("placeholder\n", encoding="utf-8")
    (root / "compose.yaml").write_text(
        'image: postgres:18.4-bookworm\nports: ["127.0.0.1:54329:5432"]\n'
        "volumes: [saxo_pg18_data]\n",
        encoding="utf-8",
    )
    (root / ".gitignore").write_text(
        "\n".join(module.REQUIRED_GITIGNORE_ENTRIES) + "\n",
        encoding="utf-8",
    )


def _passing_runner(command: tuple[str, ...]) -> tuple[int, str]:
    return 0, "available"


def test_clean_clone_preflight_is_read_only_and_passes(tmp_path: Path, monkeypatch) -> None:
    _write_repository_contract(tmp_path)
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.sys, "version_info", (3, 12, 0))

    result = module.verify_environment(
        tmp_path,
        expect_clean_clone=True,
        runner=_passing_runner,
    )

    assert result["status"] == "PASS"
    assert result["read_only"] is True
    assert result["saxo_api_requests"] == 0
    assert result["database_connections"] == 0
    assert result["database_writes"] == 0
    assert result["orders_or_prechecks_sent"] == 0


def test_copied_local_state_blocks_clean_clone(tmp_path: Path, monkeypatch) -> None:
    _write_repository_contract(tmp_path)
    (tmp_path / ".runtime").mkdir()
    (tmp_path / "backups").mkdir()
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.sys, "version_info", (3, 12, 0))

    result = module.verify_environment(
        tmp_path,
        expect_clean_clone=True,
        runner=_passing_runner,
    )

    assert result["status"] == "BLOCKED"
    assert result["blockers"] == ["FRESH_CLONE_HAS_NO_COPIED_LOCAL_STATE"]
    local_state = next(
        check for check in result["checks"]
        if check["check_id"] == "FRESH_CLONE_HAS_NO_COPIED_LOCAL_STATE"
    )
    assert local_state["detail"] == ".runtime,backups"


def test_missing_tool_is_reported_without_stderr_or_secret_values(
    tmp_path: Path, monkeypatch
) -> None:
    _write_repository_contract(tmp_path)
    monkeypatch.setattr(module.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(module.sys, "version_info", (3, 12, 0))

    def runner(command: tuple[str, ...]) -> tuple[int, str]:
        if command[:2] == ("docker", "--version"):
            return 1, "sensitive stderr must not be forwarded"
        return 0, "available"

    result = module.verify_environment(tmp_path, runner=runner)

    assert result["status"] == "BLOCKED"
    docker = next(
        check for check in result["checks"] if check["check_id"] == "DOCKER_CLI_AVAILABLE"
    )
    assert docker["detail"] == "UNAVAILABLE"
    assert "sensitive" not in str(result)
