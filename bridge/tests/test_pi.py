"""Pi's recorded leaf selects one parent chain; historical siblings stay evidence."""

import copy
import hashlib
import json

import pytest
from agent_trajviz import LoadError

from trajviz_obs.compat import load_run
from trajviz_obs.pi import active_branch


def entries(version=3):
    def message(identity, parent, role, tokens, second):
        content = f"Controlled {identity}"
        return {"type": "message", "id": identity, "parentId": parent,
                "timestamp": f"2026-10-30T02:00:0{second}.000Z", "message": {
                    "role": role, "content": content if role == "user" else [{"type": "text", "text": content}],
                    "api": "openai-completions", "provider": "openai", "model": "controlled",
                    "timestamp": 1793325600000 + second * 1000, "stopReason": "stop",
                    "usage": {"input": tokens, "output": 0, "reasoning": 0, "cacheRead": 0, "cacheWrite": 0,
                              "totalTokens": tokens}}}
    return [{"type": "session", "version": version, "id": "pi-controlled", "cwd": "/controlled",
             "timestamp": "2026-10-30T02:00:00.000Z"}, message("user", None, "user", 0, 1),
            message("abandoned", "user", "assistant", 1000, 2), message("active", "user", "assistant", 22, 3)]


def write(tmp_path, rows):
    path = tmp_path / "pi.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    return path


@pytest.mark.parametrize("version", [2, 3])
def test_pi_active_branch_matches_session_manager_leaf_and_preserves_all_source_entries(tmp_path, version):
    rows = entries(version)
    original = copy.deepcopy(rows)
    path = write(tmp_path, rows)
    before = path.read_bytes()
    run = load_run(str(path))
    assert run.format == "pi" and len(run.steps) == 2 and run.metrics["tokens"]["total"] == 22
    assert [step["id"] for step in run.steps] == ["user", "active"]
    assert [step["raw_index"] for step in run.steps] == [1, 3]
    assert run.raw["_pi_entries"] == rows == original
    assert run.raw["_pi_active_entry_ids"] == ["user", "active"]
    assert run.sha256 == hashlib.sha256(before).hexdigest() and path.read_bytes() == before
    export = tmp_path / "trajectory.json"
    export.write_text(json.dumps(run.to_dict(include_raw=True)))
    restored = load_run(str(export))
    assert restored.steps == run.steps and restored.raw["_pi_entries"] == rows


def test_legacy_linear_pi_without_tree_links_keeps_existing_behavior(tmp_path):
    rows = entries(1)
    for row in rows[1:]:
        row.pop("parentId")
    run = load_run(str(write(tmp_path, rows)))
    assert len(run.steps) == 3 and run.metrics["tokens"]["total"] == 1022
    assert active_branch(rows) is rows


def test_future_pi_tree_version_is_refused(tmp_path):
    with pytest.raises(LoadError) as caught:
        load_run(str(write(tmp_path, entries(4))))
    assert caught.value.code == "unsupported_schema"


def test_pi_active_leaf_can_be_a_nonmessage_entry_with_model_metadata_ancestors(tmp_path):
    rows = entries()
    rows.insert(3, {"type": "model_change", "id": "model", "parentId": "user", "provider": "controlled",
                    "modelId": "selected-model", "timestamp": "2026-10-30T02:00:02.500Z"})
    rows[4]["parentId"] = "model"
    rows[4]["message"].pop("model")
    rows.append({"type": "label", "id": "label", "parentId": "active", "targetId": "active",
                 "label": "Selected", "timestamp": "2026-10-30T02:00:04.000Z"})
    run = load_run(str(write(tmp_path, rows)))
    assert len(run.steps) == 2 and run.steps[-1]["model_id"] == "selected-model"
    assert run.raw["_pi_active_entry_ids"] == ["user", "model", "active", "label"]


@pytest.mark.parametrize("invalid", ["missing-parent", "cycle", "self-cycle", "duplicate", "missing-link"])
def test_invalid_pi_active_tree_is_refused_without_guessing_a_branch(tmp_path, invalid):
    rows = entries()
    if invalid == "missing-parent":
        rows[-1]["parentId"] = "missing"
    elif invalid == "cycle":
        rows[1]["parentId"] = "active"
    elif invalid == "self-cycle":
        rows[-1]["parentId"] = "active"
    elif invalid == "duplicate":
        rows[-1]["id"] = "abandoned"
    else:
        rows[-1].pop("parentId")
    path = write(tmp_path, rows)
    before = path.read_bytes()
    with pytest.raises(LoadError) as caught:
        load_run(str(path))
    assert caught.value.code == "invalid_pi_branch" and path.read_bytes() == before
