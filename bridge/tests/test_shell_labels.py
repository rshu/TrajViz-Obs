"""Shell failure displays retain uncertainty about which compound command failed."""

import copy
import json

import pytest

from trajviz_obs.compat import load_plugins, load_run


@pytest.mark.parametrize("command,expected", [
    ("python3 -m unittest -v", "unittest"),
    ("cd /work && ls -la && python3 -m unittest -v", "Bash"),
    ("printf '%s' 'a;b && c|d'", "printf"),
    ("bash -lc 'printf \"%s\" \"a;b\"'", "printf"),
    ("bash -lc 'ls && python3 -m unittest -v'", "Bash"),
    ("bash --norc -c 'ls && python3 -m unittest -v'", "Bash"),
    ("bash --rcfile /tmp/config -lc 'ls && false'", "Bash"),
    ("env NOTE='a;b' timeout 5 sh -c 'ls; false'", "Bash"),
    ("echo ready | grep missing", "Bash"),
    ("ls\nfalse", "Bash"),
    ('cmd /c "dir && python -m unittest"', "Bash"),
    ('powershell -Command "Get-ChildItem; throw \'failed\'"', "Bash"),
    ("ls 'unfinished", "Bash"),
])
def test_failed_shell_labels_do_not_guess_the_failing_inner_command(command, expected):
    from agent_trajviz.insight import chart_data, diagnostics, shell_cmd

    load_plugins()
    call = {"tool_name": "Bash", "input": {"command": command}, "status": "error",
            "tool_error": True, "error": "Exit code 1", "metadata": {"exit": 1}}
    before = copy.deepcopy(call)
    assert all(module.tool_chart_name(call) == expected for module in (shell_cmd, diagnostics, chart_data))
    assert call == before


def test_successful_shell_and_non_shell_calls_keep_the_original_labels():
    from agent_trajviz.insight import shell_cmd

    load_plugins()
    successful = {"tool_name": "Bash", "input": {"command": "cd /work && ls -la && python3 -m unittest"},
                  "status": "completed", "metadata": {"exit": 0}}
    assert shell_cmd.tool_chart_name(successful) == "ls"
    assert shell_cmd.tool_chart_name({"tool_name": "Read", "status": "error", "input": {"command": "ls && false"}}) == "Read"
    installed = shell_cmd.tool_chart_name
    load_plugins()
    assert shell_cmd.tool_chart_name is installed


def test_shell_long_options_and_option_separator_do_not_turn_literal_arguments_into_commands():
    from trajviz_obs.shell_labels import _compound

    assert not _compound("bash --norc -c 'printf \"%s\" \"a;b && c\"'")
    assert not _compound("bash --rcfile /tmp/config -c 'printf \"%s\" \"a;b\"'")
    assert not _compound("bash -- -c 'ls && false'")


def test_native_shell_failure_is_consistent_in_exports_snapshot_and_browser_without_source_changes(tmp_path):
    from agent_trajviz.insight.presenters.issues import build_overview_issues_html
    from agent_trajviz.insight.session import session_from_run

    from trajviz_obs.sidecars import scan_transcript
    from trajviz_obs.snapshot import build_snapshot

    command = "cd /work && ls -la && python3 -m unittest -v"
    path = tmp_path / "native.json"
    path.write_text(json.dumps({"source_format": "opencode_v2", "schema_version": 1,
        "session": {"id": "root", "time": {"created": 1790000000000, "updated": 1790000001000}},
        "messages": [{"type": "assistant", "id": "a", "agent": "build",
            "time": {"created": 1790000000000, "completed": 1790000001000},
            "content": [{"type": "tool", "name": "shell", "id": "failed", "state": {
                "status": "completed", "input": {"command": command}, "metadata": {"exit": 1},
                "content": [{"type": "text", "text": "Listing succeeded. unittest failed."}]},
                "time": {"created": 1790000000001, "ran": 1790000000002, "completed": 1790000000999}}]}],
        "children": []}))
    raw = path.read_bytes()
    run = load_run(str(path))
    canonical = copy.deepcopy(run.to_dict(include_raw=True))
    views = run.views(only=["issues", "error_clusters", "failure_patterns", "tool_usage", "tool_outcomes"])
    assert views["issues"].data[0]["title"].startswith("Bash: Exit code 1")
    assert views["error_clusters"].data["clusters"][0]["tool"] == "Bash"
    assert views["failure_patterns"].data[0]["cluster_label"] == "Bash: Exit code 1"
    tools = views["tool_usage"].data["tools"]
    assert len(tools) == 1 and tools[0]["tool"] == "Bash" and tools[0]["calls"] == 1
    assert views["tool_outcomes"].data["calls"][0]["tool"] == "Bash"
    assert views["tool_outcomes"].data["calls"][0]["ok"] is False
    snapshot = build_snapshot(run, transcript_path=str(path), facts=scan_transcript(str(path)), agents_meta={}, artifacts=None)
    assert snapshot["issues"][0]["title"].startswith("Bash: Exit code 1")
    assert snapshot["issues"][0]["evidence"].startswith("Bash: cd /work && ls -la")
    html = build_overview_issues_html(session_from_run(run))
    assert "Bash: Exit code 1" in html and "ls: Exit code 1" not in html
    assert run.to_dict(include_raw=True) == canonical and path.read_bytes() == raw
