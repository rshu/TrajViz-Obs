"""``view``, ``json`` and ``schemas``: the view catalog, option coercion, inline drawing and file exports."""

from __future__ import annotations

import json
from pathlib import Path

import agent_trajviz as tv
import pytest
from conftest import make_session

from trajviz_obs import views_cmd
from trajviz_obs.compat import load_run, project_view
from trajviz_obs.errors import BridgeError
from trajviz_obs.views_cmd import catalog, coerce_options, export, view_payload, write_schemas


@pytest.fixture
def synthetic_run(tmp_path):
    """A 40-turn native session with one sub-agent (81 records, so more rows than one page)."""
    return load_run(make_session(tmp_path, steps=40, agents=1))


def test_catalog_has_36_views(): assert len(catalog()) == 36 and "data_quality" in {v["id"] for v in catalog()}


def test_coerce_snapshot_step_and_agent(fixture_run):
    assert coerce_options("context_usage", {"snapshot_step": "12", "agent": "main"}, fixture_run) == {"snapshot_step": 12, "agent": "__main__"}


def test_unknown_option_is_bridge_error(fixture_run):
    with pytest.raises(BridgeError) as e: coerce_options("tool_usage", {"nope": "1"}, fixture_run)
    assert e.value.code == "bad_option"


# Deviation from the brief: its test ran on fixture_run, whose `steps` view has 24 rows (< 25); a 40-turn
# synthetic session keeps the brief's values (limit 25, "rows 1–25", more than 25 rows).
def test_view_payload_pages_and_writes_file(synthetic_run, tmp_artifacts):
    p = view_payload(synthetic_run, "steps", {}, limit=25, offset=0, out_dir=tmp_artifacts / "views", inline_json=False)
    assert (tmp_artifacts / "views" / "steps.json").exists() and p["rows_total"] > 25 and "rows 1–25" in p["inline"]["table"]


def test_export_without_trajectory_by_default(fixture_run, tmp_artifacts):
    export(fixture_run, tmp_artifacts / "views", only=None, with_trajectory=False)
    assert not (tmp_artifacts / "views" / "trajectory.json").exists() and (tmp_artifacts / "views" / "index.json").exists()


def test_catalog_entries():
    entries = {e["id"]: e for e in catalog()}
    assert list(entries) == list(tv.VIEW_CATALOG)
    assert set(entries["context_pressure"]) == {"id", "kind", "title", "description", "options"}
    assert set(entries["context_pressure"]["options"]) == {"agent", "window_limit", "snapshot_step"}
    assert entries["steps"]["kind"] == "table" and entries["steps"]["options"] == {}


def test_coerce_rejects_bad_values_with_option_help(synthetic_run):
    help_ = dict(tv.VIEW_CATALOG["context_pressure"]["options"])
    with pytest.raises(BridgeError) as e:
        coerce_options("context_pressure", {"snapshot_step": "twelve"}, synthetic_run)
    assert e.value.code == "bad_option" and e.value.extra["options"] == help_ and "snapshot_step" in e.value.message
    with pytest.raises(BridgeError) as e:
        coerce_options("context_pressure", {"window_limit": "big"}, synthetic_run)
    assert e.value.code == "bad_option" and e.value.extra["options"] == help_
    with pytest.raises(BridgeError) as e:
        coerce_options("context_pressure", {"agent": "nobody"}, synthetic_run)
    assert e.value.code == "bad_option" and e.value.extra["agents"] == ["__all__", "__main__", "a0000000000000000"]
    with pytest.raises(BridgeError) as e:
        coerce_options("tool_usage", {"nope": "1"}, synthetic_run)
    assert e.value.extra["options"] == {} and "takes no options" in e.value.message


def test_coerce_passes_valid_values(synthetic_run):
    assert coerce_options("context_pressure", {"agent": "__main__", "window_limit": "200000"}, synthetic_run) == {
        "agent": "__main__", "window_limit": 200000}
    assert coerce_options("context_usage", {"agent": "a0000000000000000", "window_limit": "1.5e5"},
                          synthetic_run) == {"agent": "a0000000000000000", "window_limit": 150000.0}
    assert coerce_options("steps", {}, synthetic_run) == {}


