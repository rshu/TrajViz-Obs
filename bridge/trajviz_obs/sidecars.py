"""What the bridge reads beside TrajViz: the session title, the real human prompts and sub-agent names.

Both scans read only the transcript's own records and the known layout
``<stem>/subagents/**/agent-<id>.meta.json``; neither follows a path found inside
transcript contents, and neither follows a symlink below the session folder.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from heapq import nsmallest
from itertools import chain
from pathlib import Path

from .native_records import command_wrapper, local_caveat_id, local_command_parent, text_content

#: Longest prompt text kept, in characters.
PROMPT_CHARS = 200

#: Deepest folder below ``<stem>/subagents`` searched for ``.meta.json`` files.
MAX_DEPTH = 16

_META_PREFIX, _META_SUFFIX = "agent-", ".meta.json"


@dataclass(frozen=True)
class TranscriptFacts:
    """The session title, the first real human prompts, how many there are, and how many ``isMeta`` rows."""

    title: str | None
    prompts: list[str]
    prompt_count: int
    meta_count: int


@dataclass(frozen=True)
class AgentMeta:
    """A sub-agent's ``description`` and ``agentType`` from its ``agent-<id>.meta.json``."""

    agent_id: str
    description: str
    agent_type: str


def _title(record: dict) -> str | None:
    for key in ("aiTitle", "title"):
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def scan_transcript(path: str, *, max_prompts: int = 20) -> TranscriptFacts:
    """Stream *path* once for its last ``ai-title`` and its real human prompts.

    A prompt is a ``user`` record without ``isMeta: true`` and with nonempty text that is not a known
    host command/reminder/notification envelope. Literal XML prompts remain human input. Tool results
    carry no text and never count.
    The first *max_prompts* texts are kept, clipped to :data:`PROMPT_CHARS`; malformed lines are skipped.
    """
    title: str | None = None
    first_prompts: list[tuple[int, str]] = []
    local_caveats: set[str] = set()
    local_commands: set[str] = set()
    pending: dict[str, list[tuple[int, str, str | None]]] = {}
    prompt_count = meta_count = 0
    with open(path, "rb") as handle:
        for position, line in enumerate(handle):
            if b"user" not in line and b"ai-title" not in line and b"local_command" not in line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            if (command := local_command_parent(record)) is not None:
                local_commands.add(command)
                pending.pop(command, None)
            kind = record.get("type")
            if kind == "ai-title":
                title = _title(record) or title
            elif kind == "user":
                message = record.get("message")
                text = text_content(message.get("content")).strip() if isinstance(message, dict) else ""
                if record.get("isMeta") is True:
                    meta_count += 1
                    if (caveat := local_caveat_id(record)) is not None:
                        local_caveats.add(caveat)
                    continue
                parent = record.get("parentUuid") if isinstance(record.get("parentUuid"), str) else None
                uuid = record.get("uuid")
                if command_wrapper(text) and isinstance(uuid, str) and uuid:
                    if uuid not in local_commands and parent not in local_caveats:
                        # A later output may confirm it. Without proof it remains literal user XML.
                        pending.setdefault(uuid, []).append((position, text[:PROMPT_CHARS], parent))
                    continue
                generated = text.startswith(("<system-reminder>", "<task-notification>"))
                if text and not generated:
                    prompt_count += 1
                    if len(first_prompts) < max_prompts:
                        first_prompts.append((position, text[:PROMPT_CHARS]))
    def unresolved():
        return ((position, text) for rows in pending.values() for position, text, parent in rows
                if parent not in local_caveats)

    prompt_count += sum(1 for _ in unresolved())
    prompts = [text for _, text in nsmallest(max(0, max_prompts), chain(first_prompts, unresolved()))]
    return TranscriptFacts(title=title, prompts=prompts, prompt_count=prompt_count, meta_count=meta_count)


def _read_meta(path: str, agent_id: str) -> AgentMeta | None:
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    description, agent_type = data.get("description"), data.get("agentType")
    description = description if isinstance(description, str) else ""
    agent_type = agent_type if isinstance(agent_type, str) else ""
    if not (description or agent_type):
        return None
    return AgentMeta(agent_id=agent_id, description=description, agent_type=agent_type)


def _walk(folder: str, depth: int, found: dict[str, AgentMeta]) -> None:
    try:
        entries = sorted(os.scandir(folder), key=lambda e: e.name)
    except OSError:
        return
    for entry in entries:
        if entry.is_symlink():
            continue
        if entry.is_dir(follow_symlinks=False):
            if depth < MAX_DEPTH:
                _walk(entry.path, depth + 1, found)
        elif (entry.name.startswith(_META_PREFIX) and entry.name.endswith(_META_SUFFIX)
              and entry.is_file(follow_symlinks=False)):
            agent_id = entry.name[len(_META_PREFIX):-len(_META_SUFFIX)]
            if agent_id and (meta := _read_meta(entry.path, agent_id)):
                found[agent_id] = meta


def agent_meta(transcript_path: str) -> dict[str, AgentMeta]:
    """Sub-agent id -> :class:`AgentMeta`, from every ``agent-<id>.meta.json`` under ``<stem>/subagents``.

    Folders up to :data:`MAX_DEPTH` levels below ``subagents`` are searched; symlinks and malformed
    files are skipped. A transcript without a session folder has no entries.
    """
    transcript = Path(transcript_path)
    session = transcript.with_suffix("")
    root = session / "subagents"
    if session.is_symlink() or root.is_symlink() or not root.is_dir():
        return {}
    found: dict[str, AgentMeta] = {}
    _walk(str(root), 0, found)
    return found
