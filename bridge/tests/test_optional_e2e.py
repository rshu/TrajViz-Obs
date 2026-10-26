"""Real optional collectors, synthetic native stores, and honest missing-DECAF behavior."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_collectors_export_real_stores_roundtrip_and_preserve_sources(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "optional_matrix.py"
    result = subprocess.run([sys.executable, str(script), "--output", str(tmp_path)],
                            capture_output=True, text=True, timeout=120, check=False)
    assert result.returncode == 0, result.stdout + result.stderr
    ledger = json.loads((tmp_path / "optional-matrix.json").read_text(encoding="utf-8"))
    assert ledger["status"] == "passed" and ledger["schema_validations"] >= 210
    refusals = [command for command in ledger["commands"] if command.get("upstream_exit") not in (None, 0)]
    assert len(refusals) == 3 and {command["args"][1] for command in refusals} == {"cursor", "opencode", "codearts"}
    assert ledger["attribution_fallback"].startswith(("passed:", "not_tested:"))
    assert ledger["attribution_verdict"].startswith("not_tested:")


def test_optional_matrix_refuses_to_overwrite_an_existing_output(tmp_path):
    script = Path(__file__).resolve().parents[2] / "scripts" / "optional_matrix.py"
    existing = tmp_path / "keep.txt"
    existing.write_text("Keep existing artifacts", encoding="utf-8")
    result = subprocess.run([sys.executable, str(script), "--output", str(tmp_path), "--prepare-only"],
                            capture_output=True, text=True, timeout=10, check=False)
    assert result.returncode == 2 and "must be empty" in result.stderr
    assert existing.read_text(encoding="utf-8") == "Keep existing artifacts"
