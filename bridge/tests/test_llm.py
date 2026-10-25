"""LLM prompts: capture the prompts TrajViz would send, replay the replies the mod got for them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import make_session

from trajviz_obs.compat import load_run
from trajviz_obs.errors import BridgeError
from trajviz_obs.llm_cmd import apply_replies, direct, plan_prompts

JUDGMENT = '{"where":"CLAUDE.md","fix":"do x","also":"","confidence":"high"}'
LABEL = '{"phase":"implement","action":"implement_runtime_logic"}'


def _opts(fixture_session: Path, **overrides) -> dict:
    return {"transcript_path": str(fixture_session), "question": None, "history_path": None, "limit": 3,
            "language": None, "max_steps": 50, "assistant_only": False, **overrides}


def _apply_opts(fixture_session: Path, out_path: Path, **overrides) -> dict:
    return {**_opts(fixture_session, **overrides), "out_path": out_path}


def _replies(plan: dict, text: str) -> dict:
    return {"hash": plan["hash"], "replies": [{"index": p["index"], "text": text} for p in plan["prompts"]]}


def test_plan_fixes_captures_one_prompt_per_issue(fixture_run, fixture_session):
    p = plan_prompts(fixture_run, "fixes", **_opts(fixture_session))
    assert p["plan"]["calls"] == min(3, len(fixture_run.view("issues").data)) == len(p["prompts"])
    assert p["prompts"][0]["max_tokens"] == 2048 and p["prompts"][0]["timeout_s"] == 120
    assert "fix judge" in p["prompts"][0]["system"] and "Judge a fix" in p["prompts"][0]["prompt"]
    assert p["plan"]["approx_input_tokens"] > 0 and p["feature"] == "fixes"


def test_plan_fixes_respects_limit(tmp_path):
    run = load_run(make_session(tmp_path, steps=60))
    issues = len(run.view("issues").data)
    assert issues > 1
    p = plan_prompts(run, "fixes", **_opts(Path(run.source), limit=1))
    assert p["plan"]["calls"] == 1 and [q["index"] for q in p["prompts"]] == [0]


def test_apply_fixes_parses_judgments(fixture_run, fixture_session, tmp_artifacts):
    p = plan_prompts(fixture_run, "fixes", **_opts(fixture_session))
    out = apply_replies(fixture_run, "fixes", _replies(p, JUDGMENT),
                        **_apply_opts(fixture_session, tmp_artifacts / "unused.json"))
    assert out["issues"][0]["judgment"]["where"] == "CLAUDE.md" and out["errors"] == []
    assert "CLAUDE.md" in out["text"] and "do x" in out["text"]


def test_apply_refuses_stale_hash(fixture_run, fixture_session, tmp_artifacts):
    p = plan_prompts(fixture_run, "fixes", **_opts(fixture_session))
    with pytest.raises(BridgeError) as exc:
        apply_replies(fixture_run, "fixes", {**_replies(p, JUDGMENT), "hash": "0" * 64},
                      **_apply_opts(fixture_session, tmp_artifacts / "unused.json"))
    assert exc.value.code == "stale_prompts"


def test_hash_follows_the_transcript(tmp_path):
    path = make_session(tmp_path, steps=20)
    before = plan_prompts(load_run(path), "fixes", **_opts(Path(path)))["hash"]
    grown = make_session(tmp_path, steps=21)
    assert grown == path and plan_prompts(load_run(path), "fixes", **_opts(Path(path)))["hash"] != before


def test_apply_refuses_changed_analysis_history(fixture_run, fixture_session, tmp_artifacts):
    """The same question with a changed exchange is a different model request."""
    history = tmp_artifacts / "analyze.json"
    opts = _opts(fixture_session, question="Why?", history_path=history)
    plan = plan_prompts(fixture_run, "analyze", **opts)
    history.write_text(json.dumps({"history": [{"role": "assistant", "content": "A new prior answer."}]}))
    with pytest.raises(BridgeError) as exc:
        apply_replies(fixture_run, "analyze", _replies(plan, "Old answer"),
                      **opts, out_path=tmp_artifacts / "unused.json")
    assert exc.value.code == "stale_prompts"


@pytest.mark.parametrize("feature", ["analyze", "fixes"])
def test_hash_follows_prompt_language(fixture_run, fixture_session, feature):
    english = plan_prompts(fixture_run, feature, **_opts(fixture_session, question="Why?", language="English"))
    chinese = plan_prompts(fixture_run, feature, **_opts(fixture_session, question="Why?", language="Chinese"))
    assert english["prompts"][0]["system"] != chinese["prompts"][0]["system"]
    assert english["hash"] != chinese["hash"]


def test_hash_follows_context_window(fixture_session):
    plans = [plan_prompts(load_run(str(fixture_session), window_limit=window), "analyze",
                          **_opts(fixture_session)) for window in (100_000, 1_000_000)]
    assert plans[0]["hash"] != plans[1]["hash"]


def test_hash_follows_label_step_cap(fixture_run, fixture_session):
    short = plan_prompts(fixture_run, "label", **_opts(fixture_session, max_steps=1))
    long = plan_prompts(fixture_run, "label", **_opts(fixture_session, max_steps=2))
    assert short["hash"] != long["hash"]


@pytest.mark.parametrize("indexes", [[0, 0], [-1], [True]])
def test_apply_refuses_invalid_reply_indexes(fixture_run, fixture_session, tmp_artifacts, indexes):
    plan = plan_prompts(fixture_run, "fixes", **_opts(fixture_session))
    replies = {"hash": plan["hash"], "replies": [{"index": i, "text": JUDGMENT} for i in indexes]}
    with pytest.raises(BridgeError) as exc:
        apply_replies(fixture_run, "fixes", replies,
                      **_apply_opts(fixture_session, tmp_artifacts / "unused.json"))
    assert exc.value.code == "bad_replies"


def test_cli_unicode_prompt_size_is_bounded(run_cli, fixture_session, tmp_artifacts, monkeypatch):
    """The UTF-8 file can fit while the JSON stdout's Unicode escapes exceed its budget."""
    from trajviz_obs import llm_cmd

    monkeypatch.setattr(llm_cmd, "_capture", lambda *args, **kwargs: [("system", "界" * 60_000)])
    code, out = run_cli(["llm-prompt", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts),
                         "--feature", "analyze"])
    assert code == 0 and "prompts" not in out
    assert out["bytes"] < llm_cmd.INLINE_JSON_BYTES
    assert len(json.dumps(out)) < llm_cmd.INLINE_JSON_BYTES
    assert json.loads(Path(out["path"]).read_text())["prompts"][0]["prompt"] == "界" * 60_000


