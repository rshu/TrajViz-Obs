"""The compat adapter: the real context window on load, the empty-session signal, labeler chat injection."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agent_trajviz import LoadError

from trajviz_obs import compat
from trajviz_obs.compat import EmptySession, labeler_chat, load_run, window_info
from trajviz_obs.errors import BridgeError


def test_window_override_reaches_pressure_stats(fixture_session):
    run = load_run(str(fixture_session), window_limit=1_000_000)
    stats = run.view("context_pressure").data["stats"]
    assert stats["window_limit"] == 1_000_000 and window_info(run)["window_source"] == "override"


def test_window_override_reaches_brief(fixture_session):
    # TrajViz 0.2.0's brief prints no window for this fixture (it names one only in a premature-compaction
    # issue), so assert on what the brief's analysis reads: the run-level pressure with no override argument.
    run = load_run(str(fixture_session), window_limit=1_000_000)
    assert run.context_pressure()["stats"]["window_limit"] == 1_000_000


def test_window_info_without_override_reports_trajviz_s(fixture_session):
    info = window_info(load_run(str(fixture_session)))
    assert info == {"window": 200_000, "window_source": "model", "window_note": ""}


def test_window_override_must_be_positive(fixture_session):
    with pytest.raises(ValueError):
        load_run(str(fixture_session), window_limit=0)


def test_empty_session_is_signalled(tmp_path):
    p = tmp_path / "s.jsonl"
    p.write_text('{"type":"ai-title","title":"x"}\n')
    with pytest.raises(EmptySession):
        load_run(str(p), current=True)
    with pytest.raises(LoadError):
        load_run(str(p))


@pytest.mark.parametrize("version", ["0.1.9", "0.3.0", "1.0.0", "unknown"])
def test_unsupported_trajviz_is_refused(monkeypatch, version):
    monkeypatch.setattr(compat, "TRAJVIZ_VERSION", version)
    with pytest.raises(BridgeError) as caught:
        compat.check_supported()
    assert caught.value.code == "unsupported_trajviz"


def test_labeler_chat_injection(fixture_session, tmp_path):
    import time

    from agent_trajviz.labeling import labeler, llm
    from agent_trajviz.labeling.labeler import label_trajectory

    original_call, original_sleep = llm.call_llm, time.sleep
    seen = []
    with labeler_chat(lambda system, user: seen.append(user) or '{"phase":"understand","action":"code_reading"}'):
        out = label_trajectory(str(fixture_session), str(tmp_path / "l.json"), base_url="x", api_key="x", model="m")
        labeler.time.sleep(60)  # the labeler's retry sleep is a no-op...
        assert time.sleep is original_sleep  # ...without touching the process-wide time.sleep
    assert seen and out["counts"]["assistant"] == len(seen)
    assert llm.call_llm is original_call and labeler.time.sleep is original_sleep


def test_labeler_chat_redacts_like_call_llm():
    from agent_trajviz.labeling import llm

    seen = []
    with labeler_chat(lambda system, user: seen.append((system, user)) or "reply"):
        reply = llm.call_llm(base_url="x", api_key="x", model="m", system_prompt="sys",
                             user_message="key sk-ant-abcdefghijklmnopqrstuvwxyz0123")
    assert reply == "reply" and seen == [("sys", "key [REDACTED]")]


# TrajViz plugins: loaded once per process before any load or view lookup.


def _obs_fake_plugin() -> None:
    """A plugin hook registering a format (``{"agent": "obsfake", "turns": [...]}``) and a view."""
    import agent_trajviz as tv

    tv.register_format(tv.Format(key="obsfake", label="Obs fake",
                                 detect=lambda doc: isinstance(doc, dict) and doc.get("agent") == "obsfake",
                                 parse=lambda doc: [{"role": t["role"], "text_preview": t["text"]} for t in doc["turns"]]))
    tv.register_view("obs_fake_view", lambda run: {"answer": 42}, title="Obs fake view")


def _obsfake_run(folder: Path, name: str = "run.json") -> str:
    path = folder / name
    path.write_text(json.dumps({"agent": "obsfake", "turns": [{"role": "user", "text": "hi"},
                                                            {"role": "assistant", "text": "hello"}]}))
    return str(path)


def test_plugins_load_once_per_process(fixture_session, fake_plugins, monkeypatch):
    import agent_trajviz

    calls, real = [], agent_trajviz.load_plugins
    monkeypatch.setattr(agent_trajviz, "load_plugins", lambda: calls.append(1) or real())
    fake_plugins("obs-fake", lambda: None)
    load_run(str(fixture_session))
    load_run(str(fixture_session))
    assert compat.load_plugins() == ["obs-fake"] and calls == [1]


def test_failing_plugin_is_plugin_error(fixture_session, fake_plugins, run_cli):
    runs = []

    def broken() -> None:
        runs.append(1)
        raise RuntimeError("boom")

    fake_plugins("broken", broken)
    for _ in range(2):  # the outcome is kept: the hook is not run again
        with pytest.raises(BridgeError) as e:
            load_run(str(fixture_session))
        assert e.value.code == "plugin_error" and "'broken'" in e.value.message and "boom" in e.value.message
    code, out = run_cli(["snapshot", "--transcript", str(fixture_session)])
    assert code == 1 and out["error"]["code"] == "plugin_error" and runs == [1]


def test_plugin_format_loads(fake_plugins, tmp_path):
    fake_plugins("obs-fake", _obs_fake_plugin)
    assert load_run(_obsfake_run(tmp_path)).format == "obsfake"


@pytest.mark.parametrize("command", ["catalog", "view", "schemas", "scorecard"])
def test_plugins_load_before_any_view_lookup_or_load(command, fake_plugins, run_cli, fixture_session, tmp_path):
    """Each command, run first in the process, sees the plugin's view (or reads the plugin's format)."""
    fake_plugins("obs-fake", _obs_fake_plugin)
    custom, artifacts = _obsfake_run(tmp_path), str(tmp_path / "artifacts")
    if command == "catalog":
        code, out = run_cli(["view", "--transcript", str(fixture_session)])
        assert code == 0 and "obs_fake_view" in [view["id"] for view in out["views"]]
    elif command == "view":
        code, out = run_cli(["view", "--transcript", custom, "--artifacts", artifacts, "obs_fake_view"])
        assert code == 0 and "| answer | 42 |" in out["inline"]["table"]
    elif command == "schemas":
        code, out = run_cli(["schemas", "--out", str(tmp_path / "schemas")])
        assert code == 0 and "obs_fake_view.schema.json" in out["files"]
    else:
        other = _obsfake_run(tmp_path, "other.json")
        code, out = run_cli(["scorecard", "--no-self", "--transcript", custom, "--artifacts", artifacts,
                             "--others", custom, other])
        assert code == 0 and [row.get("error") for row in out["rows"]] == [None, None]
