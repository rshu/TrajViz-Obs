"""Stdlib-only, cross-process installation of immutable TrajViz environments.

Stdout is one bounded JSON result; installer progress goes to stderr. Published
venvs are never modified or removed, so other sessions may keep using them.
"""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path

REVISION = 4
BASE_EXTRAS = ("llm", "charts")
LOCK_WAIT_S = 180
_active: subprocess.Popen | None = None
_job = None


@contextlib.contextmanager
def install_lock(path: Path, timeout: float = LOCK_WAIT_S):
    """OS-owned lock: crashes release it; the persistent file is never unlinked."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as lock:
        if not path.stat().st_size:
            lock.write(b"\0")
            lock.flush()
        deadline, said = time.monotonic() + timeout, False
        while True:
            try:
                if os.name == "nt":
                    import msvcrt
                    lock.seek(0)
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (11, 13, 35, 36):
                    raise
                if time.monotonic() >= deadline:
                    raise TimeoutError("Another session is setting up TrajViz; try again after it finishes.") from exc
                if not said:
                    print("Waiting for another session's TrajViz setup...", file=sys.stderr, flush=True)
                    said = True
                time.sleep(0.1)
        try:
            yield
        finally:
            if os.name == "nt":
                import msvcrt
                lock.seek(0)
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(lock, fcntl.LOCK_UN)


def atomic_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".state-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(value, out, ensure_ascii=True, allow_nan=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        deadline = time.monotonic() + 3
        while True:
            try:
                os.replace(name, path)
                break
            except PermissionError:
                # A Windows reader can temporarily withhold delete sharing.
                if os.name != "nt" or time.monotonic() >= deadline:
                    raise
                time.sleep(0.02)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)


def read_json(path: Path) -> dict:
    """Windows can briefly deny opening a name while atomic replacement completes."""
    for attempt in range(11):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except PermissionError:
            if attempt == 10:
                raise
            time.sleep(0.02)
    raise AssertionError("unreachable")


def load_state(path: Path, request: dict) -> dict | None:
    try:
        state = read_json(path)
        local_source = re.fullmatch(r"(.*?)(?:\[([^\]]*)\])?", request["source"], re.DOTALL)
        relative = request["local"] and not Path(local_source[1]).expanduser().is_absolute()
        python = Path(state["python"])
        generation = python.parent.parent
        expected = generation / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        if (isinstance(state, dict) and state.get("revision") == REVISION
                and state.get("source") == request["source"]
                and state.get("pythonOption", "") == request.get("pythonOption", "")
                and ("sourceCwd" not in state or state["sourceCwd"] == str(Path.cwd()))
                and (not relative or state.get("sourceCwd") == str(Path.cwd()))
                and isinstance(state.get("trajviz"), str) and bool(state["trajviz"].strip())
                and type(state.get("createdAt")) in (int, float) and 0 <= state["createdAt"] <= 8.64e15
                and isinstance(state.get("extras"), list)
                and all(isinstance(v, str) for v in state["extras"])
                and set(BASE_EXTRAS) <= set(state["extras"])
                and python == expected and generation.parent == path.parent
                and re.fullmatch(r"[0-9a-f]{32}", generation.name)
                and generation.resolve().parent == path.parent.resolve()
                and python.is_file()):
            return state
    except (OSError, ValueError, TypeError, KeyError):
        pass
    return None


def source_parts(source: str, local: bool) -> tuple[str, str | None, str]:
    named = re.fullmatch(r"([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)(?:\s*\[([^\]]*)\])?((?:\s*[<>=!~(;@].*)?)", source, re.DOTALL)
    if local:
        match = re.fullmatch(r"(.*?)(?:\[([^\]]*)\])?", source, re.DOTALL)
        return match[1], match[2], ""
    if named:
        return named[1], named[2], named[3]
    return "agent-trajviz", None, " @ " + source


def requirement(source: str, extras: list[str], local: bool) -> list[str]:
    """The source argument retains its own extras, including PEP 508 URLs."""
    base, own, tail = source_parts(source, local)
    merged = list(dict.fromkeys([*(v.strip() for v in (own or "").split(",") if v.strip()), *extras]))
    return (["-e"] if local else []) + [base + ("[" + ",".join(merged) + "]" if merged else "") + tail]


def _windows_job():
    """Kill the whole installer tree if this worker is forcibly terminated."""
    import ctypes
    from ctypes import wintypes as w
    class Basic(ctypes.Structure):
        _fields_ = [("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", w.DWORD),
                    ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t), ("processes", w.DWORD),
                    ("affinity", ctypes.c_size_t), ("priority", w.DWORD), ("scheduling", w.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("read_ops", "write_ops", "other_ops", "read_bytes", "write_bytes", "other_bytes")]
    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("process_memory", ctypes.c_size_t),
                    ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t), ("peak_job", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes, kernel.CreateJobObjectW.restype = [ctypes.c_void_p, w.LPCWSTR], w.HANDLE
    kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    handle = kernel.CreateJobObjectW(None, None)
    info = Extended()
    info.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    if not handle or not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)):
        if handle:
            kernel.CloseHandle(handle)
        raise ctypes.WinError(ctypes.get_last_error())
    return kernel, handle


def terminate_active() -> None:
    global _job
    child = _active
    if os.name == "nt":
        if _job is not None:
            kernel, handle = _job
            _job = None
            kernel.CloseHandle(handle)
        elif child is not None and child.poll() is None:
            child.kill()
    else:
        if child is None or child.poll() is not None:
            return
        # The supervisor owns a separate installer group, which it kills
        # before exiting. SIGKILL here would prevent that cleanup.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(child.pid, signal.SIGTERM)
    # Signal handlers must not wait here: they can interrupt Popen.wait()
    # while its internal lock is held. run_command's finally reaps the child.


def run_command(argv: list[str], *, capture: bool = False) -> str:
    global _active, _job
    if os.name == "nt" and _job is None:
        _job = _windows_job()
    # A separate supervisor survives a SIGKILL of this worker long enough to
    # reap the installer's process group. Windows uses kill-on-close jobs.
    command = [sys.executable, "-I", str(Path(__file__).resolve()), "--execute",
               json.dumps({"argv": argv, "parent": os.getpid()})]
    child = subprocess.Popen(command, stdout=subprocess.PIPE if capture else sys.stderr, stderr=sys.stderr,
                             stdin=subprocess.PIPE if os.name == "nt" else subprocess.DEVNULL,
                             start_new_session=os.name != "nt")
    _active = child
    deadline = threading.Timer(60, lambda: terminate_active() if _active is child else None) if capture else None
    if deadline is not None:
        deadline.start()
    try:
        if _job is not None:
            import ctypes
            kernel, handle = _job
            if not kernel.AssignProcessToJobObject(handle, int(child._handle)):
                child.kill()
                raise ctypes.WinError(ctypes.get_last_error())
            # The supervisor cannot start an installer before its job has
            # been assigned; even a fast-spawning descendant inherits it.
            child.stdin.write(b"start\n")
            child.stdin.close()
        if capture:
            # Verification emits only a version. Bound output before retaining it.
            data = child.stdout.read(4097)
            if len(data) > 4096:
                raise RuntimeError("Environment verification returned too much output.")
            result = data.decode("utf-8").strip()
        else:
            result = ""
        code = child.wait()
        if code:
            raise RuntimeError(f"{Path(argv[0]).name} exited with {code}; see the setup progress above.")
        return result
    finally:
        if deadline is not None:
            deadline.cancel()
            deadline.join()
        terminate_active()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=5)
        _active = None


def supervise(argv: list[str], *, parent: int | None = None) -> int:
    """POSIX installer group: parent death also stops grandchildren after SIGKILL."""
    if os.name == "nt":
        # EOF means the worker died before assigning this process to its job.
        if sys.stdin.buffer.readline() != b"start\n":
            return 1
        return subprocess.call(argv, stdin=subprocess.DEVNULL)
    parent = os.getppid() if parent is None else parent
    if os.getppid() != parent:
        return 1
    child = None
    stopped = False
    def stop(_signum=None, _frame=None):
        nonlocal stopped
        # A second SIGTERM can interrupt this handler. Event.set()/locks can
        # deadlock here; repeated process-group kills are safe and lock-free.
        stopped = True
        if child is not None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(child.pid, signal.SIGKILL)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    def watch():
        while not stopped:
            time.sleep(0.1)
            if os.getppid() != parent:
                stop()
    threading.Thread(target=watch, daemon=True).start()
    try:
        child = subprocess.Popen(argv, start_new_session=True, stdin=subprocess.DEVNULL)
        if stopped:
            stop()
        return child.wait()
    finally:
        # A successful installer can otherwise leave background descendants
        # modifying an environment after it has been published.
        stop()


def build_generation(generation: Path, request: dict, extras: list[str]) -> str:
    uv = request.get("uv")
    if uv:
        run_command([uv, "venv", "--python", request.get("pythonOption") or ">=3.11", str(generation)])
    else:
        run_command([sys.executable, "-m", "venv", str(generation)])
    python = generation / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    pip = [uv, "pip", "install", "--python", str(python)] if uv else [str(python), "-m", "pip", "install"]
    run_command([*pip, *requirement(request["installSource"], extras, request["local"])])
    imports = "import agent_trajviz, agent_trajviz.llm, plotly; "
    if "app" in extras:
        imports += "import gradio; "
    return run_command([str(python), "-I", "-c", imports + 'import importlib.metadata as m; print(m.version("agent-trajviz"))'], capture=True)


def setup(request: dict, *, builder=build_generation) -> dict:
    cache = Path(request["cache"]).resolve()
    source = request["installSource"]
    if request["local"]:
        match = re.fullmatch(r"(.*?)(?:\[([^\]]*)\])?", source, re.DOTALL)
        source = str(Path(match[1]).expanduser().resolve()) + ("[" + match[2] + "]" if match[2] else "")
    request = {**request, "installSource": source}
    own = source_parts(source, request["local"])[1]
    source_extras = [value.strip().lower() for value in (own or "").split(",") if value.strip()]
    identity = {"source": source, "python": request.get("pythonOption", ""), "revision": REVISION}
    key = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:24]
    folder = cache / "envs" / key
    with install_lock(cache / "install.lock", request.get("lockWaitSeconds", LOCK_WAIT_S)):
        old = load_state(folder / "state.json", request)
        extras = list(dict.fromkeys([*BASE_EXTRAS, *(old["extras"] if old else []), *source_extras, *request.get("extras", [])]))
        if old and set(extras) <= set(old["extras"]):
            atomic_json(cache / "state.json", old)
            return old
        generation = folder / uuid.uuid4().hex
        generation.mkdir(parents=True)
        published = False
        try:
            print("Installing TrajViz into an isolated environment...", file=sys.stderr, flush=True)
            version = builder(generation, request, extras)
            state = {"revision": REVISION, "python": str(generation / ("Scripts/python.exe" if os.name == "nt" else "bin/python")),
                     "trajviz": version, "extras": extras, "source": request["source"],
                     "pythonOption": request.get("pythonOption", ""), "createdAt": int(time.time() * 1000)}
            if request["local"] and not Path(request["source"].split("[")[0]).expanduser().is_absolute():
                state["sourceCwd"] = str(Path.cwd())
            atomic_json(folder / "state.json", state)
            published = True
            atomic_json(cache / "state.json", state)
            return state
        except BaseException:
            if not published:
                shutil.rmtree(generation, ignore_errors=True)
            raise


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--execute":
        execution = json.loads(sys.argv[2])
        return supervise(execution["argv"], parent=execution["parent"])
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    def interrupted(_signum, _frame):
        terminate_active()
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    # uv may be this worker's parent. If its host terminates uv abruptly,
    # ensure installers cannot survive as detached grandchildren.
    parent = os.getppid()
    def watch_parent():
        while True:
            time.sleep(0.25)
            if os.getppid() != parent:
                terminate_active()
                os._exit(1)
    if os.name != "nt":
        threading.Thread(target=watch_parent, daemon=True).start()
    else:
        # On Windows getppid() retains the original PID after parent exit.
        # A synchronization handle detects uv/Claude termination instead.
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        parent_handle = kernel.OpenProcess(0x100000, False, parent)
        if not parent_handle:
            raise ctypes.WinError(ctypes.get_last_error())
        def watch_windows_parent():
            kernel.WaitForSingleObject(parent_handle, 0xFFFFFFFF)
            kernel.CloseHandle(parent_handle)
            terminate_active()
            os._exit(1)
        threading.Thread(target=watch_windows_parent, daemon=True).start()
    try:
        request = json.loads(args.request)
        result = setup(request)
        sys.stdout.write(json.dumps(result, allow_nan=False) + "\n")
        return 0
    except (Exception, KeyboardInterrupt) as exc:  # noqa: BLE001 - CLI failures must stay within the JSON protocol
        sys.stdout.write(json.dumps({"error": {"code": "setup_failed", "message": str(exc) or "Setup was interrupted."}}) + "\n")
        return 1
    finally:
        terminate_active()


if __name__ == "__main__":
    raise SystemExit(main())
