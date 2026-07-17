"""Local-only, single-job Web operator for the DB3 Saxo reconciliation gate."""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Iterable
from urllib.parse import urlsplit

from .connection import project_root


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
MAX_REQUEST_BYTES = 16_384
MAX_OUTPUT_LINES = 500
RECONCILE_COMMAND = (sys.executable, "-m", "market_db.incremental_update", "reconcile")
TOKEN_ENVIRONMENT_KEY = "SAXO_ACCESS_TOKEN"
_BEARER_PATTERN = re.compile(r"(?i)bearer\s+[^\s\"']+")
_JWT_PATTERN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def sanitized_line(value: str, token: str) -> str:
    selected = value.replace(token, "<redacted>") if token else value
    selected = _BEARER_PATTERN.sub("Bearer <redacted>", selected)
    return _JWT_PATTERN.sub("<redacted-jwt>", selected).rstrip("\r\n")


def child_environment(token: str) -> dict[str, str]:
    selected = os.environ.copy()
    selected.pop(TOKEN_ENVIRONMENT_KEY, None)
    selected[TOKEN_ENVIRONMENT_KEY] = token
    return selected


class JobAlreadyRunning(RuntimeError):
    pass


class InvalidAccessToken(ValueError):
    pass


class ReconcileJobManager:
    """Run one fixed reconcile command and expose only sanitized progress."""

    def __init__(
        self,
        *,
        popen_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        command: Iterable[str] = RECONCILE_COMMAND,
        cwd: Path | None = None,
    ) -> None:
        self._popen_factory = popen_factory
        self._command = tuple(command)
        self._cwd = cwd or project_root()
        self._lock = threading.Lock()
        self._current: dict[str, Any] | None = None

    def start(self, access_token: str) -> dict[str, Any]:
        token = access_token.strip()
        if not token or len(token) > 8_192 or any(ord(character) < 32 for character in token):
            raise InvalidAccessToken("有効なSaxo SIM tokenを入力してください。")

        with self._lock:
            if self._current is not None and self._current["status"] == "RUNNING":
                raise JobAlreadyRunning("reconcile jobは既に実行中です。")
            job_id = f"db3-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{secrets.token_hex(4)}"
            child_env = child_environment(token)
            process = self._popen_factory(
                list(self._command),
                cwd=self._cwd,
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                shell=False,
            )
            child_env.pop(TOKEN_ENVIRONMENT_KEY, None)
            self._current = {
                "job_id": job_id,
                "status": "RUNNING",
                "started_at_utc": utc_now(),
                "finished_at_utc": None,
                "exit_code": None,
                "command_id": "market_db.incremental_update.reconcile",
                "orders_or_prechecks_sent": 0,
                "output": deque(maxlen=MAX_OUTPUT_LINES),
            }
            worker = threading.Thread(
                target=self._collect,
                args=(job_id, process, token),
                name=f"saxo-db-{job_id}",
                daemon=True,
            )
            worker.start()
        return self.status()

    def _collect(self, job_id: str, process: subprocess.Popen[str], token: str) -> None:
        try:
            if process.stdout is not None:
                for line in process.stdout:
                    safe_line = sanitized_line(line, token)
                    if safe_line:
                        with self._lock:
                            if self._current is not None and self._current["job_id"] == job_id:
                                self._current["output"].append(safe_line)
            exit_code = int(process.wait())
            final_status = "PASS" if exit_code == 0 else "FAILED"
        except Exception as exc:  # Output only the exception class, never its token-bearing message.
            exit_code = 1
            final_status = "FAILED"
            with self._lock:
                if self._current is not None and self._current["job_id"] == job_id:
                    self._current["output"].append(f"operator runner failed: {type(exc).__name__}")
        finally:
            token = ""
        with self._lock:
            if self._current is not None and self._current["job_id"] == job_id:
                self._current["status"] = final_status
                self._current["exit_code"] = exit_code
                self._current["finished_at_utc"] = utc_now()

    def status(self) -> dict[str, Any]:
        with self._lock:
            if self._current is None:
                return {
                    "job_id": None,
                    "status": "IDLE",
                    "orders_or_prechecks_sent": 0,
                    "output": [],
                }
            return {
                key: list(value) if key == "output" else value
                for key, value in self._current.items()
            }


def allowed_browser_request(host: str, origin: str | None, port: int) -> bool:
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if host not in allowed_hosts or origin is None:
        return False
    try:
        parsed = urlsplit(origin)
        return (
            parsed.scheme == "http"
            and parsed.hostname in {"127.0.0.1", "localhost"}
            and parsed.port == port
            and not parsed.path.rstrip("/")
            and not parsed.query
            and not parsed.fragment
        )
    except ValueError:
        return False


