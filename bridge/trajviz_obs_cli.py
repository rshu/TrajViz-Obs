"""Launcher: ``python -I trajviz_obs_cli.py <subcommand> ...``.

``-I`` keeps the working directory and user site off ``sys.path``; only this
file's own directory is added, so the ``trajviz_obs`` package beside it loads.
A bridge that cannot be imported (``agent_trajviz`` missing or broken) still
answers with the ``internal`` envelope and exit 3, its traceback on stderr.
"""

import os
import sys


def _watch_host_parent(argv: list[str]) -> list[str]:
    """Private POSIX host ownership; the unflagged Claude launcher is unchanged."""
    if not argv or argv[0] != "--host-parent":
        return argv
    if len(argv) < 2 or not argv[1].isascii() or not argv[1].isdecimal() or int(argv[1]) <= 0:
        raise ValueError("--host-parent requires a positive parent process id.")
    parent = int(argv[1])
    if os.name != "posix":
        raise ValueError("--host-parent requires POSIX process ownership.")
    if os.getpgrp() != os.getpid():
        raise ValueError("--host-parent requires a dedicated process group.")
    if os.getppid() != parent:
        raise ValueError("--host-parent must identify the current parent process.")
    import signal
    import threading
    import time

    group = os.getpid()

    def watch():
        while True:
            if os.getppid() != parent:
                # The native host gave this launcher its own process group.
                # SIGKILL also stops inherited children when the host itself
                # was killed and cannot run its ordinary cancellation path.
                if os.getpgrp() == group:
                    os.killpg(group, signal.SIGKILL)
                os._exit(1)
            time.sleep(0.1)

    threading.Thread(target=watch, daemon=True).start()
    return argv[2:]


if __name__ == "__main__":
    try:
        argv = _watch_host_parent(sys.argv[1:])
    except ValueError as exc:
        import json

        sys.stdout.write(json.dumps({"error": {"code": "usage", "message": str(exc)}}) + "\n")
        sys.stdout.flush()
        sys.exit(2)
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    try:
        from trajviz_obs.cli import main
    except Exception as exc:  # noqa: BLE001 - the import is outside cli.main's envelope, so answer here
        import json
        import traceback

        traceback.print_exc(file=sys.stderr)
        sys.stdout.write(json.dumps({"error": {"code": "internal", "message": f"{type(exc).__name__}: {exc}"}}) + "\n")
        sys.stdout.flush()
        sys.exit(3)

    sys.exit(main(argv))
