"""Do not blame the first command for an unlocalized compound-shell failure."""

from __future__ import annotations

import functools

from .errors import BridgeError


def _compound(command: str, nesting: int = 0) -> bool:
    from agent_trajviz.insight import shell_cmd

    if nesting >= 4:
        return True
    segments = shell_cmd._shell_segments(command)
    if len(segments) != 1:
        return True  # Multiple commands, or quoting the lexer cannot safely resolve.
    return _compound_segment(segments[0], nesting)


def _compound_segment(tokens: list[str], nesting: int) -> bool:
    from agent_trajviz.insight import shell_cmd

    index = 0
    for _ in range(12):
        while index < len(tokens) and shell_cmd._is_shell_assignment(tokens[index]):
            index += 1
        if index >= len(tokens):
            return False
        name = shell_cmd._shell_name(tokens[index])
        if name in {"command", "env", "sudo", "time", "nice", "nohup", "xargs"}:
            index = shell_cmd._skip_wrapper_options(tokens, index + 1, name)
            continue
        if name == "timeout" and not shell_cmd._is_windows_timeout_invocation(tokens, index):
            index = shell_cmd._skip_wrapper_options(tokens, index + 1, name) + 1
            continue
        if name == "busybox":
            index += 1
            continue
        if name in {"bash", "dash", "ksh", "sh", "zsh"}:
            index += 1
            while index < len(tokens) and tokens[index].startswith("-"):
                option = tokens[index]
                if option == "--":
                    return False  # Following words name a script, not shell options.
                if option.startswith("--"):
                    index += 2 if option in {"--rcfile", "--init-file"} else 1
                    continue
                if "c" in option[1:] and index + 1 < len(tokens):
                    return _compound(tokens[index + 1], nesting + 1)
                index += 1
            return False
        if name in {"cmd", "cmd.exe"}:
            body = shell_cmd._cmd_script_tokens(tokens, index)
            return (_compound(body[0], nesting + 1) if len(body) == 1 else
                    _compound_segment(body, nesting + 1) if body and nesting < 3 else bool(body))
        if name in shell_cmd._PS_INTERPRETERS:
            payload = shell_cmd._powershell_payload(tokens, index, nesting)
            if payload is not None and payload[0] == "command":
                body = payload[1]
                return (_compound(body, nesting + 1) if isinstance(body, str) else
                        _compound_segment(body, nesting + 1) if nesting < 3 else True)
        return False
    return True


def install() -> None:
    """Install one display-label rule at the engine's shared chart/diagnostic boundary.

    Canonical calls, commands, outcomes and source bytes stay unchanged. The
    failed shell invocation is known; its failing inner command is not.
    """
    from agent_trajviz.insight import chart_data, diagnostics, shell_cmd
    from agent_trajviz.insight.tool_failure import tool_call_failed
    from agent_trajviz.tool_vocab import BASH_TOOL_NAMES

    original = shell_cmd.tool_chart_name
    if getattr(original, "_trajviz_obs_compound_failures", False):
        return
    if any(module.tool_chart_name is not original for module in (chart_data, diagnostics)):
        raise BridgeError("unsupported_trajviz", "TrajViz shell display labels changed; update the plugin.")

    @functools.wraps(original)
    def label(call):
        name = call.get("tool_name")
        if name in BASH_TOOL_NAMES and tool_call_failed(call):
            inputs = call.get("input")
            command = inputs.get("command") if isinstance(inputs, dict) else None
            if not isinstance(command, str) or not command.strip():
                command = call.get("title")
            if isinstance(command, str) and command.strip() and _compound(command):
                return name
        return original(call)

    label._trajviz_obs_compound_failures = True
    for module in (shell_cmd, chart_data, diagnostics):
        module.tool_chart_name = label
