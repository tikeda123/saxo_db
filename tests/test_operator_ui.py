from __future__ import annotations

import io
import json
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from market_db.connection import project_root
from market_db.operator_ui import (
    InvalidAccessToken,
    JobAlreadyRunning,
    ReconcileJobManager,
    OperatorState,
    allowed_browser_request,
    make_handler,
    operator_html,
    sanitized_line,
)


class FakeProcess:
    def __init__(self, output: str, exit_code: int = 0, release: threading.Event | None = None):
        self.stdout = io.StringIO(output)
        self.exit_code = exit_code
        self.release = release

    def wait(self) -> int:
        if self.release is not None:
            assert self.release.wait(timeout=2)
        return self.exit_code


def wait_for_terminal(manager: ReconcileJobManager) -> dict[str, object]:
    for _ in range(200):
        status = manager.status()
        if status["status"] != "RUNNING":
            return status
        time.sleep(0.005)
    raise AssertionError("operator job did not finish")


def test_operator_runs_only_fixed_command_and_redacts_token(monkeypatch):
    secret = ".".join(("eyJheader", "payload", "signature"))
    calls = []

    def popen(command, **kwargs):
        calls.append((command, {**kwargs, "env": dict(kwargs["env"])}))
        return FakeProcess(f'{{"status":"PASS"}}\nBearer {secret}\n{secret}\n')

    monkeypatch.setenv("SAXO_ACCESS_TOKEN", "old-parent-value")
    manager = ReconcileJobManager(popen_factory=popen)
    manager.start(secret)
    status = wait_for_terminal(manager)

    command, kwargs = calls[0]
    assert command == [sys.executable, "-m", "market_db.incremental_update", "reconcile"]
    assert secret not in " ".join(command)
    assert kwargs["shell"] is False
    assert kwargs["stdin"] is not None
    assert Path(kwargs["cwd"]) == project_root()
    assert kwargs["env"]["SAXO_ACCESS_TOKEN"] == secret
    assert status["status"] == "PASS"
    serialized = json.dumps(status)
    assert secret not in serialized
    assert "<redacted>" in serialized
    assert status["orders_or_prechecks_sent"] == 0


def test_operator_rejects_empty_token_and_concurrent_job():
    release = threading.Event()
    manager = ReconcileJobManager(
        popen_factory=lambda command, **kwargs: FakeProcess("running\n", release=release)
    )
    with pytest.raises(InvalidAccessToken):
        manager.start("   ")
    manager.start("session-token")
    with pytest.raises(JobAlreadyRunning):
        manager.start("another-token")
    release.set()
    assert wait_for_terminal(manager)["status"] == "PASS"


def test_operator_page_never_uses_browser_storage_and_clears_password_input():
    page = operator_html("csrf", "nonce").decode("utf-8")
    assert 'type="password"' in page
    assert "tokenInput.value = '';" in page
    assert "(job.output || []).join('\\n')" in page
    assert "(job.output || []).join('\n')" not in page
    assert "localStorage." not in page
    assert "sessionStorage." not in page
    assert "document.cookie" not in page
    assert "shell=True" not in page
    assert 'id="oauth-start"' in page
    assert 'id="periodic-start"' in page
    assert "/api/oauth/status" in page
    assert "/api/periodic/status" in page
    assert 'href="http://127.0.0.1:8766/ui/overview"' in page
    assert 'href="http://127.0.0.1:8766/ui/catalog"' in page


def test_operator_requires_exact_loopback_origin_and_port():
    assert allowed_browser_request("127.0.0.1:8765", "http://127.0.0.1:8765", 8765)
    assert allowed_browser_request("localhost:8765", "http://localhost:8765", 8765)
    assert not allowed_browser_request("127.0.0.1:8765", None, 8765)
    assert not allowed_browser_request("127.0.0.1:8765", "http://evil.invalid", 8765)
    assert not allowed_browser_request("127.0.0.1:8765", "http://127.0.0.1:9999", 8765)


def test_generic_bearer_and_jwt_output_is_redacted():
    assert sanitized_line("Authorization: Bearer abc.def.ghi", "") == "Authorization: Bearer <redacted>"
    jwt = ".".join(("eyJabcdefgh", "abcdefghijk", "abcdefghijkl"))
    assert jwt not in sanitized_line(f"unexpected {jwt}", "")


def test_operator_http_is_no_store_and_rejects_cross_site_post():
    state = OperatorState(ReconcileJobManager(), 0)
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(state))
    state.port = int(server.server_port)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        url = f"http://127.0.0.1:{state.port}/"
        with urllib.request.urlopen(url, timeout=2) as response:
            page = response.read().decode("utf-8")
            assert response.headers["Cache-Control"].startswith("no-store")
            assert response.headers["X-Frame-Options"] == "DENY"
            assert "default-src 'none'" in response.headers["Content-Security-Policy"]
            assert "DB3 Reconciliation Operator" in page

        with urllib.request.urlopen(f"http://127.0.0.1:{state.port}/api/oauth/status", timeout=2) as response:
            auth = json.loads(response.read())
            assert auth["status"] == "AUTH_CONFIG_MISSING"
            assert auth["token_values_exposed"] is False

        request = urllib.request.Request(
            f"http://127.0.0.1:{state.port}/api/reconcile",
            data=b'{"token":"not-used"}',
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as captured:
            urllib.request.urlopen(request, timeout=2)
        assert captured.value.code == 403
        assert b"loopback origin required" in captured.value.read()
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)
