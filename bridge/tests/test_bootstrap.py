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
        self.assertEqual(again["extras"], ["llm", "charts", "app"])
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
                # Forced death is observed by the independent supervisor's
                # 100 ms parent watcher; allow it to reap the process group.
                time.sleep(0.3)
                stopped = heartbeat.read_text()
                time.sleep(0.2)
                self.assertEqual(heartbeat.read_text(), stopped)
            finally:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
                proc.stderr.close()


    def test_metadata_readers_never_observe_partial_json(self):
        path = self.cache / "state.json"
        b.atomic_json(path, {"n": 0})
        stop, failures = threading.Event(), []
        def read():
            while not stop.is_set():
                try:
                    json.loads(path.read_text())
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


if __name__ == "__main__":
    unittest.main()
