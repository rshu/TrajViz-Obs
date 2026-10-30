"""``llm-prompt``, ``llm-apply``, ``ask``, ``fixes`` and ``label``: TrajViz's LLM features in two modes.

Session mode (the default) never calls a model. ``llm-prompt`` runs the feature once with a ``chat`` that
records each (already redacted) prompt and answers a valid placeholder, so every prompt comes out of one pass:
``llm.ask(..., chat=)`` for ``analyze``, ``llm.suggest_fixes(..., chat=)`` for ``fixes`` and the labeler under
:func:`compat.labeler_chat` for ``label``. The mod sends the prompts to the session's model and writes the
replies; ``llm-apply`` re-runs the same call with a ``chat`` answering reply *i* on call *i* (the call order is
fixed for the same transcript, which the hash checks) and returns TrajViz's own result.

Env mode (``ask``, ``fixes``, ``label``) runs the same features through TrajViz's own HTTP client with the
``ANALYZE_*`` / ``LABEL_*`` settings (``.env`` in the working directory included); ``label`` sends only the
first ``max_steps`` steps (:func:`compat.labeler_step_cap`), as session mode plans them.

``agent_trajviz.llm`` needs the ``llm`` extra, so it is imported only when a feature runs.
"""

from __future__ import annotations

import argparse
import contextlib
import functools
import hashlib
import itertools
import json
import math
import os
import re
import tempfile
import time
import types
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, get_args

from agent_trajviz import Trajectory

from .cli import register
from .compare_cmd import label_views_payload
from .compat import (
    EmptySession,
    check_supported,
    input_files,
    labeler_chat,
    labeler_run,
    labeler_step_cap,
    llm_env_value,
    load_plugins,
    project_analysis,
    project_brief,
    protect_run_outputs,
    window_info,
)
from .errors import BridgeError
from .outputs import protect_inputs, staged_path
from .sessions import _positive_int
from .text import LINE_CHARS, _clip
from .views_cmd import INLINE_JSON_BYTES, _artifacts, _load, _session_args, _stdout_bytes, _write_json

Feature = Literal["analyze", "fixes", "label"]
FEATURES: tuple[str, ...] = get_args(Feature)

#: Most issues the fix judge takes (TrajViz's ``JUDGE_ISSUE_CAP``).
FIXES_CAP = 8
#: The labeler's own reply cap, and the timeout the mod gives each label call.
LABEL_MAX_TOKENS, LABEL_TIMEOUT_S = 1024, 120
#: Default number of steps labelled (the mod's ``labelMaxSteps``).
DEFAULT_MAX_STEPS = 50
#: Name recorded as the sidecar's model when the session's model labelled it and the mod named none.
SESSION_MODEL = "session"

#: What a capture answers each call: a reply TrajViz accepts without failing the pass.
PLACEHOLDERS: dict[str, str] = {
    "analyze": "",
    "fixes": json.dumps({"where": "", "fix": "", "also": "", "confidence": "low"}),
    "label": json.dumps({"phase": "unknown", "action": "unknown"}),
}

_ROLES = {"user": "User", "assistant": "Assistant"}

Chat = Callable[..., str]


class ReplyError(ValueError):
    """A reply the mod could not get; TrajViz reports it in its own words (a ``ValueError`` keeps the reason)."""


# -- shared ----------------------------------------------------------------------

def _feature(feature: str) -> None:
    if feature not in FEATURES:
        raise BridgeError("bad_feature", f"No LLM feature {feature!r}; one of {', '.join(FEATURES)}.",
                          features=list(FEATURES))


def _question(question: str | None, language: str | None) -> str:
    from agent_trajviz import llm

    return (question or "").strip() or llm.first_question(language)