def test_unknown_view_is_bridge_error(fixture_run):
    with pytest.raises(BridgeError) as e:
        coerce_options("stepz", {}, fixture_run)
    assert e.value.code == "unknown_view" and "steps" in e.value.extra["views"]


@pytest.mark.parametrize("view_id", list(tv.VIEW_CATALOG))
def test_every_view_draws_inline(synthetic_run, tmp_artifacts, view_id):
    p = view_payload(synthetic_run, view_id, {}, limit=5, offset=0, out_dir=tmp_artifacts, inline_json=False)
    written = json.loads((tmp_artifacts / f"{view_id}.json").read_text())
    assert written == project_view(synthetic_run, synthetic_run.view(view_id)).to_dict()
    assert p["path"] == str(tmp_artifacts / f"{view_id}.json") and p["bytes"] == (tmp_artifacts / f"{view_id}.json").stat().st_size
    assert p["view"] == {k: written[k] for k in ("id", "title", "kind", "description")}
    (key, text), = p["inline"].items()
    assert key in ("table", "series") and isinstance(text, str) and text
    assert isinstance(p["rows_total"], int)


def test_chart_views_draw_series_and_bars(synthetic_run, tmp_artifacts):
    tokens = view_payload(synthetic_run, "token_usage", {}, limit=25, offset=0, out_dir=tmp_artifacts, inline_json=False)
    lines = tokens["inline"]["series"].splitlines()
    assert lines[0].startswith("fresh_input ") and any(line.startswith("total ") for line in lines)
    assert tokens["rows_total"] == len(synthetic_run.view("token_usage").data["steps"])
    assert lines[-1] == f"{tokens['rows_total']} points · steps 0–{tokens['rows_total'] - 1}"
    pressure = view_payload(synthetic_run, "context_pressure", {}, limit=25, offset=0, out_dir=tmp_artifacts,
                            inline_json=False)
    assert [line.split()[0] for line in pressure["inline"]["series"].splitlines()[:2]] == ["main", "Worker"]
    tools = view_payload(synthetic_run, "tool_usage", {}, limit=2, offset=1, out_dir=tmp_artifacts, inline_json=False)
    bars = tools["inline"]["series"].splitlines()
    data = synthetic_run.view("tool_usage").data["tools"]
    assert len(bars) == 4 and bars[0].startswith(data[1]["tool"]) and bars[0].endswith(f" {data[1]['calls']}")
    assert "█" in bars[0] and bars[-1] == f"rows 2–3 of {len(data)}"


def test_view_payload_inline_json_and_size_cap(fixture_run, tmp_artifacts, monkeypatch):
    p = view_payload(fixture_run, "health", {}, limit=25, offset=0, out_dir=tmp_artifacts, inline_json=True)
    assert p["inline"] == {"data": project_view(fixture_run, fixture_run.view("health")).data}
    monkeypatch.setattr(views_cmd, "INLINE_JSON_BYTES", 100)
    p = view_payload(fixture_run, "health", {}, limit=25, offset=0, out_dir=tmp_artifacts, inline_json=True)
    assert set(p["inline"]) == {"table"} and p["bytes"] > 100


def test_view_payload_applies_options(synthetic_run, tmp_artifacts):
    opts = coerce_options("context_pressure", {"agent": "main", "window_limit": "1000"}, synthetic_run)
    p = view_payload(synthetic_run, "context_pressure", opts, limit=25, offset=0, out_dir=tmp_artifacts, inline_json=True)
    assert p["inline"]["data"]["agent"] == "__main__" and p["inline"]["data"]["stats"]["window_limit"] == 1000


def test_export_named_views_with_trajectory(fixture_run, tmp_artifacts):
    out = export(fixture_run, tmp_artifacts / "v", only=["health", "issues"], with_trajectory=True)
    folder = tmp_artifacts / "v"
    assert out["path"] == str(folder)
    assert out["files"] == ["health.json", "issues.json", "trajectory.json", "index.json"]
    assert out["bytes"] == sum((folder / name).stat().st_size for name in out["files"])
    index = json.loads((folder / "index.json").read_text())
    assert [v["id"] for v in index["views"]] == ["health", "issues"] and index["trajectory_file"] == "trajectory.json"
    assert tv.load(folder / "trajectory.json").sha256 == fixture_run.sha256


