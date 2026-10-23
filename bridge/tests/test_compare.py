"""``compare``, ``scorecard`` and ``labels``: TrajViz's two-run comparison, N-run scorecard and label views."""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import pytest
from conftest import make_session

from trajviz_obs.compare_cmd import compare_sessions, label_views_payload, scorecard
from trajviz_obs.compat import labeler_chat
from trajviz_obs.errors import BridgeError


def _label(transcript: str, dest: Path, phases: list[str]) -> str:
    """*transcript* labelled to the sidecar *dest* through :func:`labeler_chat`, cycling through *phases*."""
    from agent_trajviz.labeling.labeler import label_trajectory

    actions = {"understand": "code_reading", "implement": "implement_runtime_logic", "validate": "validation_run"}
    cycle = itertools.cycle(phases)

    def chat(system: str, user: str) -> str:
        phase = next(cycle)
        return json.dumps({"phase": phase, "action": actions[phase]})

    with labeler_chat(chat):
        label_trajectory(transcript, str(dest), base_url="x", api_key="x", model="m")
    return str(dest)


@pytest.fixture
def sidecar_path(fixture_session, tmp_path) -> str:
    """The fixture labelled by a fake model: every step ``understand``/``code_reading``."""
    return _label(str(fixture_session), tmp_path / "labels.json", ["understand"])


@pytest.fixture
def other_sidecar(synthetic_session, tmp_path) -> str:
    """:func:`synthetic_session` labelled ``understand``, ``implement``, ``validate`` in turn."""
    return _label(synthetic_session, tmp_path / "other-labels.json", ["understand", "implement", "validate"])


def test_compare_orientation(fixture_run, synthetic_session, tmp_artifacts):
    out = compare_sessions(fixture_run, synthetic_session, anchor_patch=None, fuzzy=False, reverse=False, out_dir=tmp_artifacts)
    assert out["orientation"]["compared"].endswith("django__django-11477.jsonl") and Path(out["path"]).exists()


def test_scorecard_current_is_first_row(fixture_run, synthetic_session, tmp_artifacts):
    assert scorecard(fixture_run, [synthetic_session], fuzzy=False, labels=None, no_self=False, out_dir=tmp_artifacts)["rows"][0]["path"].endswith("11477.jsonl")


def test_label_views_payload(sidecar_path): assert "classification_rate" in label_views_payload(sidecar_path, None)["summary"]


def test_compare_file_summary_and_text(fixture_run, synthetic_session, tmp_artifacts):
    out = compare_sessions(fixture_run, synthetic_session, anchor_patch=None, fuzzy=False, reverse=False,
                           out_dir=tmp_artifacts)
    stem = Path(synthetic_session).stem
    assert out["path"] == str(tmp_artifacts / "compare" / f"{stem}.json")
    assert out["orientation"] == {"reference": synthetic_session, "compared": fixture_run.source}
    saved = json.loads(Path(out["path"]).read_text())
    assert saved["orientation"] == out["orientation"]
    assert set(saved["views"]) == {"comparison_summary", "milestone_timeline", "segment_costs", "divergence",
                                   "anchor_classes", "evaluation_layers"}
    assert out["summary"] == saved["views"]["comparison_summary"]["data"]
    outcome = out["summary"]["outcome"]
    assert outcome["reference_filename"] == f"{stem}.jsonl" and outcome["compared_steps"] == 24
    text = out["text"]
    for needle in (f"reference `{stem}.jsonl`", "compared `django__django-11477.jsonl`", "| steps | 31 | 24 |",
                   "Alignment (informational): F1 0", "Milestones", "Divergence points ("):
        assert needle in text, needle


def test_compare_reverse_swaps_orientation(fixture_run, synthetic_session, tmp_artifacts):
    out = compare_sessions(fixture_run, synthetic_session, anchor_patch=None, fuzzy=True, reverse=True,
                           out_dir=tmp_artifacts)
    assert out["orientation"] == {"reference": fixture_run.source, "compared": synthetic_session}
    assert out["summary"]["outcome"]["reference_filename"] == "django__django-11477.jsonl"
    assert Path(out["path"]).name == f"{Path(synthetic_session).stem}.json"


def test_compare_missing_anchor_patch(fixture_run, synthetic_session, tmp_artifacts, tmp_path):
    with pytest.raises(BridgeError) as caught:
        compare_sessions(fixture_run, synthetic_session, anchor_patch=str(tmp_path / "nope.diff"), fuzzy=False,
                         reverse=False, out_dir=tmp_artifacts)
    assert caught.value.code == "not_found" and caught.value.path == str(tmp_path / "nope.diff")


def test_compare_anchor_patch_grounds_the_comparison(fixture_run, synthetic_session, tmp_artifacts, tmp_path):
    patch = tmp_path / "fix.diff"
    patch.write_text("--- a/django/urls/base.py\n+++ b/django/urls/base.py\n@@ -1 +1 @@\n-x\n+y\n")
    out = compare_sessions(fixture_run, synthetic_session, anchor_patch=str(patch), fuzzy=False, reverse=False,
                           out_dir=tmp_artifacts)
    assert out["summary"]["anchor_mode"] != "self"


