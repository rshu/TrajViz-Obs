"""Explicit, host-owned request-capture capability; never serialized into a trajectory.

A dashboard upload cannot select a capture directory. Only its pinned native
source receives a scope, and each view re-reads the journal for live identity
resolution. Ordinary trajectory/JSON exports keep their original data.
"""
from __future__ import annotations

import weakref
from dataclasses import dataclass
from pathlib import Path

from agent_trajviz import Trajectory
from agent_trajviz.views import VIEW_CATALOG, View

from .errors import BridgeError


@dataclass(frozen=True)
class RequestScope:
    root: Path
    session_id: str


_SCOPES: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def bind_request_scope(run: Trajectory, root: Path | None, session_id: str | None) -> None:
    if root is None and session_id is None:
        return
    if root is None or not root.is_absolute() or "\0" in str(root) or ".." in root.parts or not session_id:
        raise BridgeError("request_capture_scope", "Request capture needs an absolute root and an explicit native session ID.")
    from .deveco import NATIVE_KEY

    native = run.raw.get(NATIVE_KEY)
    session = native.get("session") if isinstance(native, dict) else None
    if run.format != "deveco" or not isinstance(session, dict) or session.get("id") != session_id:
        raise BridgeError("request_capture_scope", "The host-selected native DevEco session does not match the capture scope.")
    _SCOPES[run] = RequestScope(root, session_id)


def has_request_scope(run: Trajectory | None) -> bool:
    """Whether this in-memory trajectory owns the host's private capability."""
    return run is not None and run in _SCOPES


def inherit_request_scope(source: Trajectory | None, target: Trajectory) -> None:
    scope = _SCOPES.get(source) if source is not None else None
    if scope is not None:
        # Projection clones share the actual parsed native document. Matching
        # metadata in a different imported document cannot inherit capability.
        if target.raw is not source.raw:
            raise BridgeError("request_capture_scope", "Private capture scope can only follow its source projection.")
        bind_request_scope(target, scope.root, scope.session_id)


def captured_view(run: Trajectory, view_id: str, **options) -> View | None:
    scope = _SCOPES.get(run)
    if view_id != "prompt_construction" or scope is None:
        return None
    from .deveco_requests import inspect_prompt_construction, load_request_evidence

    evidence = load_request_evidence(scope.root, native_raw=run.raw, steps=run.steps)
    data = inspect_prompt_construction(run.raw, run.steps, options.get("request_id"), evidence=evidence)
    if data is None:
        return None
    spec = VIEW_CATALOG[view_id]
    return View(id=view_id, title=spec["title"], kind=spec["kind"],
                description=spec["description"], data=data)