def test_export_all_views_without_trajectory(fixture_run, tmp_artifacts):
    out = export(fixture_run, tmp_artifacts, only=None, with_trajectory=False)
    assert out["files"] == [f"{vid}.json" for vid in tv.VIEW_CATALOG] + ["index.json"]


def test_export_unknown_view_is_bridge_error(fixture_run, tmp_artifacts):
    with pytest.raises(BridgeError) as e:
        export(fixture_run, tmp_artifacts, only=["stepz"], with_trajectory=False)
    assert e.value.code == "unknown_view"


def test_write_schemas(tmp_artifacts):
    out = write_schemas(tmp_artifacts / "schemas")
    schemas = tv.view_schemas()
    assert out["path"] == str(tmp_artifacts / "schemas") and len(out["files"]) == len(schemas)
    assert out["files"][0] == "summary.schema.json"
    assert json.loads((tmp_artifacts / "schemas" / "steps.schema.json").read_text()) == schemas["steps"]
    assert out["bytes"] == sum(p.stat().st_size for p in (tmp_artifacts / "schemas").iterdir())


def test_cli_view_catalog(run_cli, fixture_session):
    code, out = run_cli(["view", "--transcript", str(fixture_session)])
    assert code == 0 and out == {"views": catalog()}


def test_cli_view_one(run_cli, fixture_session, tmp_artifacts):
    code, out = run_cli(["view", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts), "steps",
                         "--limit", "5", "--offset", "5"])
    assert code == 0 and out["path"] == str(tmp_artifacts / "views" / "steps.json")
    assert out["inline"]["table"].endswith("rows 6–10 of 24") and out["rows_total"] == 24


def test_cli_view_window_and_json(run_cli, fixture_session, tmp_artifacts):
    code, out = run_cli(["view", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts),
                         "--window", "1000000", "context_pressure", "--option", "agent=main", "--json"])
    assert code == 0 and out["inline"]["data"]["stats"]["window_limit"] == 1_000_000
    assert out["inline"]["data"]["agent"] == "__main__"


def test_cli_view_bad_option(run_cli, fixture_session, tmp_artifacts):
    code, out = run_cli(["view", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts),
                         "context_usage", "--option", "snapshot_step=x"])
    assert code == 1 and out["error"]["code"] == "bad_option" and "snapshot_step" in out["error"]["options"]
    code, out = run_cli(["view", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts),
                         "steps", "--option", "novalue"])
    assert code == 1 and out["error"]["code"] == "bad_option"


def test_cli_view_empty_current(run_cli, tmp_path):
    path = tmp_path / "s.jsonl"
    path.write_text('{"type":"ai-title","aiTitle":"x"}\n')
    code, out = run_cli(["view", "--transcript", str(path), "--kind", "current", "--artifacts", str(tmp_path / "a"),
                         "steps"])
    assert code == 0 and out["empty"] is True and out["reason"]


def test_cli_json(run_cli, fixture_session, tmp_artifacts):
    code, out = run_cli(["json", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts),
                         "--view", "health", "--view", "issues"])
    assert code == 0 and out["path"] == str(tmp_artifacts / "views")
    assert out["files"] == ["health.json", "issues.json", "index.json"]
    code, out = run_cli(["json", "--transcript", str(fixture_session), "--out", str(tmp_artifacts / "x"),
                         "--with-trajectory"])
    assert code == 0 and "trajectory.json" in out["files"] and Path(out["path"]) == tmp_artifacts / "x"


def test_cli_default_artifacts_under_cache(run_cli, fixture_session, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    code, out = run_cli(["view", "--transcript", str(fixture_session), "health"])
    assert code == 0
    assert out["path"] == str(tmp_path / "trajviz-obs" / "sessions" / "django__django-11477" / "views" / "health.json")


def test_cli_schemas(run_cli, tmp_path, monkeypatch):
    code, out = run_cli(["schemas", "--out", str(tmp_path / "s")])
    assert code == 0 and out["path"] == str(tmp_path / "s") and len(out["files"]) == len(tv.view_schemas())
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path))
    code, out = run_cli(["schemas"])
    assert code == 0 and out["path"] == str(tmp_path / "trajviz-obs" / "schemas")


