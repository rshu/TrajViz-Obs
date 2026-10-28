"""Keep Claude Code's local-command display records out of its work trajectory."""

from __future__ import annotations

from typing import Any

_LOCAL_PREFIXES = ("<local-command-caveat>", "<local-command-stdout>", "<local-command-stderr>")
_OBS_PREFIXES = ("TrajViz artifacts for this session go under ", "The whole TrajViz view ")


def text_content(content: Any) -> str:
    """A message's string content, or its text blocks joined by newlines."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "\n".join(block["text"] for block in content if isinstance(block, dict)
                     and block.get("type") == "text" and isinstance(block.get("text"), str))


def command_wrapper(text: str) -> bool:
    """The host's name/message envelope, not arbitrary XML-like user text."""
    return (text.lstrip().startswith("<command-name>") and "</command-name>" in text
            and "<command-message>" in text and "</command-message>" in text)


def local_command_parent(record: dict) -> str | None:
    """The command-entry UUID confirmed by a host local-output record."""
    parent = record.get("parentUuid")
    return parent if (record.get("type") == "system" and record.get("subtype") == "local_command"
                      and isinstance(parent, str) and parent) else None


def local_caveat_id(record: dict) -> str | None:
    """The UUID of a host caveat that can confirm the next command before output exists."""
    message, uuid = record.get("message"), record.get("uuid")
    if (record.get("type") == "user" and record.get("isMeta") is True and isinstance(uuid, str) and uuid
            and isinstance(message, dict) and message.get("role", "user") == "user"
            and text_content(message.get("content")).lstrip().startswith("<local-command-caveat>")):
        return uuid
    return None


def local_command_text(text: str, *, is_meta: bool, confirmed_command: bool = False) -> bool:
    """Only known host UI envelopes and this plugin's generated artifact notes."""
    text = text.lstrip()
    return ((is_meta and text.startswith((*_LOCAL_PREFIXES, *_OBS_PREFIXES)))
            or (confirmed_command and command_wrapper(text)))


def normalize_native_records(events: list, *, _depth: int = 0, _inherited_meta: bool = False) -> tuple[list, int]:
    """Neutralize display-only text without removing source positions or tool results.

    Empty user messages are already skipped by TrajViz's native converter. Retaining their records
    preserves original raw indices, timestamps, parent links and the loader's byte-based identity.
    The returned count covers changed user records, including inline child-progress messages.
    """
    confirmed = {value for r in events if isinstance(r, dict) and (value := local_command_parent(r)) is not None}
    caveats = {value for r in events if isinstance(r, dict) and (value := local_caveat_id(r)) is not None}
    # The command being inspected may not have written its stdout record yet.
    confirmed.update(r["uuid"] for r in events if isinstance(r, dict) and isinstance(r.get("uuid"), str)
                     and isinstance(r.get("parentUuid"), str) and r["parentUuid"] in caveats)
    normalized, count = [], 0
    for original in events:
        if not isinstance(original, dict):
            normalized.append(original)
            continue
        record = original
        message = record.get("message")
        is_meta = record.get("isMeta") is True or (record.get("isMeta") is not False and _inherited_meta)
        if (record.get("type") == "user" and isinstance(message, dict)
                and message.get("role", "user") == "user"):
            content = message.get("content")
            blocks = [{"type": "text", "text": content}] if isinstance(content, str) else content
            if isinstance(blocks, list):
                kept = [block for block in blocks if not (
                    isinstance(block, dict) and block.get("type") == "text"
                    and isinstance(block.get("text"), str) and local_command_text(
                        block["text"], is_meta=is_meta,
                        confirmed_command=isinstance(record.get("uuid"), str) and record["uuid"] in confirmed))]
                if len(kept) != len(blocks):
                    record = {**record, "message": {**message, "content": kept}}
                    count += 1
        data = record.get("data")
        nested = data.get("message") if isinstance(data, dict) else None
        if record.get("type") == "progress" and isinstance(nested, dict) and _depth < 32:
            bare = nested.get("role") in ("user", "assistant") and "message" not in nested
            wrapped = {"type": nested["role"], "message": nested,
                       "isMeta": nested.get("isMeta")} if bare else nested
            children, ignored = normalize_native_records(
                [wrapped], _depth=_depth + 1, _inherited_meta=is_meta or data.get("isMeta") is True)
            if ignored:
                cleaned = children[0]["message"] if bare else children[0]
                record = {**record, "data": {**data, "message": cleaned}}
                count += ignored
        normalized.append(record)
    return normalized, count
