"""``dashboard`` and ``converge``: the TrajViz dashboards on localhost, the Insight one preloaded with the session."""

from __future__ import annotations

import errno
import io
import json
import socket
import sys
import urllib.request

import gradio
import pytest
from agent_trajviz import MissingExtraError

from trajviz_obs.dashboard_cmd import build_preloaded_insight


def test_preloaded_app_has_load_event(fixture_session):
    app = build_preloaded_insight(str(fixture_session))
    assert any("tv-load-btn" in (getattr(d, "js", "") or "") for d in app.fns.values()) or any("tv-load-btn" in str(c) for c in app.config["dependencies"])


def test_preload_sets_the_trajectory_file_to_the_transcript(fixture_session):
    app = build_preloaded_insight(str(fixture_session))
    files = [b for b in app.blocks.values() if isinstance(b, gradio.File) and str(b.label).startswith("Trajectory")]
    assert len(files) == 1
    preload = [d for d in app.fns.values() if d.js and "tv-load-btn" in d.js and "setTimeout" in d.js]
    assert len(preload) == 1
    assert preload[0].outputs == [files[0]]
    assert preload[0].fn() == str(fixture_session)


def test_preloaded_file_keeps_its_real_path(fixture_session):
    """Gradio would hand TrajViz a cache copy, losing ``<id>/subagents/``; the transcript is served in place."""
    build_preloaded_insight(str(fixture_session))
    assert gradio.utils.is_static_file(str(fixture_session))


class _EofAfterProbe(io.StringIO):
    """A stdin that fetches the URL the command printed (proving the server is up), then reports EOF."""

    def __init__(self, stdout: io.StringIO) -> None:
        super().__init__("")
        self.stdout = stdout
        self.status: int | None = None

    def _probe(self) -> None:
        if self.status is None:
            url = json.loads(self.stdout.getvalue().splitlines()[0])["url"]
            with urllib.request.urlopen(url, timeout=30) as response:
                self.status = response.status

    def read(self, *args):
        self._probe()
        return super().read(*args)

    def readline(self, *args):
        self._probe()
        return super().readline(*args)

    def __iter__(self):
        self._probe()
        return super().__iter__()


def _serve(monkeypatch, argv: list[str]) -> tuple[int, list[str], _EofAfterProbe]:
    from trajviz_obs import cli

    out = io.StringIO()
    stdin = _EofAfterProbe(out)
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", out)
    code = cli.main(argv)
    return code, out.getvalue().splitlines(), stdin


def _argv(kind: str, transcript, data_root) -> list[str]:
    return (["dashboard", "--transcript", str(transcript)] if kind == "dashboard"
            else ["converge", "--data-root", str(data_root)])


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


@pytest.mark.parametrize("explicit_port", [False, True], ids=["any-port", "explicit-port"])
@pytest.mark.parametrize("kind", ["dashboard", "converge"])
def test_serves_until_stdin_closes(kind, explicit_port, fixture_session, tmp_path, monkeypatch):
    port = _free_port() if explicit_port else None
    argv = [*_argv(kind, fixture_session, tmp_path), *(["--port", str(port)] if port else [])]
    code, lines, stdin = _serve(monkeypatch, argv)
    assert code == 0
    assert len(lines) == 1
    url = json.loads(lines[0])["url"]
    assert url.startswith(f"http://127.0.0.1:{port}/" if port else "http://127.0.0.1:")
    assert stdin.status == 200
    with pytest.raises(OSError):
        urllib.request.urlopen(url, timeout=5)


def test_missing_app_extra_prints_only_the_error(fixture_session, run_cli, monkeypatch):
    from trajviz_obs import dashboard_cmd

    def missing() -> None:
        raise MissingExtraError("app", ["gradio"], "agent_trajviz.dashboard")

    monkeypatch.setattr(dashboard_cmd, "_dashboard", missing)
    code, out = run_cli(["dashboard", "--transcript", str(fixture_session)])
    assert code == 1
    assert out["error"]["code"] == "missing_extra" and out["error"]["extra"] == "app"


def test_missing_transcript_is_not_found(tmp_path, run_cli):
    code, out = run_cli(["dashboard", "--transcript", str(tmp_path / "gone.jsonl")])
    assert code == 1 and out["error"]["code"] == "not_found"


def test_missing_data_root_is_not_found(tmp_path, run_cli):
    code, out = run_cli(["converge", "--data-root", str(tmp_path / "gone")])
    assert code == 1 and out["error"]["code"] == "not_found"


def test_failing_plugin_is_reported_before_any_url(fixture_session, run_cli, fake_plugins):
    def broken() -> None:
        raise RuntimeError("boom")

    fake_plugins("broken", broken)
    code, out = run_cli(["dashboard", "--transcript", str(fixture_session)])
    assert code == 1 and out["error"]["code"] == "plugin_error" and "boom" in out["error"]["message"]


@pytest.mark.parametrize("kind", ["dashboard", "converge"])
def test_busy_port_is_port_busy(kind, fixture_session, tmp_path, run_cli, monkeypatch):
    """A --port another socket holds is ``port_busy``, found before the app is built."""
    from agent_trajviz import dashboard

    built = []
    monkeypatch.setattr(dashboard, "insight_app", lambda **kwargs: built.append("insight"))
    monkeypatch.setattr(dashboard, "converge_app", lambda **kwargs: built.append("converge"))
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        code, out = run_cli([*_argv(kind, fixture_session, tmp_path), "--port", str(port)])  # one JSON document
    assert code == 1 and out["error"]["code"] == "port_busy" and built == []
    assert str(port) in out["error"]["message"] and out["error"]["port"] == port


@pytest.mark.parametrize("stage", ["build", "launch"])
@pytest.mark.parametrize("kind", ["dashboard", "converge"])
def test_other_os_error_with_a_port_stays_internal(kind, stage, fixture_session, tmp_path, run_cli, monkeypatch):
    """Only a port that cannot be bound is ``port_busy``: an OSError from building or launching the app is not."""
    from agent_trajviz import dashboard

    def unwritable(*args, **kwargs):
        raise PermissionError(errno.EACCES, "Permission denied", str(tmp_path / "gradio-cache"))

    if stage == "build":  # converge builds its app inside launch_converge
        monkeypatch.setattr(dashboard, "insight_app" if kind == "dashboard" else "converge_app", unwritable)
    else:
        monkeypatch.setattr(gradio.Blocks, "launch", unwritable)
    code, out = run_cli([*_argv(kind, fixture_session, tmp_path), "--port", str(_free_port())])
    assert code == 3 and out["error"]["code"] == "internal" and "Permission denied" in out["error"]["message"]
