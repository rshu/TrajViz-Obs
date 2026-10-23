"""Launcher: ``python -I trajviz_obs_cli.py <subcommand> ...``.

``-I`` keeps the working directory and user site off ``sys.path``; only this
file's own directory is added, so the ``trajviz_obs`` package beside it loads.
"""

import os
import sys

if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
    from trajviz_obs.cli import main

    sys.exit(main())
