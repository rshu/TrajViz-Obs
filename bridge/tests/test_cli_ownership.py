"""A detached native bridge cannot survive its host and leave descendants."""

import contextlib
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

LAUNCHER = Path(__file__).resolve().parents[1] / "trajviz_obs_cli.py"


@pytest.mark.parametrize("args", [["--host-parent"], ["--host-parent", "0"],
                                  ["--host-parent", "-1"], ["--host-parent", "not-a-pid"]])
def test_bad_host_parent_is_one_newline_json_usage_error(args):
    proc = subprocess.run([sys.executable, "-I", str(LAUNCHER), *args],
                          capture_output=True, text=True, timeout=5, check=False)
    assert proc.returncode == 2 and not proc.stderr
    assert proc.stdout.endswith("\n") and len(proc.stdout.splitlines()) == 1
    assert json.loads(proc.stdout)["error"]["code"] == "usage"


@pytest.mark.skipif(os.name != "posix", reason="Native POSIX process group ownership")
@pytest.mark.parametrize("detached", [False, True])
def test_host_parent_requires_own_group_and_the_actual_parent(detached):
    parent = os.getpid() + 100000 if detached else os.getpid()
    proc = subprocess.run([sys.executable, "-I", str(LAUNCHER), "--host-parent", str(parent), "doctor"],
                          capture_output=True, text=True, timeout=5, start_new_session=detached, check=False)
    assert proc.returncode == 2 and not proc.stderr
    assert json.loads(proc.stdout)["error"]["code"] == "usage"
    assert ("current parent" if detached else "dedicated process group") in proc.stdout


@pytest.mark.skipif(os.name != "posix", reason="Native POSIX process group ownership")
def test_owned_launcher_strips_private_flag_before_the_real_cli():
    proc = subprocess.run([sys.executable, "-I", str(LAUNCHER), "--host-parent", str(os.getpid()), "doctor"],
                          capture_output=True, text=True, timeout=30, start_new_session=True, check=False)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout)["trajviz"]["version"].startswith("0.2")


@pytest.mark.skipif(os.name != "posix", reason="Native POSIX process group ownership")
@pytest.mark.parametrize("early", [False, True])
def test_hard_killed_host_cannot_leave_a_bridge_or_descendant(tmp_path, early):
    # Stage the real stdlib launcher with a deterministic sleeping handler.
    # Inherited pipes prove every descendant exits, not merely the direct child.
    launcher = tmp_path / "trajviz_obs_cli.py"
    launcher.write_bytes(LAUNCHER.read_bytes())
    package = tmp_path / "trajviz_obs"
    package.mkdir()
    (package / "__init__.py").write_text("")
    heartbeat, started, ready = (tmp_path / name for name in ("heartbeat", "started", "ready"))
    child = (f"from pathlib import Path; import time; p=Path({str(heartbeat)!r});\n"
             "while True: p.write_text(str(time.time())); time.sleep(0.02)")
    (package / "cli.py").write_text(
        "import subprocess,sys,time\nfrom pathlib import Path\ndef main(argv=None):\n"
        f" subprocess.Popen([sys.executable,'-I','-c',{child!r}])\n"
        f" Path({str(started)!r}).write_text('started')\n time.sleep(60)\n return 0\n")
    wrapper = tmp_path / "wrapper.py"
    wrapper.write_text(
        "import os,runpy,sys,time\nfrom pathlib import Path\n"
        f"early={early!r}\nparent=int(sys.argv[2])\n"
        f"if early:\n Path({str(ready)!r}).write_text('ready')\n"
        " while os.getppid()==parent: time.sleep(0.01)\n"
        f"sys.argv=[{str(launcher)!r},*sys.argv[1:]]\nrunpy.run_path(sys.argv[0],run_name='__main__')\n")
    host = tmp_path / "host.py"
    host.write_text(
        "import os,subprocess,sys,time\n"
        f"p=subprocess.Popen([sys.executable,'-I',{str(wrapper)!r},'--host-parent',str(os.getpid()),'wait'],"
        "start_new_session=True)\nprint(p.pid,flush=True)\ntime.sleep(60)\n")
    proc = subprocess.Popen([sys.executable, "-I", str(host)], stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, text=True)
    group = None
    try:
        group = int(proc.stdout.readline())
        until = time.monotonic() + 5
        marker = ready if early else heartbeat
        while not marker.exists() and time.monotonic() < until:
            time.sleep(0.01)
        assert marker.exists(), "The owned worker never reached the test checkpoint"
        proc.kill()
        proc.wait(timeout=5)
        out, err = proc.communicate(timeout=5)
        assert not err
        if early:
            assert not started.exists() and not heartbeat.exists()
            assert json.loads(out)["error"]["code"] == "usage"
        else:
            stopped = heartbeat.read_text()
            time.sleep(0.15)
            assert heartbeat.read_text() == stopped
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if group is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(group, signal.SIGKILL)
        proc.communicate(timeout=5)
