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
import hashlib
import itertools
import json
import os
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, get_args

from agent_trajviz import Trajectory

from .cli import register
from .compare_cmd import label_views_payload
from .compat import (
    EmptySession,
    check_supported,
    labeler_chat,
    labeler_run,
    labeler_step_cap,
    load_plugins,
    protect_run_outputs,
    window_info,
)
from .errors import BridgeError
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


def _hash(run: Trajectory, feature: str, question: str | None, limit: int, prompts: list[dict]) -> str:
    """Bind replies to the exact requests, including history, language, call limits and the real window.

    The transcript hash alone is insufficient: history can change without another transcript record, and
    overrides or a language change can produce different requests from the same session bytes.
    """
    payload = json.dumps([run.sha256, window_info(run)["window"], feature, question, limit, prompts],
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
    return history


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


def _label(transcript_path: str, out_path: Path, *, assistant_only: bool, model: str, **client: Any) -> dict:
    """The labeler over *transcript_path*, its sidecar written to *out_path*."""
    from agent_trajviz.labeling.labeler import OutputSafetyError, label_trajectory

    out_path.parent.mkdir(parents=True, exist_ok=True)
    settings = {"base_url": "", "api_key": "", "max_tokens": LABEL_MAX_TOKENS, **client}
    try:
        return label_trajectory(transcript_path, str(out_path), model=model, assistant_only=assistant_only,
                                **settings)
    except OutputSafetyError as exc:
        raise BridgeError("bad_output", str(exc), path=str(out_path)) from exc


def _run(run: Trajectory, feature: str, *, chat: Chat | None, config: Any, transcript_path: str, question: str,
         history: list[dict], limit: int, language: str | None, out_path: Path, assistant_only: bool, model: str,
         label_client: dict | None = None) -> Any:
    """One pass of *feature*: through *chat* (a capture or replay), else TrajViz's client with *config*."""
    from agent_trajviz import llm

    if feature == "analyze":
        return llm.ask(run, question, history, config=config, chat=chat, language=language)
    if feature == "fixes":
        return llm.suggest_fixes(run, config=config, chat=chat, language=language, limit=limit)
    with labeler_run(run):
        if chat is None:
            return _label(transcript_path, out_path, assistant_only=assistant_only, model=model, **(label_client or {}))
        with labeler_chat(chat):
            return _label(transcript_path, out_path, assistant_only=assistant_only, model=model)


def _capture(run: Trajectory, feature: str, *, transcript_path: str, question: str, history: list[dict],
             limit: int, language: str | None) -> list[tuple[str, str]]:
    """Every ``(system, prompt)`` *feature* would send, in call order."""
    calls: list[tuple[str, str]] = []
    placeholder = PLACEHOLDERS[feature]

    def chat(system: str, messages: list[dict[str, str]] | str) -> str:
        calls.append((system, messages if isinstance(messages, str) else _render(messages)))
        return placeholder

    with tempfile.TemporaryDirectory(prefix="trajviz-obs-") as scratch:
        _run(run, feature, chat=chat, config=None, transcript_path=transcript_path, question=question,
             history=history, limit=limit, language=language, out_path=Path(scratch) / "labels.json",
             assistant_only=False, model=SESSION_MODEL)
    return calls


def _result(feature: str, out: Any, *, history_path: Path | None, limit: int, out_path: Path,
            label_errors: list[str] | None = None) -> dict:
    if feature == "analyze":
        if history_path is not None:
            history_path.parent.mkdir(parents=True, exist_ok=True)
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
                 assistant_only: bool) -> dict:
    """The prompts *feature* would send: ``{"feature", "hash", "plan": {"calls", "approx_input_tokens"},
    "prompts": [{"index", "system", "prompt", "max_tokens", "timeout_s"}]}``.

    ``analyze`` asks *question* (TrajViz's first-pass question when none) after the exchange kept in
    *history_path*; ``fixes`` judges the first *limit* issues; ``label`` takes the first *max_steps* steps
    the labeler sends (*assistant_only* changes only which records the sidecar keeps).
    """
    _feature(feature)
    question = _question(question, language) if feature == "analyze" else None
    calls = _capture(run, feature, transcript_path=transcript_path, question=question or "",
                     history=_read_history(history_path) if feature == "analyze" else [], limit=limit,
                     language=language)
    if feature == "label":
        calls = calls[:max_steps]
    max_tokens, timeout_s = _limits(feature)
    prompts = [{"index": i, "system": system, "prompt": prompt, "max_tokens": max_tokens, "timeout_s": timeout_s}
               for i, (system, prompt) in enumerate(calls)]
    chars = sum(len(system) + len(prompt) for system, prompt in calls)
    return {"feature": feature, "hash": _hash(run, feature, question, limit, prompts),
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
                  assistant_only: bool, model: str | None = None) -> dict:
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
    protect_run_outputs(run, [out_path] if feature == "label" else
                        [history_path] if feature == "analyze" and history_path is not None else [])
    question = _question(question, language) if feature == "analyze" else None
    plan = plan_prompts(run, feature, transcript_path=transcript_path, question=question,
                        history_path=history_path, limit=limit, language=language, max_steps=max_steps,
                        assistant_only=assistant_only)
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
               history=_read_history(history_path) if feature == "analyze" else [], limit=limit,
               language=language, out_path=out_path, assistant_only=assistant_only, model=model or SESSION_MODEL)
    result = _result(feature, out, history_path=history_path, limit=limit, out_path=out_path,
                     label_errors=label_errors)
    if feature == "analyze" and failed:
        result["error"] = failed[0]
    return result


