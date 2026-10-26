#!/usr/bin/env python3
"""Run local plugin checks without installing dependencies or invoking a model.

Use --bridge-only for the Python CI suite. Full checks also require Claude Code
with the function-hooks API, TypeScript, and this host's API declarations.
"""

from __future__ import annotations

import argparse
import os
import shlex
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bridge-only", action="store_true", help="Run pytest and Ruff without Claude Code or Node")
    parser.add_argument("--python", help="Bridge interpreter (default: bridge/.venv, otherwise this interpreter)")
    parser.add_argument("--claude", default="claude", help="Claude Code executable")
    parser.add_argument("--api-types", help="Claude Code declarations for scripts/typecheck.py")
    parser.add_argument("--tsc", help="TypeScript executable or JavaScript CLI for scripts/typecheck.py")
    parser.add_argument("--dry-run", action="store_true", help="Print the checks without running them")
    args = parser.parse_args()
    managed = ROOT / "bridge" / ".venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    python = args.python or (str(managed) if managed.is_file() else sys.executable)
    checks: list[tuple[str, list[str]]] = []
    if not args.bridge_only:
        typecheck = [sys.executable, str(ROOT / "scripts" / "typecheck.py")]
        for name in ("api_types", "tsc"):
            value = getattr(args, name)
            if value:
                typecheck += ["--" + name.replace("_", "-"), value]
        checks += [
            ("plugin validation", [args.claude, "plugin", "validate", "--strict", str(ROOT)]),
            ("hook tests", [args.claude, "plugin", "test", str(ROOT)]),
            ("TypeScript", typecheck),
        ]
    checks += [
        ("bridge tests", [python, "-m", "pytest", str(ROOT / "bridge" / "tests"), "-q"]),
        ("command and schema matrix", [python, str(ROOT / "scripts" / "command_matrix.py"), "--python", python]),
        ("format and extension matrix", [python, str(ROOT / "scripts" / "formats_e2e.py"), "--python", python]),
        ("Python lint", [python, "-m", "ruff", "check", str(ROOT / "bridge"), str(ROOT / "scripts")]),
    ]
    failed: list[str] = []
    for label, command in checks:
        print(f"\n{label}: {shlex.join(command)}", flush=True)
        if args.dry_run:
            continue
        try:
            status = subprocess.run(command, cwd=ROOT, check=False).returncode
        except OSError as exc:
            print(f"Cannot run {label}: {exc}", file=sys.stderr)
            status = 127
        if status:
            failed.append(f"{label} (exit {status})")
    if failed:
        print("\nFailed: " + ", ".join(failed), file=sys.stderr)
        return 1
    if not args.dry_run:
        print("\nAll checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