def test_analyze_history_round_trip(fixture_run, fixture_session, tmp_artifacts):
    history = tmp_artifacts / "analyze.json"
    first = plan_prompts(fixture_run, "analyze", **_opts(fixture_session, history_path=history))
    assert first["plan"]["calls"] == 1 and first["prompts"][0]["max_tokens"] == 2048
    assert "User:" not in first["prompts"][0]["prompt"] and "bottlenecks" in first["prompts"][0]["prompt"]
    reply = "The run spent most of its time reading files."
    out = apply_replies(fixture_run, "analyze", _replies(first, reply),
                        **_apply_opts(fixture_session, tmp_artifacts / "unused.json", history_path=history))
    assert out["reply"] == reply and out["history_path"] == str(history)
    assert json.loads(history.read_text())["history"][-1] == {"role": "assistant", "content": reply}

    second = plan_prompts(fixture_run, "analyze",
                          **_opts(fixture_session, history_path=history, question="Why step 4?"))
    prompt = second["prompts"][0]["prompt"]
    assert prompt.startswith("User: ") and f"Assistant: {reply}" in prompt and prompt.endswith("User: Why step 4?")
    assert second["hash"] != first["hash"]


@pytest.mark.parametrize("message", [None, "old answer", {"role": [], "content": "text"},
                                     {"role": "system", "content": "text"}, {"role": "user", "content": {}}])
