import json
import os
import subprocess
import sys
from pathlib import Path

import agent_trajviz
import pytest

BRIDGE = Path(__file__).resolve().parents[1]


def test_doctor_envelope(run_cli):
    code, out = run_cli(["doctor"])
    assert code == 0
    assert out["trajviz"]["version"].startswith("0.2")
    assert set(out["extras"]) == {"charts", "llm", "app"}
    assert "available" in out["decaf"] and "ready" in out["llm_env"]


def test_doctor_details(run_cli, monkeypatch, tmp_path, no_llm_env):
    monkeypatch.setenv("TRAJVIZ_REFERENCE_ROOT", "/refs")
    code, out = run_cli(["doctor", "--cwd", str(tmp_path)])
    assert code == 0
    assert out["python"] == ".".join(map(str, sys.version_info[:3]))
    assert out["trajviz"]["path"] == str(Path(agent_trajviz.__file__).parent)
    assert all(isinstance(v, bool) for v in out["extras"].values())
    assert isinstance(out["decaf"]["available"], bool)
    assert out["decaf"]["reason"] is None or isinstance(out["decaf"]["reason"], str)
    assert out["llm_env"]["ready"] is False
    assert any("ANALYZE_MODEL" in name for name in out["llm_env"]["missing"])
    assert out["reference_root"] == "/refs"


def test_doctor_reads_dotenv_of_cwd(run_cli, tmp_path, no_llm_env):
    (tmp_path / ".env").write_text("ANALYZE_MODEL=m1\n")
    code, out = run_cli(["doctor", "--cwd", str(tmp_path)])
    assert code == 0
    assert not any("ANALYZE_MODEL" in name for name in out["llm_env"]["missing"])
    assert Path.cwd() != tmp_path


def test_dotenv_values_do_not_leak_between_tests(pytester):
    """TrajViz's fill-only load_dotenv writes os.environ directly; conftest must undo it after each test."""
    pytester.makeconftest((Path(__file__).parent / "conftest.py").read_text())
    pytester.makepyfile(
        """
        import os

        from trajviz_obs.doctor import doctor_info


        def test_writes(tmp_path, no_llm_env):
            (tmp_path / ".env").write_text("ANALYZE_MODEL=leak-probe\\n")
            doctor_info(str(tmp_path))
            assert os.environ["ANALYZE_MODEL"] == "leak-probe"


        def test_sees_nothing():
            assert os.environ.get("ANALYZE_MODEL") != "leak-probe"
        """
    )
    pytester.runpytest("-p", "no:cacheprovider").assert_outcomes(passed=2)
    assert os.environ.get("ANALYZE_MODEL") != "leak-probe"


def test_doctor_missing_cwd_is_not_found(run_cli, tmp_path):
    missing = tmp_path / "gone"
    code, out = run_cli(["doctor", "--cwd", str(missing)])
    assert code == 1 and out["error"]["code"] == "not_found" and out["error"]["path"] == str(missing)


def test_doctor_works_without_chart_or_dashboard_extras(tmp_path):
    """Run a fresh interpreter: imports cached by this all-extras test environment must not hide the bug."""
    script = f"""
import importlib.abc
import sys
sys.path.insert(0, {str(BRIDGE)!r})
class MinimalExtras(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {{'pygments', 'plotly', 'gradio'}}:
            raise ModuleNotFoundError('Optional package disabled: ' + fullname, name=fullname)
sys.meta_path.insert(0, MinimalExtras())
from trajviz_obs.cli import main
sys.exit(main(['doctor', '--cwd', {str(tmp_path)!r}]))
"""
    proc = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=30,
                          check=False)
    assert proc.returncode == 0, proc.stderr
    out = json.loads(proc.stdout)
    assert out["extras"] == {"charts": False, "llm": True, "app": False}
    assert out["llm_env"]["available"] is True


def test_extras_import_names_match_trajviz():
    from agent_trajviz import _extras

    from trajviz_obs import doctor

    assert doctor.EXTRAS == _extras.EXTRAS


def test_unknown_subcommand_is_usage_error(run_cli):
    code, out = run_cli(["nonsense"])
    assert code == 2
    assert out["error"]["code"] == "usage"


def test_bridge_error_envelope(run_cli, monkeypatch):
    from trajviz_obs import cli, errors

    @cli.register("boom")
    def _boom(args):
        raise errors.BridgeError("not_found", "no such thing", path="/x")

    code, out = run_cli(["boom"])
    assert code == 1 and out["error"] == {"code": "not_found", "message": "no such thing", "path": "/x"}


def test_bridge_error_extra_fields(run_cli):
    from trajviz_obs import cli, errors

    @cli.register("boom")
    def _boom(args):
        raise errors.BridgeError("ambiguous", "two match", candidates=[{"id": "a"}])

    code, out = run_cli(["boom"])
    assert code == 1
    assert out["error"] == {"code": "ambiguous", "message": "two match", "candidates": [{"id": "a"}]}


def test_missing_extra_maps_to_missing_extra(run_cli):
    from trajviz_obs import cli

    @cli.register("boom")
    def _boom(args):
        raise agent_trajviz.MissingExtraError("charts", ["plotly"], "The report")

    code, out = run_cli(["boom"])
    assert code == 1
    assert out["error"]["code"] == "missing_extra" and out["error"]["extra"] == "charts"
    assert "pip install" in out["error"]["message"]


