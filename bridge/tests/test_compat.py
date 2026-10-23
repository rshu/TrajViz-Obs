"""The compat adapter: the real context window on load, the empty-session signal, labeler chat injection."""

from __future__ import annotations

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
