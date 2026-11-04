"""Bounded native command text for TrajViz's reference-free inspection report."""
from __future__ import annotations

from .compat import attribution_message

TEXT_LIMIT = 10_000
ROW_LIMIT = 5


def _line(value, limit=240):
    text = " ".join(str(value if value is not None else "unknown").split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _count(value):
    return f"{value:,}" if type(value) is int and value >= 0 else "unknown"


def _steps(row):
    indices = row.get("step_indices")
    if not isinstance(indices, list):
        indices = [row.get("workflow_index")]
    known = [index for index in indices if type(index) is int and index >= 0]
    return ("step " if len(known) == 1 else "steps ") + ", ".join(f"#{index}" for index in known[:8]) if known else "step unknown"


def inspection_text(report):
    """Summarize recorded observations; retain the complete report beside the text."""
    summary = report.get("summary") or {}
    lines = ["Attribution · trajectory inspection",
             (f"Recorded: {_count(summary.get('steps'))} steps · {_count(summary.get('tool_calls'))} tool calls · "
              f"{_count(summary.get('threads'))} threads"),
             (f"Evidence: failed calls {_count(summary.get('failed_calls'))} · "
              f"missing results {_count(summary.get('missing_results'))} · "
              f"files {_count(summary.get('files'))} · test runs {_count(summary.get('test_runs'))}")]
    if report.get("reason"):
        lines.append(_line(attribution_message(report["reason"])))

    def rows(key, title, render):
        records = report.get(key) or []
        lines.extend(["", title + ":"])
        if not records:
            lines.append("No observations recorded.")
            return
        for row in records[:ROW_LIMIT]:
            lines.extend(render(row))
        if len(records) > ROW_LIMIT:
            lines.append(f"{len(records) - ROW_LIMIT:,} more observations; /obs dashboard opens the full inspection.")

    rows("findings", "Findings", lambda row: [
        f"- {_line(attribution_message(row.get('title')))} · {_steps(row)}",
        "  " + _line(attribution_message(row.get("observation"))),
    ])
    rows("failures", "Recorded tool failures", lambda row: [
        f"- {_steps(row)} · {_line(row.get('tool_name'))} · status {_line(row.get('status'))} · exit {_line(row.get('exit_code'))}",
        "  " + _line(row.get("command") or row.get("output_excerpt")),
    ])
    rows("files", "Files", lambda row: [
        f"- {_line(row.get('path'))} · {_line(', '.join(row.get('actions') or []))} · {_steps(row)}",
    ])

    def test(row):
        counts = row.get("counts")
        measured = ([f"{key}={value}" for key, value in counts.items() if type(value) is int and value >= 0]
                    if isinstance(counts, dict) else [])
        result = [f"- {_steps(row)} · {_line(row.get('runner'))} · {_line(str(row.get('status') or 'unknown').replace('_', ' '))} · "
                  + (", ".join(measured) if measured else "counts unknown"),
                  "  process: " + _line(str(row.get("process_status") or "unknown").replace("_", " ")),
                  "  command: " + _line(row.get("command"))]
        if row.get("output_excerpt"):
            result.append("  output: " + _line(str(row["output_excerpt"])[-400:], 400))
        return result

    rows("tests", "Tests", test)
    rows("recovery", "Recovery observations", lambda row: [
        f"- {_steps(row)} · {_line(str(row.get('lifecycle') or 'unknown').replace('_', ' '))}",
        "  " + _line(row.get("command") or ", ".join(row.get("files") or [])),
    ])
    footer = ["", "Recording coverage:"]
    for key, value in (report.get("coverage") or {}).items():
        if isinstance(value, dict):
            footer.append(f"- {key.replace('_', ' ')}: {_count(value.get('observed'))}/{_count(value.get('total'))} observed")
    complete = (report.get("coverage") or {}).get("capture_complete")
    footer.append("- Capture completeness: " + ("unknown" if complete is None else "complete" if complete is True else "incomplete"))
    outcome = report.get("outcome") or {}
    footer.extend(["", f"Task outcome: {_line(outcome.get('status'))} — {_line(outcome.get('basis'))}", "Limits:"])
    footer.extend("- " + _line(attribution_message(note)) for note in report.get("limitations") or [])
    footer.extend(["Full inspection: /obs dashboard → Attribution workspace.",
                  "Reference-based attribution: /obs attribution --reference-root DIR --agent AGENT --instance TASK"])
    body, tail = "\n".join(lines), "\n".join(footer)
    if len(body) + len(tail) + 1 > TEXT_LIMIT:
        marker = "\n… Text preview clipped; /obs dashboard opens the full inspection."
        body = body[:max(0, TEXT_LIMIT - len(tail) - len(marker) - 1)] + marker
    return body + "\n" + tail


def inspect_session(run):
    from agent_trajviz.awe import inspect_trajectory

    report = inspect_trajectory(run)
    return {**report, "text": inspection_text(report)}
