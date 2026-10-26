"""Custom taxonomy, deterministic sidecar options and real provider settings stay aligned with plans."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from trajviz_obs import llm_cmd
from trajviz_obs.errors import BridgeError

CUSTOM = '{"phase":"inspect","action":"read_source"}'


@pytest.fixture
def taxonomy(tmp_path):
    path = tmp_path / "custom-taxonomy.md"
    path.write_text("# Taxonomy (v42)\n### inspect\n- `read_source`: Read project source.\n")
    return path


def options(source, **extra):
    return {"transcript_path": str(source), "question": None, "history_path": None, "limit": 3,
            "language": None, "max_steps": 2, "assistant_only": False, **extra}


def replies(plan):
    return {"hash": plan["hash"], "replies": [{"index": p["index"], "text": CUSTOM} for p in plan["prompts"]]}


def test_custom_taxonomy_capture_replay_and_deterministic_user_labels(fixture_run, fixture_session, taxonomy, tmp_path):
    settings = {"taxonomy_path": str(taxonomy), "max_content_chars": 128, "max_tokens": 256,
                "delay": 0.25, "user_phase": "request", "user_action": "request_change"}
    args = options(fixture_session, label_options=settings)
    plan = llm_cmd.plan_prompts(fixture_run, "label", **args)
    assert "### inspect" in plan["prompts"][0]["system"]
    assert all(p["max_tokens"] == 256 and p["delay_s"] == 0.25 for p in plan["prompts"])
    output = tmp_path / "labels.json"
    result = llm_cmd.apply_replies(fixture_run, "label", replies(plan), **args, out_path=output)
    sidecar = json.loads(output.read_text())
    assert sidecar["taxonomy_version"] == "v42" and result["summary"]["classified"] == 2
    assert sidecar["defaults"]["user"] == {"phase": "request", "action": "request_change"}
    assert all(s["phase"] == "request" and s["action"] == "request_change"
               for s in sidecar["steps"] if s["role"] == "user")
    assert sum(s.get("phase") == "inspect" and s.get("action") == "read_source" for s in sidecar["steps"]) == 2


def test_taxonomy_change_invalidates_replies(fixture_run, fixture_session, taxonomy, tmp_path):
    args = options(fixture_session, label_options={"taxonomy_path": str(taxonomy)})
    plan = llm_cmd.plan_prompts(fixture_run, "label", **args)
    taxonomy.write_text(taxonomy.read_text() + "\nExtra classification instructions changed.\n")
    with pytest.raises(BridgeError) as caught:
        llm_cmd.apply_replies(fixture_run, "label", replies(plan), **args, out_path=tmp_path / "labels.json")
    assert caught.value.code == "stale_prompts" and not (tmp_path / "labels.json").exists()


def test_taxonomy_validation_and_capture_use_the_same_snapshot_when_original_changes(taxonomy, monkeypatch,
                                                                                   fixture_run, fixture_session):
    from agent_trajviz.labeling import taxonomy as module

    original, real, reads = taxonomy.read_text(), module.load_taxonomy, []
    def load(captured):
        captured = Path(captured)
        reads.append(captured)
        assert captured != taxonomy and captured.read_text() == original
        taxonomy.write_text("The original was edited while the operation was running.")
        return real(captured)
    monkeypatch.setattr(module, "load_taxonomy", load)
    plan = llm_cmd.plan_prompts(fixture_run, "label", **options(fixture_session,
                               label_options={"taxonomy_path": str(taxonomy)}))
    assert reads and "### inspect" in plan["prompts"][0]["system"]
    assert "original was edited" not in plan["prompts"][0]["system"]


@pytest.mark.parametrize("change", [{"assistant_only": True}, {"max_steps": 60},
    {"label_options": {"user_phase": "request"}}, {"label_options": {"user_action": "request_change"}}])
def test_sidecar_options_bind_hash_even_when_all_model_prompts_are_identical(change, fixture_run, fixture_session):
    first = llm_cmd.plan_prompts(fixture_run, "label", **options(fixture_session, max_steps=50))
    changed = options(fixture_session, max_steps=50)
    changed.update(change)
    modified = llm_cmd.plan_prompts(fixture_run, "label", **changed)
    assert first["prompts"] == modified["prompts"] and first["hash"] != modified["hash"]


@pytest.mark.parametrize("body,error", [(None, "not_found"), (b"No phase or action headings", "bad_taxonomy"),
                                        (b"\xff", "bad_taxonomy")])
def test_bad_taxonomy_returns_a_useful_cli_error_without_a_partial_plan(body, error, fixture_session, tmp_path, run_cli):
    taxonomy = tmp_path / "taxonomy.md"
    if body is not None:
        taxonomy.write_bytes(body)
    code, result = run_cli(["llm-prompt", "--feature", "label", "--transcript", str(fixture_session),
                           "--artifacts", str(tmp_path / "artifacts"), "--taxonomy", str(taxonomy)])
    assert code == 1 and result["error"]["code"] == error and result["error"]["path"] == str(taxonomy)
    assert not (tmp_path / "artifacts").exists()


@pytest.mark.parametrize("mode", ["direct", "replay", "plan"])
def test_taxonomy_is_protected_from_every_label_artifact(mode, fixture_run, fixture_session, tmp_path, run_cli):
    taxonomy = tmp_path / "prompts-label.json"
    original = "# Taxonomy (v42)\n### inspect\n- `read_source`: Source.\n"
    taxonomy.write_text(original)
    args = options(fixture_session, label_options={"taxonomy_path": str(taxonomy)})
    if mode == "plan":
        code, result = run_cli(["llm-prompt", "--feature", "label", "--transcript", str(fixture_session),
                               "--artifacts", str(tmp_path), "--taxonomy", str(taxonomy)])
        assert code == 1 and result["error"]["code"] == "bad_output"
    else:
        plan = llm_cmd.plan_prompts(fixture_run, "label", **args)
        with pytest.raises(BridgeError) as caught:
            if mode == "direct":
                llm_cmd.direct(fixture_run, "label", **args, out_path=taxonomy)
            else:
                llm_cmd.apply_replies(fixture_run, "label", replies(plan), **args, out_path=taxonomy)
        assert caught.value.code == "bad_output"
    assert taxonomy.read_text() == original


@pytest.fixture
def provider(monkeypatch, tmp_path, no_llm_env):
    from agent_trajviz.labeling import llm

    monkeypatch.chdir(tmp_path)
    for name, value in (("LABEL_BASE_URL", "http://127.0.0.1:9"), ("LABEL_API_KEY", "synthetic"), ("LABEL_MODEL", "test")):
        monkeypatch.setenv(name, value)
    calls, sleeps = [], []
    def call(base_url, api_key, model, system_prompt, user_message, provider="openai", temperature=None,
             max_tokens=None, timeout=120):
        calls.append({"system": system_prompt, "prompt": user_message, "temperature": temperature,
                      "max_tokens": max_tokens})
        return CUSTOM
    monkeypatch.setattr(llm, "call_llm", call)
    monkeypatch.setattr(llm_cmd, "time", SimpleNamespace(sleep=sleeps.append))
    return SimpleNamespace(calls=calls, sleeps=sleeps)


def test_env_label_honors_upstream_generation_settings_and_paces_only_actual_calls(provider, monkeypatch, taxonomy,
                                                                                  fixture_run, fixture_session, tmp_path):
    monkeypatch.setenv("LABEL_MAX_TOKENS", " '1536' # configured reply cap")
    monkeypatch.setenv("LABEL_TEMPERATURE", "0.7")
    result = llm_cmd.direct(fixture_run, "label", **options(fixture_session, label_options={
        "taxonomy_path": str(taxonomy), "delay": 0.25, "max_content_chars": 128}), out_path=tmp_path / "labels.json")
    assert result["summary"]["classified"] == 2 and len(provider.calls) == 2
    assert all(c["max_tokens"] == 1536 and c["temperature"] == 0.7 and "### inspect" in c["system"]
               for c in provider.calls)
    assert provider.sleeps == [0.25]  # the remaining unknown assistant records never wait
    plan = llm_cmd.plan_prompts(fixture_run, "label", **options(fixture_session))
    assert all(p["max_tokens"] == 1024 for p in plan["prompts"])  # host session calls keep their stated cap


@pytest.mark.parametrize("tokens,temperature", [("invalid", "nan"), ("-5", "-0.1"), ("0", "inf")])
def test_invalid_env_generation_settings_use_upstream_defaults(tokens, temperature, provider, monkeypatch, taxonomy,
                                                              fixture_run, fixture_session, tmp_path):
    monkeypatch.setenv("LABEL_MAX_TOKENS", tokens)
    monkeypatch.setenv("LABEL_TEMPERATURE", temperature)
    llm_cmd.direct(fixture_run, "label", **options(fixture_session, max_steps=1,
        label_options={"taxonomy_path": str(taxonomy)}), out_path=tmp_path / "labels.json")
    assert provider.calls[0]["max_tokens"] == 1024 and provider.calls[0]["temperature"] == 0.3


def test_explicit_reply_cap_overrides_environment(provider, monkeypatch, taxonomy, fixture_run, fixture_session, tmp_path):
    monkeypatch.setenv("LABEL_MAX_TOKENS", "1536")
    llm_cmd.direct(fixture_run, "label", **options(fixture_session, max_steps=1,
        label_options={"taxonomy_path": str(taxonomy), "max_tokens": 256}), out_path=tmp_path / "labels.json")
    assert provider.calls[0]["max_tokens"] == 256


def test_identical_truncated_prompts_cannot_bypass_the_real_provider_step_cap(provider, taxonomy, fixture_run,
                                                                          fixture_session, tmp_path):
    result = llm_cmd.direct(fixture_run, "label", **options(fixture_session, max_steps=2,
        label_options={"taxonomy_path": str(taxonomy), "max_content_chars": 1}), out_path=tmp_path / "labels.json")
    assert len(provider.calls) == 2 and provider.calls[0]["prompt"] == provider.calls[1]["prompt"] == "S"
    assert result["summary"]["classified"] == 2 and result["errors"] == []