def test_load_error_maps_to_load_error(run_cli, tmp_path):
    from trajviz_obs import cli

    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n")

    @cli.register("boom")
    def _boom(args):
        agent_trajviz.load(str(bad))

    code, out = run_cli(["boom"])
    assert code == 1
    err = out["error"]
    assert err["code"] == "load_error" and err["trajviz_code"] and err["path"] == str(bad)


def test_unexpected_exception_is_internal(run_cli, capsys):
    from trajviz_obs import cli

    @cli.register("boom")
    def _boom(args):
        raise ValueError("kaput")

    code, out = run_cli(["boom"])
    assert code == 3
    assert out["error"]["code"] == "internal" and "kaput" in out["error"]["message"]
    assert "Traceback" in capsys.readouterr().err


def test_help_is_json(run_cli):
    code, out = run_cli(["--help"])
    assert code == 0 and "doctor" in out["help"]


def test_launcher_runs_isolated(tmp_path):
    proc = subprocess.run([sys.executable, "-I", str(BRIDGE / "trajviz_obs_cli.py"), "doctor"], cwd=tmp_path,
                          capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["trajviz"]["version"].startswith("0.2")


def _run_without_requests(tmp_path, *args):
    """Run the launcher in a fresh ``-I`` interpreter where ``requests`` (the llm extra) cannot be imported."""
    shim = tmp_path / "no_requests.py"
    shim.write_text(
        "import runpy, sys\n"
        "sys.modules['requests'] = None\n"
        f"sys.argv = [{str(BRIDGE / 'trajviz_obs_cli.py')!r}, *sys.argv[1:]]\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    return subprocess.run([sys.executable, "-I", str(shim), *args], cwd=tmp_path, capture_output=True, text=True,
                          timeout=120, check=False)


def test_doctor_reports_missing_llm_extra(tmp_path):
    proc = _run_without_requests(tmp_path, "doctor")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = json.loads(proc.stdout)
    assert out["extras"]["llm"] is False and out["extras"]["app"] is False
    assert out["llm_env"]["available"] is False and out["llm_env"]["ready"] is False
    assert "llm" in out["llm_env"]["reason"]


def test_help_works_without_llm_extra(tmp_path):
    proc = _run_without_requests(tmp_path, "--help")
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "doctor" in json.loads(proc.stdout)["help"]


def test_fixture_session_is_vendored(fixture_session, tmp_artifacts):
    assert fixture_session.is_file() and fixture_session.stat().st_size > 0
    assert tmp_artifacts.is_dir() and not any(tmp_artifacts.iterdir())


def test_doctor_lists_plugins(run_cli, fake_plugins):
    code, out = run_cli(["doctor"])
    assert code == 0 and out["plugins"] == {"loaded": [], "error": None}


def test_doctor_names_loaded_plugins(run_cli, fake_plugins):
    fake_plugins("obs-fake", lambda: None)
    code, out = run_cli(["doctor"])
    assert code == 0 and out["plugins"] == {"loaded": ["obs-fake"], "error": None}


def test_doctor_keeps_a_printing_plugin_off_stdout(fake_plugins, capsys):
    """A plugin hook's print goes to stderr: stdout holds exactly one JSON document."""
    from trajviz_obs import cli

    fake_plugins("chatty", lambda: print("chatty plugin registered"))
    code = cli.main(["doctor"])
    out, err = capsys.readouterr()
    assert code == 0 and json.loads(out)["plugins"] == {"loaded": ["chatty"], "error": None}
    assert "chatty plugin registered" in err


def test_doctor_survives_a_failing_plugin(run_cli, fake_plugins):
    def broken() -> None:
        raise RuntimeError("boom")

    fake_plugins("broken", broken)
    code, out = run_cli(["doctor"])
    assert code == 0 and out["plugins"]["loaded"] == []
    assert "'broken'" in out["plugins"]["error"] and "boom" in out["plugins"]["error"]


@pytest.mark.parametrize("trajviz", ["missing", "broken"])
def test_launcher_answers_in_json_without_a_working_trajviz(tmp_path, trajviz):
    if trajviz == "missing":
        setup = "sys.modules['agent_trajviz'] = None\n"
    else:  # a half-installed package that raises on import, found before the venv's own
        package = tmp_path / "fake" / "agent_trajviz"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("raise RuntimeError('half installed')\n")
        setup = f"sys.path.insert(0, {str(tmp_path / 'fake')!r})\n"
    shim = tmp_path / "shim.py"
    shim.write_text(f"import runpy, sys\n{setup}sys.argv = [{str(BRIDGE / 'trajviz_obs_cli.py')!r}, *sys.argv[1:]]\n"
                    "runpy.run_path(sys.argv[0], run_name='__main__')\n")
    proc = subprocess.run([sys.executable, "-I", str(shim), "doctor"], cwd=tmp_path, capture_output=True, text=True,
                          timeout=120, check=False)
    assert proc.returncode == 3, proc.stdout + proc.stderr
    (line,) = proc.stdout.splitlines()
    error = json.loads(line)["error"]
    assert error["code"] == "internal"
    assert ("agent_trajviz" if trajviz == "missing" else "RuntimeError: half installed") in error["message"]
    assert "Traceback" in proc.stderr