def test_malformed_history_is_rejected_before_prompt_planning(message, fixture_session, tmp_artifacts, run_cli):
    history = tmp_artifacts / "analyze.json"
    original = json.dumps({"history": [message]})
    history.write_text(original)
    code, result = run_cli(["llm-prompt", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts),
                            "--feature", "analyze"])
    assert code == 1 and result["error"]["code"] == "bad_history"
    assert history.read_text() == original and not (tmp_artifacts / "prompts-analyze.json").exists()


def test_label_capture_and_apply(fixture_run, fixture_session, tmp_artifacts):
    p = plan_prompts(fixture_run, "label", **_opts(fixture_session))
    assert 0 < p["plan"]["calls"] <= 50 and p["prompts"][0]["max_tokens"] == 1024
    assert p["prompts"][0]["timeout_s"] == 120 and "Step #" in p["prompts"][0]["prompt"]
    sidecar = tmp_artifacts / "labels.json"
    out = apply_replies(fixture_run, "label", _replies(p, LABEL), **_apply_opts(fixture_session, sidecar))
    assert out["path"] == str(sidecar) and sidecar.exists()
    assert out["summary"]["classification_rate"] > 0 and "implement" in out["text"]


def test_label_max_steps_caps_the_prompts(fixture_run, fixture_session, tmp_artifacts):
    p = plan_prompts(fixture_run, "label", **_opts(fixture_session, max_steps=2))
    assert p["plan"]["calls"] == 2 and len(p["prompts"]) == 2
    sidecar = tmp_artifacts / "labels.json"
    out = apply_replies(fixture_run, "label", _replies(p, LABEL),
                        **_apply_opts(fixture_session, sidecar, max_steps=2, assistant_only=True))
    steps = json.loads(sidecar.read_text())["steps"]
    assert sum(s["label_source"] == "llm" for s in steps) == 2 and {s["role"] for s in steps} == {"assistant"}
    assert out["summary"]["classified"] == 2 and out["errors"] == []


def test_reply_error_is_reported(fixture_run, fixture_session, tmp_artifacts):
    p = plan_prompts(fixture_run, "fixes", **_opts(fixture_session))
    replies = {"hash": p["hash"], "replies": [{"index": 0, "error": "api-error 529"}]}
    out = apply_replies(fixture_run, "fixes", replies, **_apply_opts(fixture_session, tmp_artifacts / "unused.json"))
    assert "api-error 529" in out["errors"][0] and out["issues"][0]["judgment"] is None


def test_label_reply_error_is_reported(fixture_run, fixture_session, tmp_artifacts):
    p = plan_prompts(fixture_run, "label", **_opts(fixture_session, max_steps=3))
    assert p["plan"]["calls"] == 3
    replies = {"hash": p["hash"], "replies": [{"index": 0, "error": "api-error 529"}, {"index": 2, "text": LABEL}]}
    out = apply_replies(fixture_run, "label", replies,
                        **_apply_opts(fixture_session, tmp_artifacts / "labels.json", max_steps=3))
    assert out["errors"] == ["prompt 0: api-error 529", "prompt 1: no reply"]
    assert "api-error 529" in out["text"] and out["summary"]["classified"] == 1


def test_analyze_reply_error_is_reported(fixture_run, fixture_session, tmp_artifacts):
    history = tmp_artifacts / "analyze.json"
    p = plan_prompts(fixture_run, "analyze", **_opts(fixture_session, history_path=history))
    replies = {"hash": p["hash"], "replies": [{"index": 0, "error": "overloaded 529"}]}
    out = apply_replies(fixture_run, "analyze", replies,
                        **_apply_opts(fixture_session, tmp_artifacts / "unused.json", history_path=history))
    assert out["error"] == "overloaded 529" and "request failed" in out["reply"]