def operator_html(csrf_token: str, script_nonce: str) -> bytes:
    return f"""<!doctype html>
<html lang="ja">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta name="csrf-token" content="{csrf_token}">
  <title>saxo_db DB3 Operator</title>
  <style nonce="{script_nonce}">
    :root {{ color-scheme: light; --ink:#10231c; --muted:#5d6f67; --paper:#f3f1e8; --card:#fffdf7; --line:#d8d4c5; --green:#116149; --red:#9f2f2f; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; font:15px/1.55 -apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif; color:var(--ink); background:radial-gradient(circle at top left,#dce9df,transparent 42%),var(--paper); }}
    main {{ width:min(900px,calc(100% - 32px)); margin:48px auto; }}
    header {{ margin-bottom:24px; }}
    h1 {{ margin:0 0 8px; font:600 34px/1.15 Georgia,serif; }}
    p {{ color:var(--muted); }}
    .badge {{ display:inline-block; padding:5px 10px; border:1px solid #91ad9f; border-radius:999px; color:var(--green); background:#edf7f0; font-weight:700; font-size:12px; }}
    .card {{ background:var(--card); border:1px solid var(--line); border-radius:18px; padding:24px; box-shadow:0 14px 40px rgba(32,50,42,.09); margin-top:18px; }}
    label {{ display:block; font-weight:700; margin-bottom:8px; }}
    input {{ width:100%; border:1px solid #a9b2ac; border-radius:10px; padding:13px 14px; font:14px ui-monospace,SFMono-Regular,Menlo,monospace; background:white; }}
    button {{ margin-top:14px; border:0; border-radius:10px; padding:12px 18px; background:var(--green); color:white; font-weight:800; cursor:pointer; }}
    button:disabled {{ opacity:.55; cursor:wait; }}
    .notice {{ padding:12px 14px; border-left:4px solid var(--green); background:#edf7f0; color:#294b3e; }}
    .status {{ display:flex; gap:10px; align-items:center; margin-bottom:12px; font-weight:800; }}
    .dot {{ width:10px; height:10px; border-radius:50%; background:#7d8b85; }}
    .dot.running {{ background:#d38818; }} .dot.pass {{ background:#14905f; }} .dot.failed {{ background:var(--red); }}
    pre {{ margin:0; min-height:120px; max-height:440px; overflow:auto; white-space:pre-wrap; word-break:break-word; border-radius:12px; padding:16px; color:#dcebe3; background:#10231c; font:12px/1.55 ui-monospace,SFMono-Regular,Menlo,monospace; }}
    .error {{ color:var(--red); font-weight:700; }}
  </style>
</head>
<body>
<main>
  <header><span class="badge">SIM / GET ONLY / LOOPBACK</span><h1>DB3 Reconciliation Operator</h1><p>DataVersion復旧、必要なfull-refetch、通常run連続2回を一つの固定jobで実行します。</p></header>
  <section class="card">
    <div class="notice">tokenはこのjobの子process環境だけで使用し、ファイル、DB、ログ、cookie、localStorageへ保存しません。</div>
    <p><label for="token">Saxo SIM 24時間token</label><input id="token" type="password" autocomplete="new-password" spellcheck="false" autocapitalize="off"></p>
    <button id="start" type="button">AI運用用 reconcile を開始</button>
    <p id="message" aria-live="polite"></p>
  </section>
  <section class="card">
    <div class="status"><span id="dot" class="dot"></span><span id="state">IDLE</span></div>
    <pre id="output">jobはまだ開始されていません。</pre>
  </section>
</main>
<script nonce="{script_nonce}">
const csrf = document.querySelector('meta[name="csrf-token"]').content;
const tokenInput = document.querySelector('#token');
const startButton = document.querySelector('#start');
const message = document.querySelector('#message');
const state = document.querySelector('#state');
const dot = document.querySelector('#dot');
const output = document.querySelector('#output');
let pollTimer = null;

function render(job) {{
  state.textContent = job.status;
  dot.className = `dot ${{job.status.toLowerCase()}}`;
  output.textContent = (job.output || []).join('\\n') || 'sanitized outputを待機しています。';
  output.scrollTop = output.scrollHeight;
  startButton.disabled = job.status === 'RUNNING';
  if (job.status !== 'RUNNING' && pollTimer) {{ clearInterval(pollTimer); pollTimer = null; }}
}}

async function readStatus() {{
  const response = await fetch('/api/status', {{ cache:'no-store', credentials:'same-origin' }});
  render(await response.json());
}}

startButton.addEventListener('click', async () => {{
  let token = tokenInput.value.trim();
  if (!token) {{ message.className = 'error'; message.textContent = 'tokenを入力してください。'; return; }}
  let requestBody = JSON.stringify({{ token }});
  tokenInput.value = '';
  token = '';
  startButton.disabled = true;
  message.className = '';
  message.textContent = 'reconcile jobを登録しています…';
  try {{
    const response = await fetch('/api/reconcile', {{
      method:'POST', credentials:'same-origin', cache:'no-store',
      headers:{{'Content-Type':'application/json','X-CSRF-Token':csrf}}, body:requestBody
    }});
    requestBody = '';
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || `HTTP ${{response.status}}`);
    render(result);
    message.textContent = 'jobを開始しました。画面を閉じてもserver process内で継続します。';
    if (!pollTimer) pollTimer = setInterval(readStatus, 2000);
  }} catch (error) {{
    requestBody = '';
    message.className = 'error';
    message.textContent = error.message;
    startButton.disabled = false;
  }}
}});

readStatus().catch(() => {{ message.className='error'; message.textContent='status取得に失敗しました。'; }});
</script>
</body>
</html>""".encode("utf-8")