# -- env mode --------------------------------------------------------------------

def _not_configured(missing: list[str]) -> BridgeError:
    return BridgeError("llm_not_configured", "The LLM endpoint is not configured; set " + ", ".join(missing)
                       + " (environment or .env), or use the session's model.", missing=missing)


def _label_client(config: Any, model: str | None) -> tuple[str, dict]:
    """The labeler's model and endpoint: ``LABEL_*`` first, then the analysis settings."""
    env = os.environ
    model = model or env.get("LABEL_MODEL") or config.model
    client = {"base_url": env.get("LABEL_BASE_URL") or config.base_url,
              "api_key": env.get("LABEL_API_KEY") or config.api_key,
              "provider": env.get("LABEL_PROVIDER") or config.provider}
    missing = [name for name, value in (("LABEL_BASE_URL", client["base_url"]), ("LABEL_API_KEY", client["api_key"]),
                                        ("LABEL_MODEL", model)) if not value]
    if missing:
        raise _not_configured(missing)
    return model, client


def direct(run: Trajectory, feature: Feature, *, transcript_path: str, question: str | None,
           history_path: Path | None, limit: int, language: str | None, out_path: Path, max_steps: int,
           assistant_only: bool, model: str | None = None) -> dict:
    """:func:`apply_replies`'s result, the model called through TrajViz's own client (``ANALYZE_*``/``LABEL_*``).

    Missing settings raise ``BridgeError("llm_not_configured", missing=[names])``. ``label`` sends the first
    *max_steps* steps to the endpoint and labels the rest ``unknown`` without a call, as session mode does; a
    step whose call failed (after the labeler's retry) is ``"prompt <i>: <error class>"`` in ``errors``.
    """
    from agent_trajviz import llm

    _feature(feature)
    config = llm.config_from_env(load_dotenv=True)
    protect_run_outputs(run, [out_path] if feature == "label" else
                        [history_path] if feature == "analyze" and history_path is not None else [])
    label_model, client = _label_client(config, model) if feature == "label" else (SESSION_MODEL, None)
    if feature != "label" and not config.ready:
        raise _not_configured(list(config.missing))
    question = _question(question, language) if feature == "analyze" else None
    history = _read_history(history_path) if feature == "analyze" else []
    options = {"chat": None, "config": config, "transcript_path": transcript_path, "question": question or "",
               "history": history, "limit": limit, "language": language, "out_path": out_path,
               "assistant_only": assistant_only, "model": label_model, "label_client": client}
    if feature != "label":
        return _result(feature, _run(run, feature, **options), history_path=history_path, limit=limit,
                       out_path=out_path)
    with labeler_step_cap(max_steps, PLACEHOLDERS["label"]) as label_errors:
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
    if output:
        p.add_argument("-o", "--out", type=Path, default=None, help="label: the sidecar (default: <artifacts>/labels.json).")
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
            "assistant_only": getattr(args, "assistant_only", False)}


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
    _write_json(manifest, {"schema_version": 1, "hash": plan["hash"], "feature": args.feature,
                           "transcript": _input_identity(args.transcript), "source": source})
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
                 and manifest.get("transcript") == _input_identity(args.transcript))
        if not valid:
            raise ValueError("the saved request belongs to a different transcript or feature")
        check_supported()
        load_plugins()
        captured = json.loads((folder / "trajectory.json").read_text(encoding="utf-8"))
        # Preserve the caller's original provenance, never an embedded export path or the cache filename.
        if isinstance(captured, dict):
            captured["source"] = args.transcript
        run = Trajectory.from_dict(captured)
        source = manifest.get("source")
        if not isinstance(source, dict) or source.get("sha256") != run.sha256:
            raise ValueError("the saved trajectory identity changed")
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
    plan = plan_prompts(run, args.feature, **_options(args, args.feature))
    if args.kind == "current":
        plan["source"] = _freeze_current(args, run, plan)
    path = _artifacts(args) / f"prompts-{args.feature}.json"
    protect_run_outputs(run, [path])
    path.parent.mkdir(parents=True, exist_ok=True)
    size = _write_json(path, plan)
    out = {**plan, "path": str(path), "bytes": size}
    if size > INLINE_JSON_BYTES or _stdout_bytes(out) > INLINE_JSON_BYTES:
        del out["prompts"]
    return out


@register("llm-apply")
def _llm_apply(args: argparse.Namespace) -> dict:
    replies = _read_replies(args.replies)
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
