"""Type-check against local Claude Code declarations without vendoring them.

No downloads or writes to the plugin are performed. A temporary tsconfig
includes the selected API and is removed after tsc exits.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def api_types(selected: str | None) -> Path:
    if selected:
        path = Path(selected).expanduser().resolve()
        if not path.exists():
            raise ValueError(f"API declarations do not exist: {path}")
        return path
    generated = ROOT / ".claude-plugin" / "types"
    if (generated / "claude-code" / "index.d.ts").is_file():
        return generated
    version = subprocess.run(
        ["claude", "--version"], check=True, capture_output=True, text=True
    ).stdout.split()[0]
    uid = os.getuid() if hasattr(os, "getuid") else None
    roots = [Path(tempfile.gettempdir()), Path("/tmp")]
    matches: list[Path] = []
    if uid is not None:
        for temporary in roots:
            bundled = temporary / f"claude-{uid}" / "bundled-skills" / version
            matches.extend(bundled.glob("*/plugin-authoring/types/claude-code.d.ts"))
    matches = sorted(set(matches))
    if matches:
        return matches[-1].parent
    raise ValueError(
        "Claude Code API types are missing. Load this plugin in an interactive "
        "Claude Code session, or pass --api-types /path/to/plugin-authoring/types."
    )


def compiler(selected: str | None) -> list[str]:
    command = (shutil.which(selected) or selected) if selected else shutil.which("tsc")
    if command:
        path = Path(command).expanduser().absolute()
        if path.is_file():
            return [str(path)] if os.access(path, os.X_OK) else ["node", str(path)]
        raise ValueError(f"TypeScript CLI does not exist: {path}")
    local = ROOT / "node_modules" / "typescript" / "bin" / "tsc"
    cached = sorted((Path.home() / ".npm" / "_npx").glob("*/node_modules/typescript/bin/tsc"))
    choices = ([local] if local.is_file() else []) + cached
    if choices:
        return ["node", str(choices[0])]
    raise ValueError(
        "TypeScript 5.4+ is missing. Install TypeScript for development or "
        "pass --tsc /path/to/typescript/bin/tsc."
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-types", help="Generated types directory or claude-code.d.ts")
    parser.add_argument("--tsc", help="TypeScript CLI executable or JavaScript path")
    args = parser.parse_args()
    try:
        declarations = api_types(args.api_types)
        cmd = compiler(args.tsc)
        config = json.loads((ROOT / "tsconfig.json").read_text())
        config["include"] = [str(ROOT / "hooks"), str(ROOT / "types"), str(declarations)]
        with tempfile.TemporaryDirectory(prefix="trajviz-obs-types-") as directory:
            config_path = Path(directory) / "tsconfig.json"
            config_path.write_text(json.dumps(config))
            print(f"Checking hooks against {declarations}", flush=True)
            return subprocess.run([*cmd, "-p", str(config_path)], check=False, cwd=ROOT).returncode
    except (ValueError, OSError, subprocess.CalledProcessError) as exc:
        print(f"typecheck: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
