"""Bundle a native session and its subagents for upload into a TrajViz dashboard.

Preserve the parent transcript and every file under ``<session-id>/subagents/`` in the native directory
layout. Output is replaced atomically and must not overwrite any session input.
"""

from __future__ import annotations

import argparse
import json
import shutil
import zipfile
from pathlib import Path

from .cli import register
from .compat import input_files, load_run, protect_run_outputs, resolve_input_path
from .errors import BridgeError
from .outputs import protect_inputs, session_files, staged_path
from .sessions import capture_session_id
from .views_cmd import _artifacts

#: Most member names ``bundle`` lists on stdout.
MEMBERS_LISTED = 200


def _file_dest(dest: Path) -> Path:
    """*dest* with its folder created; an existing folder at *dest* is refused (an explicit file is wanted)."""
    if dest.is_dir():
        raise BridgeError("bad_path", f"{dest} is a folder; give a file path.", path=str(dest))
    dest.parent.mkdir(parents=True, exist_ok=True)
    return dest


def _session_id(transcript: Path) -> str | None:
    """A native record's session id; session_files validates it as one safe path component."""
    with transcript.open("rb") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except (ValueError, UnicodeError, RecursionError):
                continue
            if (isinstance(record, dict) and record.get("type") in {"user", "assistant", "system", "progress", "summary"}
                    and isinstance(record.get("sessionId"), str) and record["sessionId"]):
                return record["sessionId"]
    return None


def _result(dest: Path, members: list[str], *, note: str | None = None) -> dict:
    return {"path": str(dest), "bytes": dest.stat().st_size, "members": members[:MEMBERS_LISTED],
            "members_total": len(members), **({"note": note} if note else {})}


def bundle_session(transcript_path: str, dest: Path, *, session_id: str | None = None) -> dict:
    """The transcript and every file under ``<id>/subagents/`` as a ZIP at *dest*: ``{"path", "bytes", "members",
    "members_total"}``.

    Members are stored as ``<id>.jsonl`` and ``<id>/subagents/…`` (relative to the transcript's folder);
    ``members`` names the first :data:`MEMBERS_LISTED` of them (the ZIP holds them all, ``members_total`` counts
    them), so the answer does not grow with the session.
    """
    transcript = resolve_input_path(transcript_path).resolve()
    if not transcript.exists():
        raise BridgeError("not_found", f"No such transcript: {transcript}", path=str(transcript))
    from agent_trajviz.insight.formats.otlp_files import looks_like_otlp_source

    if looks_like_otlp_source(str(transcript)):
        import agent_trajviz as tv

        choices = tv.list_sessions(transcript)
        selected = session_id or (choices[0]["id"] if choices else None)
        run = load_run(str(transcript), session_id=selected)
        files = [path for path in input_files(run) if path.is_file()]
        protect_run_outputs(run, [dest])
        note = ("This bundle preserves the complete saved capture, including other captured sessions. "
                "Use /obs json --with-trajectory for a selected-session export.")
        if zipfile.is_zipfile(transcript):
            with zipfile.ZipFile(transcript) as archive:
                members = archive.namelist()
            dest = _file_dest(dest)
            with staged_path(dest) as temporary:
                shutil.copyfile(transcript, temporary)
            return _result(dest, members, note=note)
        base = transcript if transcript.is_dir() else transcript.parent
        members = [path.relative_to(base).as_posix() for path in files]
        manifest = run.raw.get("_otel", {}).get("source", {}).get("manifest")
        generated = None
        if isinstance(manifest, dict) and manifest and transcript.is_file() and transcript.name not in {
                "manifest.json", "otlp-manifest.json"}:
            # Selected manifests may have any basename, while the ZIP reader
            # requires a canonical one. Rebase members to avoid name collisions.
            files = [path for path in files if path != transcript]
            members = [f"data/{index:03d}.jsonl" for index in range(len(files))]
            renamed = dict(zip(files, members, strict=True))
            generated = {**manifest, **{key: [renamed[base / name] for name in manifest.get(key, [])]
                                      for key in ("traces", "logs")}}
        elif not isinstance(manifest, dict) or not manifest:
            # A raw JSON/JSONL request needs an explicit bundle manifest. Keep
            # its original bytes: the file can contain both traces and logs.
            name = "capture" + (".jsonl" if transcript.suffix.lower() == ".jsonl" else ".json")
            members = [name]
            files = [transcript]
            generated = {"type": "agent-trajviz-otlp-bundle", "schema_version": 1,
                         "profile": "claude_code", "traces": [name], "logs": []}
        dest = _file_dest(dest)
        with staged_path(dest) as temporary, zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for path, name in zip(files, members, strict=True):
                archive.write(path, name)
            if generated is not None:
                archive.writestr("manifest.json", json.dumps(generated))
                members.append("manifest.json")
        return _result(dest, members, note=note)
    if session_id is not None:
        # The public loader rejects captured-session selection for native formats.
        load_run(str(transcript), session_id=session_id)
    if transcript.is_file() and zipfile.is_zipfile(transcript):
        protect_inputs([dest], [transcript])
        with zipfile.ZipFile(transcript) as archive:
            members = archive.namelist()
        dest = _file_dest(dest)
        with staged_path(dest) as temporary:
            shutil.copyfile(transcript, temporary)
        return _result(dest, members)
    native_id = (_session_id(transcript) if transcript.is_file()
                 and transcript.suffix.lower() not in {".zst", ".zstd"} else None)
    run = None
    native_json = False
    if native_id is None:
        run = load_run(str(transcript))
        if run.format == "opencode" and transcript.is_file() and transcript.suffix.lower() == ".json":
            from .opencode import is_native

            try:
                with transcript.open(encoding="utf-8-sig") as handle:
                    native_json = is_native(json.load(handle))
            except (OSError, ValueError):
                pass
    if run is not None and not native_json:
        # Session trees and non-Claude JSONL need their public normalized export
        # to remain portable, including verified children and the active branch.
        protect_run_outputs(run, [dest])
        dest = _file_dest(dest)
        payload = json.dumps(run.to_dict(include_raw=True), ensure_ascii=False, allow_nan=False)
        with staged_path(dest) as temporary, zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("trajectory.json", payload)
        return _result(dest, ["trajectory.json"], note="Open this ZIP in /obs dashboard; for another TrajViz dashboard, "
                       "extract and upload trajectory.json. Original session files are unchanged.")
    files = session_files(transcript, session_id=native_id)
    members = [path.relative_to(transcript.parent).as_posix() for path in files]
    protect_inputs([dest], files)
    dest = _file_dest(dest)
    with staged_path(dest) as tmp, zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path, member in zip(files, members, strict=True):
            archive.write(path, member)
    return _result(dest, members)


def add_parser(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser("bundle", help="The selected session and its companion inputs as a portable ZIP.")
    p.add_argument("--transcript", required=True, help="The session transcript.")
    p.add_argument("--session-id", type=capture_session_id, default=None,
                   help="Validate this session inside a saved OTLP capture; the bundle preserves the full capture.")
    p.add_argument("--artifacts", type=Path, default=None,
                   help="The session's artifact folder (default: <cache>/trajviz-obs/sessions/<session-id>).")
    p.add_argument("--out", type=Path, default=None, help="The file (default: <artifacts>/bundle.zip).")


@register("bundle")
def _bundle(args: argparse.Namespace) -> dict:
    return bundle_session(args.transcript, args.out or _artifacts(args) / "bundle.zip", session_id=args.session_id)