def test_scorecard_file_labels_and_text(fixture_run, synthetic_session, tmp_artifacts):
    out = scorecard(fixture_run, [synthetic_session], fuzzy=False, labels=["now", "before"], no_self=False,
                    out_dir=tmp_artifacts)
    assert out["path"] == str(tmp_artifacts / "compare" / "scorecard.json")
    saved = json.loads(Path(out["path"]).read_text())
    assert set(saved["views"]) == {"run_scorecard", "run_behavior", "run_timelines"}
    assert out["rows"] == saved["views"]["run_scorecard"]["data"]
    assert [r["label"] for r in out["rows"]] == ["now", "before"]
    assert "| now |" in out["text"] and "| before |" in out["text"] and "baseline now" in out["text"]


def test_scorecard_no_self(synthetic_session, tmp_path, tmp_artifacts):
    (tmp_path / "third").mkdir()
    third = make_session(tmp_path / "third", steps=12)
    out = scorecard(None, [synthetic_session, third], fuzzy=False, labels=None, no_self=True, out_dir=tmp_artifacts)
    assert [r["path"] for r in out["rows"]] == [synthetic_session, third]


def test_scorecard_unreadable_other_is_an_error_row(fixture_run, synthetic_session, tmp_path, tmp_artifacts):
    out = scorecard(fixture_run, [synthetic_session, str(tmp_path / "missing.jsonl")], fuzzy=False, labels=None,
                    no_self=False, out_dir=tmp_artifacts)
    assert out["rows"][2]["error"] and "| error |" in out["text"] and "| missing |" in out["text"]


def test_scorecard_needs_two_runs(synthetic_session, tmp_artifacts):
    with pytest.raises(BridgeError) as caught:
        scorecard(None, [synthetic_session], fuzzy=False, labels=None, no_self=True, out_dir=tmp_artifacts)
    assert caught.value.code == "compare_failed"


def test_label_views_payload_phases_and_text(sidecar_path):
    out = label_views_payload(sidecar_path, None)
    assert out["summary"]["classified"] == 23 and out["phases"]["counts"] == {"understand": 23}
    assert set(out["phases"]["durations_s"]) == {"understand"} and "comparison" not in out
    assert "23 classified" in out["text"] and "| understand | 23 |" in out["text"]


def test_label_views_payload_comparison(sidecar_path, other_sidecar):
    out = label_views_payload(sidecar_path, other_sidecar)
    comparison = out["comparison"]
    assert comparison["reference"]["counts"] == {"understand": 10, "implement": 10, "validate": 10}
    assert comparison["compared"]["counts"] == {"understand": 23}
    assert set(comparison["compared"]["durations_s"]) == {"understand"}
    assert "| implement | 10 | 0 |" in out["text"]


def test_cli_compare(run_cli, fixture_session, synthetic_session, tmp_artifacts):
    code, out = run_cli(["compare", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts),
                         "--other", synthetic_session, "--reverse", "--fuzzy-commands"])
    assert code == 0 and out["orientation"]["reference"] == str(fixture_session)
    assert out["path"].startswith(str(tmp_artifacts / "compare")) and out["text"]


def test_cli_compare_empty_current(run_cli, tmp_path, synthetic_session):
    empty = tmp_path / "empty.jsonl"
    empty.write_text('{"type":"ai-title","aiTitle":"x"}\n')
    code, out = run_cli(["compare", "--transcript", str(empty), "--kind", "current", "--other", synthetic_session,
                         "--artifacts", str(tmp_path / "a")])
    assert code == 0 and out["empty"] is True


def test_cli_compare_unreadable_other(run_cli, fixture_session, tmp_path, tmp_artifacts):
    code, out = run_cli(["compare", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts),
                         "--other", str(tmp_path / "missing.jsonl")])
    assert code == 1 and out["error"]["code"] == "load_error"


def test_cli_scorecard(run_cli, fixture_session, synthetic_session, tmp_artifacts):
    code, out = run_cli(["scorecard", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts),
                         "--others", synthetic_session, "--labels", "now,before"])
    assert code == 0 and [r["label"] for r in out["rows"]] == ["now", "before"]


def test_cli_scorecard_no_self_skips_the_current_session(run_cli, tmp_path, synthetic_session, tmp_artifacts):
    (tmp_path / "third").mkdir()
    third = make_session(tmp_path / "third", steps=12)
    code, out = run_cli(["scorecard", "--transcript", str(tmp_path / "not-there.jsonl"), "--artifacts",
                         str(tmp_artifacts), "--others", synthetic_session, third, "--no-self"])
    assert code == 0 and [r["path"] for r in out["rows"]] == [synthetic_session, third]


def test_cli_labels(run_cli, sidecar_path, other_sidecar):
    code, out = run_cli(["labels", "--sidecar", sidecar_path, "--other", other_sidecar])
    assert code == 0 and out["summary"]["classified"] == 23 and "comparison" in out


def test_cli_labels_not_a_sidecar(run_cli, fixture_session):
    code, out = run_cli(["labels", "--sidecar", str(fixture_session)])
    assert code == 1 and out["error"]["code"] == "load_error" and out["error"]["trajviz_code"] == "invalid_labels"