def test_cli_view_negative_offset_is_usage_error(run_cli, fixture_session):
    code, out = run_cli(["view", "--transcript", str(fixture_session), "steps", "--offset", "-1"])
    assert code == 2 and out["error"]["code"] == "usage"


def test_sparkline_lines_page_by_limit(tmp_path, tmp_artifacts):
    run = load_run(make_session(tmp_path, steps=60, agents=5))
    p = view_payload(run, "context_pressure", {}, limit=2, offset=1, out_dir=tmp_artifacts, inline_json=False)
    lines = p["inline"]["series"].splitlines()
    assert [line.split()[:2] for line in lines[:2]] == [["Worker", "0"], ["Worker", "1"]]
    assert len(lines) == 3 and lines[-1].endswith(" · lines 2–3 of 6") and lines[-1].startswith(f"{p['rows_total']} points")


# The output-size rule: --limit is capped, and the inline part never passes INLINE_STDOUT_BYTES on stdout.


def test_cli_view_limit_is_capped_at_500(run_cli, fixture_session, tmp_artifacts):
    base = ["view", "--transcript", str(fixture_session), "--artifacts", str(tmp_artifacts), "steps"]
    code, out = run_cli([*base, "--limit", "501"])
    assert code == 2 and out["error"]["code"] == "usage" and "500" in out["error"]["message"]
    assert run_cli([*base, "--limit", "500"])[0] == 0


def test_inline_table_is_cut_to_the_byte_budget(synthetic_run, tmp_artifacts, monkeypatch):
    monkeypatch.setattr(views_cmd, "INLINE_STDOUT_BYTES", 4096)
    p = view_payload(synthetic_run, "steps", {}, limit=60, offset=5, out_dir=tmp_artifacts, inline_json=False)
    text, page = p["inline"]["table"], min(60, p["rows_total"] - 5)
    *table, footer, marker = text.splitlines()
    shown = len(table) - 3  # the header, its rule and the blank line before the footer
    assert len(json.dumps(text)) <= 4096 and 0 < shown < page
    assert footer == f"rows 6–{5 + shown} of {p['rows_total']}"
    assert marker.startswith(f"… {page - shown} more rows cut") and marker.endswith(p["path"])


def test_inline_series_is_cut_to_the_byte_budget(tmp_path, tmp_artifacts, monkeypatch):
    run = load_run(make_session(tmp_path, steps=60))
    monkeypatch.setattr(views_cmd, "INLINE_STDOUT_BYTES", 900)
    p = view_payload(run, "token_usage", {}, limit=25, offset=0, out_dir=tmp_artifacts, inline_json=False)
    *lines, footer, marker = p["inline"]["series"].splitlines()
    assert len(json.dumps(p["inline"]["series"])) <= 900 and 0 < len(lines) < 6
    assert footer.endswith(f" · lines 1–{len(lines)} of 6") and marker.startswith(f"… {6 - len(lines)} more line")


def test_inline_json_needs_room_on_stdout(fixture_run, tmp_artifacts, monkeypatch):
    monkeypatch.setattr(views_cmd, "INLINE_STDOUT_BYTES", 200)
    p = view_payload(fixture_run, "health", {}, limit=25, offset=0, out_dir=tmp_artifacts, inline_json=True)
    assert set(p["inline"]) == {"table"} and p["bytes"] <= views_cmd.INLINE_JSON_BYTES


def test_cli_view_of_wide_rows_stays_under_256_kb(tmp_path, tmp_artifacts, capsys):
    from trajviz_obs import cli

    path = Path(make_session(tmp_path, steps=600))
    # A model id of non-ASCII text: each character is 6 bytes on stdout (\uXXXX), so 500 rows pass 256 KB.
    path.write_text(path.read_text().replace('"model": "claude-test"', '"model": ' + json.dumps("模型" * 60)))
    code = cli.main(["view", "--transcript", str(path), "--artifacts", str(tmp_artifacts), "steps", "--limit", "500"])
    text = capsys.readouterr().out
    assert code == 0 and len(text.encode()) <= 256 * 1024
    out = json.loads(text)
    assert out["rows_total"] > 500 and "more rows cut" in out["inline"]["table"].splitlines()[-1]
