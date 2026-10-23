"""Shared fixtures for the bridge tests."""

from __future__ import annotations

import contextlib
import io
import json
from collections.abc import Callable
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixture_session() -> Path:
    """The vendored native Claude Code transcript (see fixtures/native_claude_code/README.md)."""
    return FIXTURES / "native_claude_code" / "django__django-11477.jsonl"


@pytest.fixture
def tmp_artifacts(tmp_path: Path) -> Path:
    """An empty artifact folder for one test."""
    path = tmp_path / "artifacts"
    path.mkdir()
    return path


@pytest.fixture(autouse=True)
def _restore_subcommands():
    """Keep handlers a test registers from leaking into later tests."""
    from trajviz_obs import cli

    cli.load_modules()
    saved = dict(cli.SUBCOMMANDS)
    yield
    cli.SUBCOMMANDS.clear()
    cli.SUBCOMMANDS.update(saved)


@pytest.fixture
def run_cli() -> Callable[[list[str]], tuple[int, dict]]:
    """Call ``cli.main(argv)`` in-process; return its exit code and the parsed stdout JSON."""
    from trajviz_obs import cli

    def run(argv: list[str]) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main(argv)
        return code, json.loads(out.getvalue())

    return run
