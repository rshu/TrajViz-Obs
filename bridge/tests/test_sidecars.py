import json
import os
from pathlib import Path

import pytest

from trajviz_obs.sidecars import MAX_DEPTH, AgentMeta, TranscriptFacts, agent_meta, scan_transcript


def _user(content, **fields) -> str:
    return json.dumps({"type": "user", "message": {"role": "user", "content": content}, **fields})


def _write(path: Path, lines: list[str], tail: bytes = b"") -> str:
    path.write_bytes("".join(line + "\n" for line in lines).encode() + tail)
    return str(path)


def test_scan_counts_only_human_prompts(tmp_session):
    f = scan_transcript(tmp_session.transcript)
    assert f.title == "Build the obs plugin" and f.prompt_count == 1 and f.prompts == ["fix the tests"]


def test_scan_counts_meta_rows(tmp_session):
    assert scan_transcript(tmp_session.transcript).meta_count == 1


def test_scan_vendored_fixture(fixture_session):
    f = scan_transcript(str(fixture_session))
    assert f.title is None and f.prompt_count == 1
    assert f.prompts[0].startswith("You are solving a real-world GitHub issue.") and len(f.prompts[0]) == 200


def test_scan_skips_malformed_lines_and_a_truncated_tail(tmp_path):
    path = _write(tmp_path / "s.jsonl", ["{not json", '"a string"', "[1, 2]", _user("first")],
                  tail=b'\xff\xfe not utf-8\n{"type": "user", "message": {"role": "user", "content": "half wri')
    assert scan_transcript(path) == TranscriptFacts(title=None, prompts=["first"], prompt_count=1, meta_count=0)


def test_scan_prompt_rules(tmp_path):
    lines = [
        _user("  <system-reminder>injected</system-reminder>"),
        _user([{"type": "tool_result", "tool_use_id": "t1", "content": "out"}]),
        _user([{"type": "text", "text": "line one"}, {"type": "image"}, {"type": "text", "text": "line two"}]),
        _user("x" * 500),
        _user("skill body", isMeta=True),
        _user(""),
        json.dumps({"type": "assistant", "message": {"role": "assistant", "content": "not a prompt"}}),
    ]
    f = scan_transcript(_write(tmp_path / "s.jsonl", lines))
    assert f.prompts == ["line one\nline two", "x" * 200] and f.prompt_count == 2 and f.meta_count == 1


def test_scan_keeps_the_last_title_and_reads_both_title_keys(tmp_path):
    lines = [json.dumps({"type": "ai-title", "aiTitle": "first"}), json.dumps({"type": "ai-title", "title": "second"}),
             json.dumps({"type": "ai-title", "aiTitle": ""})]
    assert scan_transcript(_write(tmp_path / "s.jsonl", lines)).title == "second"


def test_scan_max_prompts_caps_the_list_not_the_count(tmp_path):
    f = scan_transcript(_write(tmp_path / "s.jsonl", [_user(f"p{i}") for i in range(5)]), max_prompts=3)
    assert f.prompts == ["p0", "p1", "p2"] and f.prompt_count == 5


def test_agent_meta_from_nested_sidecar(tmp_session):
    meta = agent_meta(tmp_session.transcript)
    assert meta["abc"].description == "Builder check" and meta["abc"].agent_type == "general-purpose"


def test_agent_meta_without_a_session_folder(tmp_path):
    assert agent_meta(_write(tmp_path / "lonely.jsonl", [])) == {}


def _meta(folder: Path, agent_id: str, body) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    text = body if isinstance(body, str) else json.dumps(body)
    (folder / f"agent-{agent_id}.meta.json").write_text(text, encoding="utf-8")


def test_agent_meta_skips_malformed_files(tmp_session):
    sub = tmp_session.dir / tmp_session.id / "subagents"
    _meta(sub, "top", {"agentType": "Explore", "description": "Find callers"})
    _meta(sub, "broken", "{nope")
    _meta(sub, "list", [1])
    _meta(sub, "empty", {"agentType": 3})
    _meta(sub, "onlytype", {"agentType": "workflow-subagent"})
    (sub / "agent-notmeta.jsonl").write_text("{}\n")
    assert agent_meta(tmp_session.transcript) == {
        "abc": AgentMeta(agent_id="abc", description="Builder check", agent_type="general-purpose"),
        "top": AgentMeta(agent_id="top", description="Find callers", agent_type="Explore"),
        "onlytype": AgentMeta(agent_id="onlytype", description="", agent_type="workflow-subagent"),
    }


def test_agent_meta_depth_limit(tmp_session):
    sub = tmp_session.dir / tmp_session.id / "subagents"
    deepest = sub.joinpath(*[f"d{i}" for i in range(MAX_DEPTH)])
    _meta(deepest, "deep", {"agentType": "a", "description": "at the limit"})
    _meta(deepest / "more", "deeper", {"agentType": "a", "description": "past the limit"})
    meta = agent_meta(tmp_session.transcript)
    assert "deep" in meta and "deeper" not in meta


@pytest.mark.skipif(not hasattr(os, "symlink"), reason="needs symlinks")
def test_agent_meta_does_not_follow_symlinks(tmp_session, tmp_path):
    outside = tmp_path / "outside"
    _meta(outside, "linkeddir", {"agentType": "a", "description": "via a linked folder"})
    _meta(outside, "linkedfile", {"agentType": "a", "description": "via a linked file"})
    sub = tmp_session.dir / tmp_session.id / "subagents"
    (sub / "link").symlink_to(outside, target_is_directory=True)
    (sub / "agent-linkedfile.meta.json").symlink_to(outside / "agent-linkedfile.meta.json")
    assert set(agent_meta(tmp_session.transcript)) == {"abc"}
