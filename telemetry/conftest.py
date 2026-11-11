"""Locate the plugin checkout for paired service and sender tests."""

from __future__ import annotations

import os
from pathlib import Path

import pytest


@pytest.fixture(scope="session")
def client_bridge() -> Path:
    configured = os.environ.get("TRAJVIZ_OBS_CLIENT_CHECKOUT")
    checkout = Path(configured).expanduser().resolve() if configured else Path(__file__).resolve().parents[1]
    bridge = checkout / "bridge"
    if (bridge / "telemetry_client.py").is_file():
        return bridge
    if configured:
        pytest.fail("TRAJVIZ_OBS_CLIENT_CHECKOUT must point to a checkout with the telemetry sender.")
    pytest.skip("Set TRAJVIZ_OBS_CLIENT_CHECKOUT to run paired sender and service checks.")