def _hash(run: Trajectory, feature: str, question: str | None, limit: int, prompts: list[dict],
          label_settings: dict | None = None) -> str:
    """Bind replies to the exact requests, including history, language, call limits and the real window.

    The transcript hash alone is insufficient: history can change without another transcript record, and
    overrides or a language change can produce different requests from the same session bytes.
    """
    # Reloadable exports preserve the original transcript's sha256. Bind the normalized steps too: an
    # edited export's unplanned label tail must not inherit the original request's identity.
    payload = json.dumps([run.sha256, run.steps, window_info(run)["window"], feature, question, limit, prompts, label_settings],
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_history(path: Path | None) -> list[dict[str, Any]]:
    """The prior analysis exchange kept in *path* (``{"history": [{role, content}, ...]}``); none when absent."""
    if path is None or not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BridgeError("bad_history", f"Cannot read the analysis history {path}: {exc}", path=str(path)) from exc
    history = data.get("history") if isinstance(data, dict) else None
    if not isinstance(history, list):
        raise BridgeError("bad_history", f"{path} holds no analysis history.", path=str(path))
    for index, message in enumerate(history):
        if (not isinstance(message, dict) or message.get("role") not in ("user", "assistant")
                or not isinstance(message.get("content"), str)):
            raise BridgeError("bad_history", f"Invalid analysis history message {index} in {path}; expected a "
                              "user or assistant role and text content.", path=str(path))
    return history


def _history_lock_path(path: Path) -> Path:
    return path.with_name(f".{path.name}.lock")


@contextlib.contextmanager
def _history_commit(path: Path):
    """Serialize the final history comparison and replacement across bridge processes."""
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # Keep this inode: unlinking a lock file lets a third writer bypass a waiting writer's lock.
        with _history_lock_path(path).open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                if handle.tell() == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl

                fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError as exc:
        raise BridgeError("bad_history", f"Cannot save analysis history {path} ({type(exc).__name__}).",
                          path=str(path)) from exc


def _render(messages: list[dict[str, str]]) -> str:
    """One message as it is; an exchange as ``User: …`` / ``Assistant: …`` blocks, the new question last."""
    if len(messages) == 1:
        return messages[0]["content"]
    return "\n\n".join(f"{_ROLES.get(m['role'], m['role'])}: {m['content']}" for m in messages)


def _limits(feature: str) -> tuple[int, int]:
    """``(max_tokens, timeout_s)`` of one call of *feature*."""
    if feature == "label":
        return LABEL_MAX_TOKENS, LABEL_TIMEOUT_S
    from agent_trajviz import llm

    config = llm.config_from_env()
    return config.max_tokens, config.timeout


def _fixes_text(issues: list[dict], errors: list[str]) -> str:
    lines = [f"Fixes for the top {len(issues)} issue{'s' if len(issues) != 1 else ''}:"]
    for n, issue in enumerate(issues, 1):
        judgment = issue.get("judgment")
        head = f"{n}. {issue.get('title')}"
        if not judgment:
            lines += [_clip(head, LINE_CHARS), "   no judgment"]
            continue
        lines.append(_clip(f"{head} (confidence {judgment.get('confidence')})", LINE_CHARS))
        lines += [_clip(f"   {key}: {judgment[key]}", LINE_CHARS) for key in ("where", "fix", "also") if judgment.get(key)]
    return "\n".join(lines + _errors_lines(errors))


def _errors_lines(errors: list[str]) -> list[str]:
    return ["", "Errors:", *(_clip(f"- {error}", LINE_CHARS) for error in errors)] if errors else []


def _finite_number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _label_settings(options: dict | None = None, *, env: bool = False) -> dict:
    """Validate and snapshot every label input before planning, replay or provider execution."""
    from agent_trajviz.labeling.taxonomy import DEFAULT_TAXONOMY, load_taxonomy

    values = dict(options or {})
    if "_taxonomy_text" in values:
        return values
    settings = {"max_content_chars": 8000, "delay": 0.0, "user_phase": "user", "user_action": "user_prompt",
                "max_tokens": LABEL_MAX_TOKENS, "temperature": None, **values}
    for key in ("max_content_chars", "max_tokens"):
        if type(settings[key]) is not int or settings[key] < 1 or (key == "max_tokens" and settings[key] > 64000):
            raise BridgeError("bad_label_option", f"{key.replace('_', '-')} must be a positive whole number"
                              + (" up to 64000." if key == "max_tokens" else "."))
    if not _finite_number(settings["delay"]) or not 0 <= settings["delay"] <= 3600:
        raise BridgeError("bad_label_option", "delay must be a finite number from 0 to 3600 seconds.")
    for key in ("user_phase", "user_action"):
        if not isinstance(settings[key], str) or not settings[key].strip():
            raise BridgeError("bad_label_option", f"{key.replace('_', '-')} needs a non-empty label.")
    if settings["temperature"] is not None and (not _finite_number(settings["temperature"])
            or settings["temperature"] < 0):
        raise BridgeError("bad_label_option", "temperature must be a finite number of zero or more.")
    if env:
        for key, name, default, numeric in (("max_tokens", "LABEL_MAX_TOKENS", LABEL_MAX_TOKENS, int),
                                             ("temperature", "LABEL_TEMPERATURE", 0.3, float)):
            if key in values:
                continue
            try:
                value = numeric(llm_env_value(name))
                valid = _finite_number(value) and value >= (1 if key == "max_tokens" else 0)
                if key == "max_tokens":
                    valid = valid and value <= 64000
                settings[key] = value if valid else default
            except (ValueError, OverflowError):
                settings[key] = default
    taxonomy = Path(settings.get("taxonomy_path") or DEFAULT_TAXONOMY).expanduser().absolute()
    try:
        text = taxonomy.read_text(encoding="utf-8")
        with tempfile.TemporaryDirectory(prefix="trajviz-obs-taxonomy-check-") as folder:
            captured = Path(folder) / "taxonomy.md"
            captured.write_text(text, encoding="utf-8")
            mapping, _ = load_taxonomy(captured)
    except FileNotFoundError as exc:
        raise BridgeError("not_found", f"No such taxonomy file: {taxonomy}", path=str(taxonomy)) from exc
    except (OSError, UnicodeError) as exc:
        raise BridgeError("bad_taxonomy", f"Cannot read taxonomy {taxonomy} ({type(exc).__name__}).",
                          path=str(taxonomy)) from exc
    actions = [action for group in mapping.values() for action in group]
    if not mapping or not actions or any(not group for group in mapping.values()) or len(set(actions)) != len(actions):
        raise BridgeError("bad_taxonomy", "Taxonomy needs ### phase headings and unique `action` bullets for each phase.",
                          path=str(taxonomy))
    return {**settings, "taxonomy_path": str(taxonomy), "_taxonomy_text": text,
            "_taxonomy_sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def _prepare_label_output(out_path: Path, *, sources: list[Path]) -> None:
    """Reject predictable output failures before the host or HTTP client spends model calls."""
    try:
        if out_path.exists() and not out_path.is_file():
            raise BridgeError("bad_path", f"{out_path} is not a file; give a label output file path.", path=str(out_path))
        protect_inputs([out_path], sources)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(prefix=".trajviz-obs-check-", dir=out_path.parent):
            pass
    except OSError as exc:
        raise BridgeError("bad_path", f"Cannot write label output to {out_path} ({type(exc).__name__}).",
                          path=str(out_path)) from exc


def _label(transcript_path: str, out_path: Path, *, assistant_only: bool, model: str,
           label_options: dict | None = None, **client: Any) -> dict:
    """Label the captured run against the taxonomy bytes validated for this operation."""
    from agent_trajviz.labeling.labeler import OutputSafetyError, label_trajectory

    options = _label_settings(label_options)
    _prepare_label_output(out_path, sources=[Path(transcript_path), Path(options["taxonomy_path"])])
    text = options.pop("_taxonomy_text")
    options.pop("_taxonomy_sha256")
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with staged_path(out_path) as staged, tempfile.TemporaryDirectory(prefix="trajviz-obs-taxonomy-") as folder:
            taxonomy = Path(folder) / "taxonomy.md"
            taxonomy.write_text(text, encoding="utf-8")
            options["taxonomy_path"] = str(taxonomy)
            return label_trajectory(transcript_path, str(staged), model=model, assistant_only=assistant_only,
                                    **{"base_url": "", "api_key": "", **options, **client})
    except OutputSafetyError as exc:
        raise BridgeError("bad_output", str(exc), path=str(out_path)) from exc
    except OSError as exc:
        raise BridgeError("bad_path", f"Cannot write labels to {out_path} ({type(exc).__name__}).",
                          path=str(out_path)) from exc


def _run(run: Trajectory, feature: str, *, chat: Chat | None, config: Any, transcript_path: str, question: str,
         history: list[dict], limit: int, language: str | None, out_path: Path, assistant_only: bool, model: str,
         label_client: dict | None = None, label_options: dict | None = None) -> Any:
    """One pass of *feature*: through *chat* (a capture or replay), else TrajViz's client with *config*."""
    from agent_trajviz import llm

    if feature == "analyze":
        # Keep the public client's redaction, history, configuration and error
        # handling, while giving it the same evidence-aware brief as /obs brief.
        original = llm.ask
        if not isinstance(original, types.FunctionType):
            raise BridgeError("unsupported_trajviz", "TrajViz's analysis client changed; update the plugin.")
        ask = types.FunctionType(original.__code__, {**original.__globals__, "brief": project_brief},
                                 original.__name__, original.__defaults__, original.__closure__)
        ask.__kwdefaults__ = original.__kwdefaults__
        return ask(run, question, history, config=config, chat=chat, language=language)
    if feature == "fixes":
        return llm.suggest_fixes(_AnalysisRun(run), config=config, chat=chat, language=language, limit=limit)
    with labeler_run(run):
        if chat is None:
            return _label(transcript_path, out_path, assistant_only=assistant_only, model=model,
                          label_options=label_options, **(label_client or {}))
        with labeler_chat(chat):
            return _label(transcript_path, out_path, assistant_only=assistant_only, model=model, label_options=label_options)


class _AnalysisRun:
    """Read-only analysis facade; native canonical steps and source stay unchanged."""

    def __init__(self, run: Trajectory) -> None:
        self._run = run

    @property
    def _analysis(self):
        return project_analysis(self._run)

    def __getattr__(self, name: str):
        return getattr(self._run, name)


def _capture(run: Trajectory, feature: str, *, transcript_path: str, question: str, history: list[dict],
             limit: int, language: str | None, label_options: dict | None = None) -> list[tuple[str, str]]:
    """Every ``(system, prompt)`` *feature* would send, in call order."""
    calls: list[tuple[str, str]] = []
    placeholder = PLACEHOLDERS[feature]

    def chat(system: str, messages: list[dict[str, str]] | str) -> str:
        calls.append((system, messages if isinstance(messages, str) else _render(messages)))
        return placeholder

    with tempfile.TemporaryDirectory(prefix="trajviz-obs-") as scratch:
        _run(run, feature, chat=chat, config=None, transcript_path=transcript_path, question=question,
             history=history, limit=limit, language=language, out_path=Path(scratch) / "labels.json",
             assistant_only=False, model=SESSION_MODEL, label_options=label_options)
    return calls


def _result(feature: str, out: Any, *, history_path: Path | None, limit: int, out_path: Path,
            label_errors: list[str] | None = None, expected_history: list[dict] | None = None) -> dict:
    if feature == "analyze":
        if history_path is not None:
            with _history_commit(history_path):
                if expected_history is not None:
                    try:
                        unchanged = _read_history(history_path) == expected_history
                    except BridgeError:
                        unchanged = False
                    if not unchanged:
                        raise BridgeError("stale_prompts", "The analysis history changed while this request ran; "
                                          "retry against the latest exchange.", path=str(history_path))
                _write_json(history_path, {"history": out})
        return {"reply": out[-1]["content"], "history_path": None if history_path is None else str(history_path)}
    if feature == "fixes":
        issues, errors = out
        judged = issues[:limit]
        return {"issues": judged, "errors": errors, "text": _fixes_text(judged, errors)}
    views, errors = label_views_payload(str(out_path), None), list(label_errors or [])
    return {"path": str(out_path), "summary": views["summary"],
            "text": "\n".join([views["text"], *_errors_lines(errors)]), "errors": errors}


# -- session mode ----------------------------------------------------------------

def plan_prompts(run: Trajectory, feature: Feature, *, transcript_path: str, question: str | None,
                 history_path: Path | None, limit: int, language: str | None, max_steps: int,
                 assistant_only: bool, label_options: dict | None = None,
                 out_path: Path | None = None, _history: list[dict] | None = None) -> dict:
    """The prompts *feature* would send: ``{"feature", "hash", "plan": {"calls", "approx_input_tokens"},
    "prompts": [{"index", "system", "prompt", "max_tokens", "timeout_s"}]}``.

    ``analyze`` asks *question* (TrajViz's first-pass question when none) after the exchange kept in
    *history_path*; ``fixes`` judges the first *limit* issues; ``label`` takes the first *max_steps* steps
    the labeler sends (*assistant_only* changes only which records the sidecar keeps).
    """
    _feature(feature)
    label_options = _label_settings(label_options) if feature == "label" else None
    if label_options is not None and out_path is not None:
        _prepare_label_output(out_path, sources=[Path(transcript_path), Path(label_options["taxonomy_path"])])
        protect_run_outputs(run, [out_path])
    question = _question(question, language) if feature == "analyze" else None
    calls = _capture(run, feature, transcript_path=transcript_path, question=question or "",
                     history=(_read_history(history_path) if _history is None else _history) if feature == "analyze" else [],
                     limit=limit, language=language, label_options=label_options)
    if feature == "label":
        calls = calls[:max_steps]
    max_tokens, timeout_s = _limits(feature)
    if label_options is not None:
        max_tokens = label_options["max_tokens"]
    prompts = [{"index": i, "system": system, "prompt": prompt, "max_tokens": max_tokens, "timeout_s": timeout_s}
               for i, (system, prompt) in enumerate(calls)]
    chars = sum(len(system) + len(prompt) for system, prompt in calls)
    settings_hash = ({**label_options, "assistant_only": assistant_only, "max_steps": max_steps}
                     if label_options is not None else None)
    if label_options is not None:
        for prompt in prompts:
            prompt["delay_s"] = label_options["delay"]
    return {"feature": feature, "hash": _hash(run, feature, question, limit, prompts, settings_hash),
            "plan": {"calls": len(prompts), "approx_input_tokens": -(-chars // 4)}, "prompts": prompts}


def _replies_by_index(replies: dict) -> dict[int, dict]:
    items = replies.get("replies") if isinstance(replies, dict) else None
    if not isinstance(items, list):
        raise BridgeError("bad_replies", 'Replies are {"hash", "replies": [{"index", "text"} | {"index", "error"}]}.')
    out: dict[int, dict] = {}
    for item in items:
        ok = (isinstance(item, dict) and type(item.get("index")) is int and item["index"] >= 0
              and (isinstance(item.get("text"), str) or isinstance(item.get("error"), str)))
        if not ok:
            raise BridgeError("bad_replies", f"Not a reply: {json.dumps(item)[:200]}")
        if item["index"] in out:
            raise BridgeError("bad_replies", f"More than one reply for prompt {item['index']}.")
        out[item["index"]] = item
    return out


def apply_replies(run: Trajectory, feature: Feature, replies: dict, *, transcript_path: str, question: str | None,
                  history_path: Path | None, limit: int, language: str | None, out_path: Path, max_steps: int,
                  assistant_only: bool, model: str | None = None, label_options: dict | None = None) -> dict:
    """TrajViz's result of *feature* with the model's *replies* to :func:`plan_prompts`'s prompts.

    ``analyze`` → ``{"reply", "history_path"}`` (plus ``error`` when the reply failed), the exchange appended
    to *history_path*; ``fixes`` → ``{"issues", "errors", "text"}``; ``label`` → ``{"path", "summary", "text",
    "errors"}`` with the sidecar at *out_path* (*model* names the labelling model in it). A reply
    ``{"index", "error"}`` (or none for a planned prompt) is a failed call: TrajViz's error for ``analyze`` and
    ``fixes``; for ``label`` an ``unknown`` label and ``"prompt <i>: <error>"`` in ``errors``. A step past
    *max_steps* is labelled ``unknown`` too, but was never planned, so it is no error. Replies made for other
    prompts (another transcript, history, language, context window or limit) raise
    ``BridgeError("stale_prompts")``.
    """
    _feature(feature)
    protect_run_outputs(run, [history_path, _history_lock_path(history_path)]
                        if feature == "analyze" and history_path is not None else [])
    label_options = _label_settings(label_options) if feature == "label" else None
    question = _question(question, language) if feature == "analyze" else None
    history = _read_history(history_path) if feature == "analyze" else []
    plan = plan_prompts(run, feature, transcript_path=transcript_path, question=question,
                        history_path=history_path, limit=limit, language=language, max_steps=max_steps,
                        assistant_only=assistant_only, label_options=label_options,
                        out_path=out_path, _history=history)
    if not isinstance(replies, dict) or replies.get("hash") != plan["hash"]:
        raise BridgeError("stale_prompts", "These replies were made for other prompts (the session changed since "
                                           "they were planned); plan the prompts again.")
    by_index, counter, failed, label_errors = _replies_by_index(replies), itertools.count(), [], []

    def chat(system: str, messages: Any) -> str:
        i = next(counter)
        if feature == "label" and i >= max_steps:
            return PLACEHOLDERS["label"]  # past the cap: never planned, so no reply is expected
        reply = by_index.get(i)
        if reply is not None and isinstance(reply.get("text"), str):
            return reply["text"]
        if feature == "label":
            label_errors.append(f"prompt {i}: {reply['error'] if reply is not None else 'no reply'}")
            return PLACEHOLDERS["label"]
        failed.append(reply["error"] if reply is not None else f"no reply to prompt {i}")
        raise ReplyError(failed[-1])

    out = _run(run, feature, chat=chat, config=None, transcript_path=transcript_path, question=question or "",
               history=history, limit=limit, language=language, out_path=out_path, assistant_only=assistant_only, model=model or SESSION_MODEL,
               label_options=label_options)
    result = _result(feature, out, history_path=history_path, limit=limit, out_path=out_path,
                     label_errors=label_errors, expected_history=history if feature == "analyze" else None)
    if feature == "analyze" and failed:
        result["error"] = failed[0]
    return result


# -- env mode --------------------------------------------------------------------

def _not_configured(missing: list[str]) -> BridgeError:
    return BridgeError("llm_not_configured", "The LLM endpoint is not configured; set " + ", ".join(missing)
                       + " (environment or .env), or use the session's model.", missing=missing)


@contextlib.contextmanager
def _label_pacing(delay: float):
    """Wait only between real provider attempts, never for the sidecar's capped unknown tail."""
    from agent_trajviz.labeling import llm

    original = llm.call_llm
    sent = False

    @functools.wraps(original)
    def call(*args, **kwargs):
        nonlocal sent
        if sent and delay > 0:
            time.sleep(delay)
        sent = True
        return original(*args, **kwargs)

    llm.call_llm = call
    try:
        yield
    finally:
        llm.call_llm = original


def _label_client(config: Any, model: str | None) -> tuple[str, dict]:
    """The labeler's model and endpoint: ``LABEL_*`` first, then the analysis settings."""
    model = model or llm_env_value("LABEL_MODEL") or config.model
    provider = (llm_env_value("LABEL_PROVIDER") or config.provider).lower()
    client = {"base_url": llm_env_value("LABEL_BASE_URL") or config.base_url,
              "api_key": llm_env_value("LABEL_API_KEY") or config.api_key,
              "provider": provider if provider in {"openai", "anthropic"} else "openai"}
    missing = [name for name, value in (("LABEL_BASE_URL", client["base_url"]), ("LABEL_API_KEY", client["api_key"]),
                                        ("LABEL_MODEL", model)) if not value]
    if missing:
        raise _not_configured(missing)
    return model, client


def direct(run: Trajectory, feature: Feature, *, transcript_path: str, question: str | None,
           history_path: Path | None, limit: int, language: str | None, out_path: Path, max_steps: int,
           assistant_only: bool, model: str | None = None, label_options: dict | None = None) -> dict:
    """:func:`apply_replies`'s result, the model called through TrajViz's own client (``ANALYZE_*``/``LABEL_*``).

    Missing settings raise ``BridgeError("llm_not_configured", missing=[names])``. ``label`` sends the first
    *max_steps* steps to the endpoint and labels the rest ``unknown`` without a call, as session mode does; a
    step whose call failed (after the labeler's retry) is ``"prompt <i>: <error class>"`` in ``errors``.
    """
    from agent_trajviz import llm

    _feature(feature)
    config = llm.config_from_env(load_dotenv=True)
    label_options = _label_settings(label_options, env=True) if feature == "label" else None
    if label_options is not None:
        _prepare_label_output(out_path, sources=[Path(transcript_path), Path(label_options["taxonomy_path"])])
    protect_run_outputs(run, [out_path] if feature == "label" else
                        [history_path, _history_lock_path(history_path)]
                        if feature == "analyze" and history_path is not None else [])
    label_model, client = _label_client(config, model) if feature == "label" else (SESSION_MODEL, None)
    if feature != "label" and not config.ready:
        raise _not_configured(list(config.missing))
    question = _question(question, language) if feature == "analyze" else None
    history = _read_history(history_path) if feature == "analyze" else []
    options = {"chat": None, "config": config, "transcript_path": transcript_path, "question": question or "",
               "history": history, "limit": limit, "language": language, "out_path": out_path,
               "assistant_only": assistant_only, "model": label_model, "label_client": client, "label_options": label_options}
    if feature != "label":
        return _result(feature, _run(run, feature, **options), history_path=history_path, limit=limit,
                       out_path=out_path, expected_history=history if feature == "analyze" else None)
    with _label_pacing(label_options["delay"]), labeler_step_cap(max_steps, PLACEHOLDERS["label"]) as label_errors:
        options["label_options"] = {**label_options, "delay": 0.0}
        out = _run(run, feature, **options)
    return _result(feature, out, history_path=history_path, limit=limit, out_path=out_path,
                   label_errors=label_errors)


# -- subcommands -----------------------------------------------------------------

def _fixes_limit(text: str) -> int:
    value = _positive_int(text)
    if value > FIXES_CAP:
        raise argparse.ArgumentTypeError(f"at most {FIXES_CAP} issues are judged, not {value}")
    return value


def _common(p: argparse.ArgumentParser, *, feature: bool) -> None:
    _session_args(p)
    if feature:
        p.add_argument("--feature", required=True, choices=FEATURES, help="The LLM feature.")
    p.add_argument("--language", default=None, help="Reply language (default: ANALYZE_LANGUAGE, else English).")


def _question_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--question", default=None, help="analyze: the question (default: TrajViz's first pass).")


def _limit_arg(p: argparse.ArgumentParser) -> None:
    p.add_argument("--limit", type=_fixes_limit, default=FIXES_CAP,
                   help=f"fixes: issues judged, at most {FIXES_CAP} (default: {FIXES_CAP}).")


def _label_args(p: argparse.ArgumentParser, *, output: bool) -> None:
    p.add_argument("--max-steps", type=_positive_int, default=DEFAULT_MAX_STEPS,
                   help=f"label: steps labelled (default: {DEFAULT_MAX_STEPS}).")
    p.add_argument("--assistant-only", action="store_true", help="label: keep only assistant steps in the sidecar.")
    p.add_argument("--taxonomy", dest="taxonomy_path", type=Path, default=None, help="Custom label taxonomy Markdown.")
    p.add_argument("--max-content-chars", type=_positive_int, default=8000, help="Maximum step prompt characters.")
    p.add_argument("--max-tokens", type=_positive_int, default=None, help="Reply token cap, up to 64000.")
    p.add_argument("--temperature", type=float, default=None, help="Env-mode sampling temperature.")
    p.add_argument("--delay", type=float, default=0.0, help="Seconds between label calls, from 0 to 3600.")
    p.add_argument("--user-phase", default="user", help="Deterministic user-step phase.")
    p.add_argument("--user-action", default="user_prompt", help="Deterministic user-step action.")
    p.add_argument("-o", "--out", type=Path, default=None, help="label: the sidecar (default: <artifacts>/labels.json).")
    if output:
        p.add_argument("--model", default=None, help="label: the model named in the sidecar.")


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("llm-prompt", help="The prompts an LLM feature would send, for the session's model.")
    _common(p, feature=True)
    _question_arg(p)
    _limit_arg(p)
    _label_args(p, output=False)

    p = sub.add_parser("llm-apply", help="TrajViz's result of an LLM feature from the replies to its prompts.")
    _common(p, feature=True)
    p.add_argument("--replies", required=True, type=Path, help='{"hash", "replies": [{"index", "text"|"error"}]}.')
    _question_arg(p)
    _limit_arg(p)
    _label_args(p, output=True)

    p = sub.add_parser("ask", help="TrajViz's trajectory analysis through its own client (ANALYZE_*).")
    _common(p, feature=False)
    _question_arg(p)

    p = sub.add_parser("fixes", help="TrajViz's Issues fix judge through its own client (ANALYZE_*).")
    _common(p, feature=False)
    _limit_arg(p)

    p = sub.add_parser("label", help="Step labeling through TrajViz's own client (LABEL_*).")
    _common(p, feature=False)
    _label_args(p, output=True)


def _options(args: argparse.Namespace, feature: str) -> dict[str, Any]:
    artifacts = _artifacts(args)
    return {"transcript_path": args.transcript, "question": getattr(args, "question", None),
            "history_path": artifacts / "analyze.json" if feature == "analyze" else None,
            "limit": getattr(args, "limit", FIXES_CAP), "language": args.language,
            "max_steps": getattr(args, "max_steps", DEFAULT_MAX_STEPS),
            "assistant_only": getattr(args, "assistant_only", False),
            "label_options": {**{key: getattr(args, key) for key in ("taxonomy_path", "max_content_chars", "delay",
                                                                    "user_phase", "user_action") if hasattr(args, key)},
                              **{key: getattr(args, key) for key in ("max_tokens", "temperature")
                                 if getattr(args, key, None) is not None}}}


def _out(args: argparse.Namespace) -> Path:
    return args.out or _artifacts(args) / "labels.json"


def _read_replies(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise BridgeError("not_found", f"No such replies file: {path}", path=str(path)) from exc
    except (OSError, ValueError) as exc:
        raise BridgeError("bad_replies", f"Cannot read the replies {path}: {exc}", path=str(path)) from exc


def _input_identity(path: str) -> str:
    return os.path.abspath(os.path.expanduser(path))


def _freeze_current(args: argparse.Namespace, run: Trajectory, plan: dict) -> dict:
    """Persist a request's immutable run under its opaque hash before the host adds model/UI records."""
    folder = _artifacts(args) / "plans" / plan["hash"]
    trajectory, manifest = folder / "trajectory.json", folder / "transaction.json"
    protect_run_outputs(run, [trajectory, manifest])
    folder.mkdir(parents=True, exist_ok=True)
    source = {"path": args.transcript, "sha256": run.sha256, "frozen": True}
    _write_json(trajectory, run.to_dict(include_raw=True))
    with trajectory.open("rb") as handle:
        captured_sha256 = hashlib.file_digest(handle, "sha256").hexdigest()
    _write_json(manifest, {"schema_version": 1, "hash": plan["hash"], "feature": args.feature,
                           "transcript": _input_identity(args.transcript), "source": source,
                           "capture_session_id": getattr(args, "session_id", None),
                           "inputs": [str(path.absolute()) for path in input_files(run)],
                           "trajectory_sha256": captured_sha256})
    return source


def _frozen_current(args: argparse.Namespace, replies: dict) -> tuple[Trajectory, dict]:
    """Load only this artifact directory's saved request; reply data never supplies a filesystem path."""
    fingerprint = replies.get("hash") if isinstance(replies, dict) else None
    if not isinstance(fingerprint, str) or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
        raise BridgeError("stale_prompts", "These replies have no valid saved prompt hash; plan the prompts again.")
    folder = _artifacts(args) / "plans" / fingerprint
    try:
        manifest = json.loads((folder / "transaction.json").read_text(encoding="utf-8"))
        valid = (isinstance(manifest, dict) and manifest.get("schema_version") == 1
                 and manifest.get("hash") == fingerprint and manifest.get("feature") == args.feature
                 and manifest.get("transcript") == _input_identity(args.transcript)
                 and manifest.get("capture_session_id") == getattr(args, "session_id", None))
        if not valid:
            raise ValueError("the saved request belongs to a different transcript or feature")
        check_supported()
        load_plugins()
        captured_bytes = (folder / "trajectory.json").read_bytes()
        if manifest.get("trajectory_sha256") != hashlib.sha256(captured_bytes).hexdigest():
            raise ValueError("the saved trajectory contents changed")
        captured = json.loads(captured_bytes)
        # Preserve the caller's original provenance, never an embedded export path or the cache filename.
        if isinstance(captured, dict):
            captured["source"] = args.transcript
        run = Trajectory.from_dict(captured)
        source = manifest.get("source")
        if not isinstance(source, dict) or source.get("sha256") != run.sha256:
            raise ValueError("the saved trajectory identity changed")
        inputs = manifest.get("inputs", [args.transcript])
        if not isinstance(inputs, list) or any(not isinstance(path, str) for path in inputs):
            raise ValueError("the saved input list changed")
        destinations = ([_out(args)] if args.feature == "label" else
                        [_artifacts(args) / "analyze.json", _history_lock_path(_artifacts(args) / "analyze.json")]
                        if args.feature == "analyze" else [])
        # A frozen run intentionally does not reopen live inputs. Keep the original
        # validated companion files protected even after they change or disappear.
        protect_inputs(destinations, map(Path, inputs))
        return run, {"path": args.transcript, "sha256": run.sha256, "frozen": True}
    except (OSError, ValueError, TypeError) as exc:
        raise BridgeError("stale_prompts", f"Cannot read the saved input for these replies ({exc}); "
                          "plan the prompts again.") from exc


@register("llm-prompt")
def _llm_prompt(args: argparse.Namespace) -> dict:
    try:
        run = _load(args)
    except EmptySession as exc:
        return {"empty": True, "reason": exc.reason}
    options = _options(args, args.feature)
    path = _artifacts(args) / f"prompts-{args.feature}.json"
    if args.feature == "label":
        options["label_options"] = _label_settings(options["label_options"])
        _prepare_label_output(path, sources=[Path(args.transcript), Path(options["label_options"]["taxonomy_path"])])
        protect_run_outputs(run, [path])
    plan = plan_prompts(run, args.feature, **options, out_path=_out(args) if args.feature == "label" else None)
    if args.kind == "current":
        plan["source"] = _freeze_current(args, run, plan)
    protect_run_outputs(run, [path])
    path.parent.mkdir(parents=True, exist_ok=True)
    size = _write_json(path, plan)
    out = {**plan, "path": str(path), "bytes": size}
    if size > INLINE_JSON_BYTES or _stdout_bytes(out) > INLINE_JSON_BYTES:
        del out["prompts"]
    return out


@register("llm-apply")
def _llm_apply(args: argparse.Namespace) -> dict:
    replies_path = Path(args.replies)
    replies = _read_replies(replies_path)
    if args.feature == "label":
        _prepare_label_output(_out(args), sources=[replies_path])
    destinations = ([_out(args)] if args.feature == "label" else
                    [_artifacts(args) / "analyze.json", _history_lock_path(_artifacts(args) / "analyze.json")]
                    if args.feature == "analyze" else [])
    protect_inputs(destinations, [replies_path])
    source = None
    if args.kind == "current":
        run, source = _frozen_current(args, replies)
    else:
        try:
            run = _load(args)
        except EmptySession as exc:
            return {"empty": True, "reason": exc.reason}
    result = apply_replies(run, args.feature, replies, **_options(args, args.feature), out_path=_out(args),
                           model=args.model)
    if source is not None:
        result["source"] = source
    return result


def _direct(args: argparse.Namespace, feature: str) -> dict:
    try:
        run = _load(args)
    except EmptySession as exc:
        return {"empty": True, "reason": exc.reason}
    out_path = _out(args) if feature == "label" else _artifacts(args) / "labels.json"
    return direct(run, feature, **_options(args, feature), out_path=out_path, model=getattr(args, "model", None))


@register("ask")
def _ask(args: argparse.Namespace) -> dict:
    return _direct(args, "analyze")


@register("fixes")
def _fixes(args: argparse.Namespace) -> dict:
    return _direct(args, "fixes")


@register("label")
def _label_cmd(args: argparse.Namespace) -> dict:
    return _direct(args, "label")
