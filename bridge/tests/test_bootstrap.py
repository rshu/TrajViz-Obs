"""Stdlib regression tests for environment publication and cross-process locking."""
import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("trajviz_obs_bootstrap", Path(__file__).resolve().parents[1] / "bootstrap.py")
b = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = b
spec.loader.exec_module(b)


def request(cache, source="agent-trajviz>=0.2.0,<0.3", extras=()):
    return {"cache": str(cache), "source": source, "installSource": source, "local": False, "pythonOption": "", "uv": None, "extras": list(extras)}


def fake_builder(generation, req, extras):
    with (Path(req["cache"]) / "builds").open("a") as out:
        out.write(json.dumps(extras) + "\n")
    time.sleep(0.05)
    python = generation / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python.parent.mkdir()
    python.write_text("ready", encoding="utf-8")
    return "0.2.0"


def concurrent_setup(req, queue):
    queue.put(b.setup(req, builder=fake_builder))


def hold_lock(path, queue):
    with b.install_lock(Path(path)):
        queue.put("locked")
        time.sleep(60)


class BootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.cache = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_concurrent_first_runs_build_once_and_atomic_metadata(self):
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        workers = [ctx.Process(target=concurrent_setup, args=(request(self.cache), queue)) for _ in range(3)]
        for p in workers:
            p.start()
        states = [queue.get(timeout=10) for _ in workers]
        for p in workers:
            p.join(10)
            self.assertEqual(p.exitcode, 0)
        self.assertEqual(len({s["python"] for s in states}), 1)
        self.assertEqual(len((self.cache / "builds").read_text().splitlines()), 1)
        self.assertEqual(json.loads((self.cache / "state.json").read_text()), states[0])

    def test_different_sources_and_extras_do_not_change_published_interpreters(self):
        first = b.setup(request(self.cache), builder=fake_builder)
        second = b.setup(request(self.cache, extras=["app"]), builder=fake_builder)
        other = b.setup(request(self.cache, source="agent-trajviz==0.2.1"), builder=fake_builder)
        self.assertNotEqual(first["python"], second["python"])
        self.assertNotEqual(second["python"], other["python"])
        self.assertEqual(Path(first["python"]).read_text(), "ready")
        again = b.setup(request(self.cache), builder=fake_builder)
        self.assertEqual(again["python"], second["python"])
        self.assertEqual(again["extras"], ["llm", "charts", "zstd", "app"])
        self.assertEqual(len((self.cache / "builds").read_text().splitlines()), 3)

    def test_failed_install_preserves_prior_environment_and_metadata(self):
        first = b.setup(request(self.cache), builder=fake_builder)
        def failed(generation, req, extras):
            raise RuntimeError("network unavailable")
        with self.assertRaisesRegex(RuntimeError, "network unavailable"):
            b.setup(request(self.cache, extras=["app"]), builder=failed)
        self.assertEqual(json.loads((self.cache / "state.json").read_text()), first)
        self.assertTrue(Path(first["python"]).is_file())
        self.assertEqual(len(list((self.cache / "envs").glob("*/*/Scripts" if os.name == "nt" else "*/*/bin"))), 1)

    def test_invalid_metadata_rebuilds_without_mutating_old_generation(self):
        first = b.setup(request(self.cache), builder=fake_builder)
        metadata = Path(first["python"]).parent.parent.parent / "state.json"
        external = self.cache / "external-python"
        external.write_text("unrelated")
        invalid = [{"python": str(external)}, {"extras": ["llm"]}, {"trajviz": None},
                   {"createdAt": float("inf")}, {"sourceCwd": "/wrong/working-directory"}]
        for change in invalid:
            metadata.write_text(json.dumps({**first, **change}))
            rebuilt = b.setup(request(self.cache), builder=fake_builder)
            self.assertNotEqual(rebuilt["python"], first["python"])
            self.assertEqual(Path(first["python"]).read_text(), "ready")
        self.assertEqual(external.read_text(), "unrelated")

    def test_metadata_cannot_redirect_generation_directory_through_symlink(self):
        first = b.setup(request(self.cache), builder=fake_builder)
        generation = Path(first["python"]).parent.parent
        elsewhere = self.cache / "elsewhere"
        generation.rename(elsewhere)
        try:
            generation.symlink_to(elsewhere, target_is_directory=True)
        except OSError as exc:
            self.skipTest(f"Directory symlinks unavailable: {exc}")
        rebuilt = b.setup(request(self.cache), builder=fake_builder)
        self.assertNotEqual(rebuilt["python"], first["python"])
        self.assertTrue((elsewhere / ("Scripts/python.exe" if os.name == "nt" else "bin/python")).is_file())

    def test_relative_checkout_metadata_requires_its_original_working_directory(self):
        req = {**request(self.cache, "./checkout"), "local": True}
        first = b.setup(req, builder=fake_builder)
        self.assertEqual(first["sourceCwd"], str(Path.cwd()))
        metadata = Path(first["python"]).parent.parent.parent / "state.json"
        missing = {key: value for key, value in first.items() if key != "sourceCwd"}
        metadata.write_text(json.dumps(missing))
        rebuilt = b.setup(req, builder=fake_builder)
        self.assertNotEqual(rebuilt["python"], first["python"])
        other = self.cache / "other"
        other.mkdir()
        with b.contextlib.chdir(other):
            different = b.setup(req, builder=fake_builder)
        self.assertNotEqual(different["python"], rebuilt["python"])

    def test_transient_metadata_permission_errors_retry_but_corrupt_json_does_not(self):
        path = self.cache / "state.json"
        with patch.object(Path, "read_text", side_effect=[PermissionError("sharing"), PermissionError("sharing"), '{"n":1}']) as read:
            self.assertEqual(b.read_json(path), {"n": 1})
            self.assertEqual(read.call_count, 3)
        with patch.object(Path, "read_text", return_value="{bad") as read:
            with self.assertRaises(ValueError):
                b.read_json(path)
            self.assertEqual(read.call_count, 1)

    def test_killed_owner_releases_os_lock_and_wait_is_bounded(self):
        ctx = multiprocessing.get_context("spawn")
        queue = ctx.Queue()
        worker = ctx.Process(target=hold_lock, args=(str(self.cache / "install.lock"), queue))
        worker.start()
        self.assertEqual(queue.get(timeout=10), "locked")
        with self.assertRaises(TimeoutError), b.install_lock(self.cache / "install.lock", timeout=0.1):
            pass
        worker.terminate()
        worker.join(10)
        with b.install_lock(self.cache / "install.lock", timeout=1):
            pass

    def test_interrupted_worker_terminates_installer_descendants(self):
        for forced in (False, True):
            heartbeat = self.cache / ("heartbeat-kill" if forced else "heartbeat-term")
            descendant_code = "from pathlib import Path;import time;p=Path(" + repr(str(heartbeat)) + ");\nwhile True:p.write_text(str(time.time()));time.sleep(0.05)"
            script = self.cache / "installer.py"
            script.write_text("import subprocess,sys,time\np=subprocess.Popen([sys.executable,'-c'," + repr(descendant_code) + "])\nprint(p.pid, flush=True)\ntime.sleep(60)\n")
            runner = self.cache / "runner.py"
            runner.write_text("import sys,signal\nsys.path.insert(0," + repr(str(Path(__file__).resolve().parents[2])) + ")\nfrom bridge import bootstrap as b\ndef stop(s,f):\n b.terminate_active();raise SystemExit(1)\nsignal.signal(signal.SIGTERM,stop)\nb.run_command([sys.executable," + repr(str(script)) + "])\n")
            proc = subprocess.Popen([sys.executable, str(runner)], stderr=subprocess.PIPE, text=True)
            try:
                int(proc.stderr.readline())
                until = time.monotonic() + 3
                while not heartbeat.exists() and time.monotonic() < until:
                    time.sleep(0.01)
                self.assertTrue(heartbeat.exists())
                proc.kill() if forced else proc.terminate()
                proc.wait(timeout=10)
                # Every installer descendant inherits this stderr pipe. EOF
                # proves they have exited; a live descendant fails within the
                # timeout even when its heartbeat is temporarily unscheduled.
                proc.communicate(timeout=10)
                stopped = heartbeat.read_text()
                time.sleep(0.2)
                self.assertEqual(heartbeat.read_text(), stopped)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stderr.close()

    @unittest.skipIf(os.name == "nt", "POSIX nested signal handling")
    def test_supervisor_handles_nested_termination_without_locking(self):
        script = f"""
import os, signal, subprocess, sys, threading
from unittest.mock import patch
sys.path.insert(0, {str(Path(__file__).resolve().parents[2])!r})
from bridge import bootstrap as b
injected = False
original_event = threading.Event
def nested_signal():
    global injected
    if not injected:
        injected = True
        signal.raise_signal(signal.SIGTERM)
class NestedEvent(original_event):
    def __init__(self):
        super().__init__()
        notify = self._cond.notify_all
        def nested_notify():
            # Reenter while Event.set owns its non-reentrant condition lock.
            nested_signal()
            notify()
        self._cond.notify_all = nested_notify
class Child:
    pid = 123456789
    def wait(self):
        signal.raise_signal(signal.SIGTERM)
        return 0
kills = []
def kill_group(*args):
    kills.append(args)
    # The lock-free implementation must handle nested delivery here too.
    nested_signal()
with patch.object(threading, 'Event', NestedEvent), patch.object(threading.Thread, 'start'), \\
     patch.object(subprocess, 'Popen', return_value=Child()), patch.object(os, 'killpg', side_effect=kill_group):
    assert b.supervise([]) == 0
assert injected and kills
print('nested termination completed')
"""
        result = subprocess.run([sys.executable, "-I", "-c", script], capture_output=True, text=True, timeout=3,
                                check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("nested termination completed", result.stdout)

    @unittest.skipIf(os.name == "nt", "Windows uses a job-assignment handshake")
    def test_supervisor_does_not_start_installer_after_early_worker_death(self):
        ready, marker = self.cache / "supervisor-ready", self.cache / "installer-started"
        wrapper = self.cache / "delayed-bootstrap.py"
        wrapper.write_text("import os,runpy,time\nfrom pathlib import Path\nparent=os.getppid()\nPath(" + repr(str(ready)) +
                           ").write_text(str(os.getpid()))\nuntil=time.monotonic()+3\n" +
                           "while os.getppid()==parent and time.monotonic()<until:time.sleep(0.01)\nrunpy.run_path(" +
                           repr(str(Path(b.__file__).resolve())) + ",run_name='__main__')\n")
        installer = "from pathlib import Path;Path(" + repr(str(marker)) + ").write_text('started')"
        runner = self.cache / "early-death-runner.py"
        runner.write_text("import sys\nsys.path.insert(0," + repr(str(Path(__file__).resolve().parents[2])) +
                          ")\nfrom bridge import bootstrap as b\nb.__file__=" + repr(str(wrapper)) +
                          "\nb.run_command([sys.executable,'-c'," + repr(installer) + "])\n")
        proc = subprocess.Popen([sys.executable, str(runner)], stderr=subprocess.PIPE, text=True)
        try:
            until = time.monotonic() + 3
            while not ready.exists() and time.monotonic() < until:
                time.sleep(0.01)
            self.assertTrue(ready.exists())
            proc.kill()
            proc.wait(timeout=5)
            # The delayed supervisor must exit without launching its installer,
            # even though it first observes its parent after being reparented.
            proc.communicate(timeout=5)
            self.assertFalse(marker.exists())
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            proc.stderr.close()

    def test_completed_installer_cannot_leave_background_descendants(self):
        heartbeat = self.cache / "completed-heartbeat"
        pidfile = self.cache / "completed-pid"
        code = "from pathlib import Path;import time;p=Path(" + repr(str(heartbeat)) + ");\nwhile True:p.write_text(str(time.time()));time.sleep(0.05)"
        script = self.cache / "completed-installer.py"
        script.write_text("import subprocess,sys,time\nfrom pathlib import Path\np=subprocess.Popen([sys.executable,'-c'," + repr(code) + "])\nPath(" + repr(str(pidfile)) + ").write_text(str(p.pid))\nwhile not Path(" + repr(str(heartbeat)) + ").exists():time.sleep(0.01)\n")
        completed = False
        try:
            b.run_command([sys.executable, str(script)])
            time.sleep(0.15)
            stopped = heartbeat.read_text()
            time.sleep(0.2)
            self.assertEqual(heartbeat.read_text(), stopped)
            completed = True
        finally:
            if not completed and pidfile.exists():
                # Cleanup also protects the test host if the regression returns.
                import signal
                with b.contextlib.suppress(OSError):
                    os.kill(int(pidfile.read_text()), signal.SIGTERM)

    @unittest.skipUnless(os.name == "nt", "Native Windows job-assignment handshake")
    def test_windows_supervisor_waits_for_job_assignment(self):
        marker = self.cache / "installer-started"
        argv = [sys.executable, "-c", "from pathlib import Path;Path(" + repr(str(marker)) + ").write_text('started')"]
        execution = {"argv": argv, "parent": os.getpid()}
        child = subprocess.Popen([sys.executable, "-I", str(Path(b.__file__).resolve()), "--execute", json.dumps(execution)],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            time.sleep(0.2)
            self.assertFalse(marker.exists())
            child.stdin.close()
            child.wait(timeout=5)
            self.assertNotEqual(child.returncode, 0)
            self.assertFalse(marker.exists())
        finally:
            if child.poll() is None:
                child.kill()
                child.wait()
            child.stdout.close()
            child.stderr.close()

    def test_metadata_readers_never_observe_partial_json(self):
        path = self.cache / "state.json"
        b.atomic_json(path, {"n": 0})
        stop, failures = threading.Event(), []
        def read():
            while not stop.is_set():
                try:
                    b.read_json(path)
                except (OSError, ValueError) as exc:
                    failures.append(str(exc))
        reader = threading.Thread(target=read)
        reader.start()
        try:
            for n in range(30):
                b.atomic_json(path, {"n": n, "payload": "x" * 10000})
        finally:
            stop.set()
            reader.join()
        self.assertEqual(failures, [])

    def test_requirements_preserve_urls_local_paths_and_extras(self):
        self.assertEqual(b.requirement("agent-trajviz[app]>=0.2", ["llm", "charts"], False), ["agent-trajviz[app,llm,charts]>=0.2"])
        self.assertEqual(b.requirement("/tmp/my checkout", ["llm", "charts"], True), ["-e", "/tmp/my checkout[llm,charts]"])
        self.assertEqual(b.requirement("git+https://example.test/a.git", ["llm"], False), ["agent-trajviz[llm] @ git+https://example.test/a.git"])
        self.assertEqual(b.requirement("agent-trajviz", [], False), ["agent-trajviz"])
        self.assertEqual(b.requirement('agent-trajviz; python_version >= "3.11"', ["llm"], False), ['agent-trajviz[llm]; python_version >= "3.11"'])

    def test_source_extras_are_recorded_and_reused(self):
        for source, local in [("agent-trajviz[App]>=0.2", False),
                              ("agent-trajviz[app] @ https://example.test/a.whl", False),
                              (str(self.cache / "local checkout") + "[app]", True)]:
            req = {**request(self.cache, source), "local": local}
            first = b.setup(req, builder=fake_builder)
            self.assertIn("app", first["extras"])
            again = b.setup({**req, "extras": ["app"]}, builder=fake_builder)
            self.assertEqual(again["python"], first["python"])


if __name__ == "__main__":
    unittest.main()
