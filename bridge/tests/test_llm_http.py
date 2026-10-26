"""Exercise the real env-mode HTTP clients against a local, deterministic provider."""

from __future__ import annotations

import json
import threading
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import pytest

LABEL = '{"phase":"understand","action":"code_reading"}'
JUDGMENT = '{"where":"CLAUDE.md","fix":"check x","also":"","confidence":"high"}'


@pytest.fixture
def endpoint(monkeypatch, tmp_path, no_llm_env):
    calls, responses = [], deque()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_POST(self):
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            calls.append({"path": self.path, "headers": dict(self.headers), "body": request})
            status, text = responses.popleft() if responses else (200, "local answer")
            payload = ({"content": [{"type": "text", "text": text}]} if self.path.endswith("/messages")
                       else {"choices": [{"message": {"content": text}}]})
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.chdir(tmp_path)  # an ambient .env must never send a test to a real endpoint
    monkeypatch.setenv("NO_PROXY", "127.0.0.1")
    base = f"http://127.0.0.1:{server.server_port}/v1"
    for prefix in ("ANALYZE", "LABEL"):
        monkeypatch.setenv(f"{prefix}_BASE_URL", base)
        monkeypatch.setenv(f"{prefix}_API_KEY", f"test-{prefix.lower()}-key")
        monkeypatch.setenv(f"{prefix}_MODEL", f"local-{prefix.lower()}")
        monkeypatch.setenv(f"{prefix}_PROVIDER", "openai")
    yield SimpleNamespace(calls=calls, responses=responses, base=base)
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)
    assert not thread.is_alive()


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_ask_real_http_roundtrip_keeps_history_and_redacts_question(provider, endpoint, monkeypatch,
                                                                  fixture_session, tmp_path, run_cli):
    monkeypatch.setenv("ANALYZE_PROVIDER", provider)
    secret = "sk-ant-abcdefghijklmnopqrstuvwxyz0123"
    artifacts = tmp_path / "artifacts"
    args = ["ask", "--transcript", str(fixture_session), "--artifacts", str(artifacts)]
    endpoint.responses.extend([(200, "first answer"), (200, "second answer")])
    code, first = run_cli([*args, "--question", f"Explain failure with {secret}"])
    assert code == 0 and first["reply"] == "first answer"
    code, second = run_cli([*args, "--question", "What changed?"])
    assert code == 0 and second["reply"] == "second answer"
    assert len(endpoint.calls) == 2
    request = endpoint.calls[-1]
    assert request["path"] == ("/v1/messages" if provider == "anthropic" else "/v1/chat/completions")
    assert request["body"]["model"] == "local-analyze"
    headers = {name.lower(): value for name, value in request["headers"].items()}
    assert headers.get("x-api-key") == "test-analyze-key" if provider == "anthropic" else (
        headers.get("authorization") == "Bearer test-analyze-key")
    messages = request["body"]["messages"]
    assert messages[-2:] == [{"role": "assistant", "content": "first answer"},
                            {"role": "user", "content": "What changed?"}]
    assert "[REDACTED]" in json.dumps(messages) and secret not in json.dumps(endpoint.calls)
    history = json.loads(Path(second["history_path"]).read_text())["history"]
    assert len(history) == 4 and history[-1]["content"] == "second answer"


def test_fixes_real_http_parses_judgment(endpoint, fixture_session, tmp_path, run_cli):
    endpoint.responses.append((200, JUDGMENT))
    code, result = run_cli(["fixes", "--transcript", str(fixture_session), "--artifacts", str(tmp_path / "artifacts"),
                            "--limit", "1"])
    assert code == 0 and result["errors"] == []
    assert result["issues"][0]["judgment"]["fix"] == "check x"
    assert len(endpoint.calls) == 1 and endpoint.calls[0]["body"]["model"] == "local-analyze"


@pytest.mark.parametrize("provider", ["openai", "anthropic", " 'ANTHROPIC' # use messages "])
def test_label_real_http_uses_label_settings_and_retries_within_step_cap(provider, endpoint, monkeypatch,
                                                                       fixture_session, tmp_path, run_cli):
    monkeypatch.setenv("LABEL_PROVIDER", provider)
    # Exhaust the first step's retry, then classify the second. A retry must not consume the second step.
    endpoint.responses.extend([(500, "provider body must stay private"), (500, "provider body must stay private"),
                               (200, LABEL)])
    code, result = run_cli(["label", "--transcript", str(fixture_session), "--artifacts", str(tmp_path / "artifacts"),
                            "--max-steps", "2", "--assistant-only"])
    assert code == 0 and result["errors"] == ["prompt 0: HTTPError"]
    assert result["summary"]["classified"] == 1 and len(endpoint.calls) == 3
    expected = "/v1/chat/completions" if provider == "openai" else "/v1/messages"
    assert all(call["path"] == expected and call["body"]["model"] == "local-label" for call in endpoint.calls)
    assert endpoint.calls[0]["body"] == endpoint.calls[1]["body"]
    assert endpoint.calls[1]["body"] != endpoint.calls[2]["body"]
    assert "provider body must stay private" not in json.dumps(result)
    sidecar = json.loads(Path(result["path"]).read_text())
    assert {step["role"] for step in sidecar["steps"]} == {"assistant"}
    assert sum(step["label_source"] == "llm" for step in sidecar["steps"]) == 1


def test_ask_http_error_is_safe_and_preserved_in_history(endpoint, fixture_session, tmp_path, run_cli):
    endpoint.responses.append((401, "response includes a pretend credential: sk-sensitive"))
    code, result = run_cli(["ask", "--transcript", str(fixture_session), "--artifacts", str(tmp_path / "artifacts"),
                            "--question", "Why?"])
    assert code == 0 and "HTTP 401" in result["reply"]
    assert "sk-sensitive" not in json.dumps(result)
    history = json.loads(Path(result["history_path"]).read_text())["history"]
    assert history[-1]["content"] == result["reply"]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
def test_label_generation_settings_reach_real_provider_requests(provider, endpoint, monkeypatch, fixture_session,
                                                               tmp_path, run_cli):
    monkeypatch.setenv("LABEL_PROVIDER", provider)
    monkeypatch.setenv("LABEL_MAX_TOKENS", "321")
    monkeypatch.setenv("LABEL_TEMPERATURE", "0.17")
    common = ["label", "--transcript", str(fixture_session), "--artifacts", str(tmp_path / "artifacts"),
              "--max-steps", "1", "--assistant-only"]
    endpoint.responses.extend([(200, LABEL), (200, LABEL)])
    code, first = run_cli(common)
    assert code == 0 and first["summary"]["classified"] == 1
    assert endpoint.calls[-1]["body"]["max_tokens"] == 321
    assert endpoint.calls[-1]["body"]["temperature"] == 0.17
    code, second = run_cli([*common, "--max-tokens", "222", "--temperature", "0.37"])
    assert code == 0 and second["summary"]["classified"] == 1
    assert endpoint.calls[-1]["body"]["max_tokens"] == 222
    assert endpoint.calls[-1]["body"]["temperature"] == 0.37
