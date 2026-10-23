"""The bridge's own failure type, rendered as the error envelope."""

from __future__ import annotations

from typing import Any


class BridgeError(Exception):
    """A failure the bridge reports as ``{"error": {"code", "message", "path"?, ...}}`` with exit 1."""

    def __init__(self, code: str, message: str, *, path: str | None = None, **extra: Any) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.path = path
        self.extra = extra

    def to_dict(self) -> dict[str, Any]:
        body: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.path is not None:
            body["path"] = self.path
        body.update(self.extra)
        return body
