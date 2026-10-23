"""Launcher: ``python -I trajviz_obs_cli.py <subcommand> ...``.

``-I`` keeps the working directory and user site off ``sys.path``; only this
file's own directory is added, so the ``trajviz_obs`` package beside it loads.
A bridge that cannot be imported (``agent_trajviz`` missing or broken) still
answers with the ``internal`` envelope and exit 3, its traceback on stderr.
"""

import os
import sys

if __name__ == "__main__":
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

    sys.exit(main())