def test_direct_needs_configuration(fixture_run, fixture_session, tmp_artifacts, tmp_path, monkeypatch, no_llm_env):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(BridgeError) as exc:
        direct(fixture_run, "fixes", **_apply_opts(fixture_session, tmp_artifacts / "unused.json"))
    assert exc.value.code == "llm_not_configured" and exc.value.extra["missing"]


@pytest.fixture
def label_endpoint(tmp_path, monkeypatch, no_llm_env):
    """LABEL_* set, no .env, and the labeler's HTTP call replaced: ``calls`` lists each step message sent."""
    import agent_trajviz.labeling.llm as label_llm

    monkeypatch.chdir(tmp_path)
    for name, value in (("LABEL_BASE_URL", "http://127.0.0.1:9"), ("LABEL_API_KEY", "k"), ("LABEL_MODEL", "m")):
        monkeypatch.setenv(name, value)
    monkeypatch.setattr("agent_trajviz.labeling.labeler.time.sleep", lambda seconds: None)
    endpoint = {"calls": [], "fail": set()}

    def call_llm(base_url, api_key, model, system_prompt, user_message, provider="openai", temperature=None,
                 max_tokens=None, timeout=120):
        endpoint["calls"].append(user_message)
        if len(endpoint["calls"]) in endpoint["fail"]:
            raise ConnectionError("refused")
        return LABEL

    monkeypatch.setattr(label_llm, "call_llm", call_llm)
    return endpoint


def test_direct_label_labels_the_first_max_steps(fixture_run, fixture_session, tmp_artifacts, label_endpoint):
    planned = plan_prompts(fixture_run, "label", **_opts(fixture_session))["plan"]["calls"]
    assert planned > 2
    sidecar = tmp_artifacts / "labels.json"
    out = direct(fixture_run, "label", **_apply_opts(fixture_session, sidecar, max_steps=2))
    assert len(label_endpoint["calls"]) == 2 and out["summary"]["classified"] == 2 and out["errors"] == []
    assert out["path"] == str(sidecar) and json.loads(sidecar.read_text())["model"] == "m"


def test_direct_label_retry_is_one_step_and_failures_are_reported(fixture_run, fixture_session, tmp_artifacts,
                                                                  label_endpoint):
    label_endpoint["fail"] = {1, 3, 4}  # step 0 fails then succeeds on retry; step 1 fails twice
    out = direct(fixture_run, "label", **_apply_opts(fixture_session, tmp_artifacts / "labels.json", max_steps=3))
    messages = label_endpoint["calls"]
    assert len(messages) == 5 and messages[0] == messages[1] and messages[2] == messages[3]  # step 3: past the cap
    assert len(set(messages)) == 3
    assert out["errors"] == ["prompt 1: ConnectionError"] and out["summary"]["classified"] == 2


def test_cli_prompt_then_apply(run_cli, fixture_session, tmp_artifacts):
    base = ["--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts), "--feature", "fixes"]
    code, plan = run_cli(["llm-prompt", *base, "--limit", "3"])
    assert code == 0 and plan["plan"]["calls"] == 1 and Path(plan["path"]).exists()
    replies = tmp_artifacts / "replies-fixes.json"
    replies.write_text(json.dumps(_replies(plan, JUDGMENT)))
    code, out = run_cli(["llm-apply", *base, "--limit", "3", "--replies", str(replies)])
    assert code == 0 and out["issues"][0]["judgment"]["confidence"] == "high"
    code, out = run_cli(["llm-apply", *base, "--limit", "2", "--replies", str(replies)])
    assert code == 1 and out["error"]["code"] == "stale_prompts"


def test_cli_rejects_limit_over_cap(run_cli, fixture_session):
    code, out = run_cli(["fixes", "--transcript", str(fixture_session), "--limit", "9"])
    assert code == 2 and out["error"]["code"] == "usage"