class OperatorState:
    def __init__(self, manager: ReconcileJobManager, port: int) -> None:
        self.manager = manager
        self.port = port
        self.csrf_token = secrets.token_urlsafe(32)
        self.script_nonce = secrets.token_urlsafe(24)


def make_handler(state: OperatorState) -> type[BaseHTTPRequestHandler]:
    class OperatorRequestHandler(BaseHTTPRequestHandler):
        server_version = "saxo-db-operator"
        sys_version = ""

        def log_message(self, _format: str, *args: object) -> None:
            return

        def _headers(self, content_type: str, content_length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(content_length))
            self.send_header("Cache-Control", "no-store, max-age=0")
            self.send_header("Pragma", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Permissions-Policy", "camera=(), microphone=(), geolocation=()")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'none'; connect-src 'self'; base-uri 'none'; form-action 'self'; "
                f"frame-ancestors 'none'; script-src 'nonce-{state.script_nonce}'; "
                f"style-src 'nonce-{state.script_nonce}'",
            )

        def _send(self, status: int, body: bytes, content_type: str) -> None:
            self.send_response(status)
            self._headers(content_type, len(body))
            self.end_headers()
            self.wfile.write(body)

        def _json(self, status: int, payload: dict[str, Any]) -> None:
            self._send(
                status,
                json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8"),
                "application/json; charset=utf-8",
            )

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
            if self.path == "/":
                self._send(200, operator_html(state.csrf_token, state.script_nonce), "text/html; charset=utf-8")
                return
            if self.path == "/api/status":
                self._json(200, state.manager.status())
                return
            if self.path == "/health":
                self._json(200, {"status": "PASS", "bind": "loopback"})
                return
            self._json(404, {"error": "not found"})

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API.
            if self.path != "/api/reconcile":
                self._json(404, {"error": "not found"})
                return
            if not allowed_browser_request(
                self.headers.get("Host", ""), self.headers.get("Origin"), state.port
            ):
                self._json(403, {"error": "loopback origin required"})
                return
            if not secrets.compare_digest(
                self.headers.get("X-CSRF-Token", ""), state.csrf_token
            ):
                self._json(403, {"error": "invalid request token"})
                return
            if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
                self._json(415, {"error": "application/json required"})
                return
            try:
                content_length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                content_length = 0
            if not 1 <= content_length <= MAX_REQUEST_BYTES:
                self._json(413, {"error": "invalid request size"})
                return
            try:
                payload = json.loads(self.rfile.read(content_length))
                if not isinstance(payload, dict) or not isinstance(payload.get("token"), str):
                    raise ValueError
                access_token = payload.pop("token")
                result = state.manager.start(access_token)
                access_token = ""
                payload.clear()
            except (json.JSONDecodeError, UnicodeDecodeError, ValueError, InvalidAccessToken):
                self._json(400, {"error": "有効なSaxo SIM tokenを入力してください。"})
                return
            except JobAlreadyRunning as exc:
                self._json(409, {"error": str(exc)})
                return
            except OSError as exc:
                self._json(500, {"error": f"job start failed: {type(exc).__name__}"})
                return
            self._json(202, result)

    return OperatorRequestHandler


def serve(port: int = DEFAULT_PORT) -> None:
    if not 1_024 <= port <= 65_535:
        raise ValueError("port must be between 1024 and 65535")
    state = OperatorState(ReconcileJobManager(), port)
    server = ThreadingHTTPServer((LOOPBACK_HOST, port), make_handler(state))
    server.daemon_threads = True
    print(
        json.dumps(
            {
                "status": "READY",
                "url": f"http://{LOOPBACK_HOST}:{port}/",
                "token_persisted": False,
                "orders_or_prechecks_sent": 0,
            },
            sort_keys=True,
        ),
        flush=True,
    )
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Local-only DB3 reconciliation operator UI")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(list(argv) if argv is not None else None)
    serve(args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
