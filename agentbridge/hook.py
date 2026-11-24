from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from typing import Any

from .config import DEFAULT_HOST, DEFAULT_PORT

_READ_ONLY_NAMES = re.compile(r"(^|_)(read|view|list|ls|grep|search|find|status|diff|show|inspect|browse)(_|$)", re.I)
_READ_ONLY_COMMANDS = re.compile(
    r"^(?:ls|dir|pwd|cd|cat|type|head|tail|rg|grep|find|where|which|git\s+(?:status|diff|log|show|branch\s+(?:-a|--all)|rev-parse|remote\s+-v))\b",
    re.I,
)


def _get_any(mapping: Any, *names: str) -> Any:
    if not isinstance(mapping, dict):
        return None
    for name in names:
        if name in mapping:
            return mapping[name]
        for key, value in mapping.items():
            if key.casefold() == name.casefold():
                return value
    return None


def _tool_parts(payload: dict[str, Any]) -> tuple[str, dict[str, Any], Any, str]:
    if isinstance(payload.get("toolCall"), dict):  # Antigravity
        call = payload["toolCall"]
        tool_name = str(call.get("name", ""))
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        outcome = payload.get("error")
        return tool_name, args, outcome, "antigravity"
    # Codex command hook input
    tool_name = str(payload.get("tool_name", ""))
    args = payload.get("tool_input") if isinstance(payload.get("tool_input"), dict) else {}
    outcome = payload.get("tool_response")
    return tool_name, args, outcome, "codex"


def _session_key(payload: dict[str, Any]) -> str | None:
    value = payload.get("session_id") or payload.get("conversationId") or payload.get("conversation_id")
    return str(value) if value else None


def _bound_agent_from_tool(payload: dict[str, Any]) -> str | None:
    tool_name, args, outcome, _source = _tool_parts(payload)
    if "agentbridge" not in tool_name.casefold():
        return None
    if isinstance(outcome, dict) and (outcome.get("isError") or outcome.get("error")):
        return None
    if _source == "antigravity" and outcome:
        return None
    if "join_group" not in tool_name.casefold():
        identity = _get_any(args, "agent_id", "agentId")
        return str(identity) if identity else None
    if _source == "antigravity":
        outcome = payload.get("toolResult") or payload.get("toolResponse") or payload.get("tool_result")
    candidates: list[Any] = [outcome]
    if isinstance(outcome, dict):
        candidates.extend([outcome.get("structuredContent"), outcome.get("result")])
        content = outcome.get("content")
        if isinstance(content, list):
            candidates.extend(item.get("text") for item in content if isinstance(item, dict))
    for candidate in candidates:
        if isinstance(candidate, str):
            try:
                candidate = json.loads(candidate)
            except ValueError:
                continue
        if isinstance(candidate, dict):
            value = candidate.get("agent_id") or candidate.get("agentId")
            if value:
                return str(value)
            result = candidate.get("result")
            if isinstance(result, str):
                try:
                    parsed = json.loads(result)
                except ValueError:
                    continue
                if isinstance(parsed, dict) and parsed.get("agent_id"):
                    return str(parsed["agent_id"])
    return None


def _extract_files(args: dict[str, Any]) -> list[str]:
    names = {
        "file", "path", "filepath", "file_path", "filename", "file_name", "target", "target_path",
        "files", "files_touched", "paths", "absolute_path",
    }
    files: list[str] = []
    for key, value in args.items():
        if key.casefold() not in names:
            continue
        if isinstance(value, (list, tuple)):
            files.extend(str(item) for item in value if isinstance(item, (str, int)))
        elif isinstance(value, (str, int)):
            files.append(str(value))
    return list(dict.fromkeys(files))[:6]


def _command(args: dict[str, Any]) -> str:
    value = _get_any(args, "command", "cmd", "commandline", "script", "shell_command")
    if isinstance(value, list):
        return " ".join(map(str, value))
    return str(value or "").strip()


def _safe_command_label(command: str, category: str) -> str:
    lower = command.strip().lower()
    if category == "install":
        match = re.search(r"\b(pip|pip3|npm|pnpm|yarn|uv|cargo|go)\s+(?:install|add|get)\b", lower)
        return f"{match.group(1)} install" if match else "package install"
    if category == "git":
        match = re.search(r"\bgit\s+([a-z-]+)", lower)
        return f"git {match.group(1)}" if match else "git operation"
    for pattern, label in (
        (r"\bpytest\b", "pytest"), (r"\bpython(?:3)?\s+-m\s+unittest\b", "unittest"),
        (r"\b(?:npm|pnpm|yarn)\s+test\b", "package tests"),
        (r"\bcargo\s+test\b", "cargo test"), (r"\bgo\s+test\b", "go test"),
    ):
        if re.search(pattern, lower):
            return label
    return "command executed"


def classify_tool_event(payload: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce a hook payload to a compact write event; never forward tool output or diffs."""
    tool_name, args, outcome, _source = _tool_parts(payload)
    name = tool_name.strip().lower()
    if not name or "agentbridge" in name or "report_step" in name:
        return None

    files = _extract_files(args)
    command = _command(args)
    if _READ_ONLY_NAMES.search(name):
        return None

    if any(token in name for token in ("write", "edit", "patch", "create_file", "delete_file", "move_file", "rename_file")):
        action = "edit"
        label = "file edit"
    elif any(token in name for token in ("read_file", "list_directory", "search", "grep")):
        return None
    elif any(token in name for token in ("run_command", "terminal", "shell", "exec", "bash", "powershell", "command")):
        normalized = re.sub(r"\s+", " ", command).strip()
        if not normalized or _READ_ONLY_COMMANDS.match(normalized):
            return None
        if re.search(r"\b(?:pip|pip3|npm|pnpm|yarn|uv|cargo|go)\s+(?:install|add|get)\b", normalized, re.I):
            action = "install"
        elif re.search(r"\bgit\s+", normalized, re.I):
            action = "git"
        elif re.search(r"\b(?:pytest|unittest|npm\s+test|pnpm\s+test|yarn\s+test|cargo\s+test|go\s+test)\b", normalized, re.I):
            action = "test"
        else:
            action = "command"
        label = _safe_command_label(normalized, action)
    elif "git" in name:
        action = "git"
        label = "git operation"
    else:
        return None

    if isinstance(outcome, dict):
        exit_code = outcome.get("exit_code", outcome.get("exitCode"))
        failed = (exit_code not in (None, 0, "0")) if exit_code is not None else bool(outcome.get("error"))
    else:
        failed = bool(outcome) and str(outcome).strip().lower() not in {"", "none", "null", "false", "0", "{}"}
    result = "failed" if failed else "completed"
    if action == "edit":
        summary = f"{label} {result}"
    else:
        summary = f"{label} {result}"
    return {"action_type": action, "summary": summary[:100], "files_touched": files}


def _request_json(url: str, payload: dict[str, Any], timeout: float = 1.5) -> dict[str, Any]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            decoded = json.loads(response.read().decode("utf-8"))
            return decoded if isinstance(decoded, dict) else {}
    except (OSError, ValueError, urllib.error.URLError, TimeoutError):
        return {}


def _print_codex_context(messages: list[str], event: str = "PostToolUse") -> None:
    if not messages:
        return
    result = {
        "hookSpecificOutput": {
            "hookEventName": event,
            "additionalContext": "\n".join(messages),
        }
    }
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))


def _print_antigravity_injection(messages: list[str]) -> None:
    if not messages:
        sys.stdout.write("{}")
        return
    result = {
        "injectSteps": [
            {"systemMessage": {"systemMessage": "\n".join(messages)}}
        ]
    }
    sys.stdout.write(json.dumps(result, ensure_ascii=False, separators=(",", ":")))

def _identity_context(response: dict[str, Any], session_key: str | None) -> list[str]:
    if not session_key or "member" not in response:
        return []
    if member := response.get("member"):
        return [f"AgentBridge 已綁定 {member['agent_name']}（agent_id: {member['agent_id']}）。沿用此 ID，無須再次報到。"]
    if response.get("recovery_required"):
        return [f"此 AgentBridge 對話尚未綁定員工。換模型仍沿用舊帳號。"
                f"用原 agent_id、私密 member_key 和 session_key={session_key} 呼叫 join_group 恢復。"
                "記不得原身分時請老闆從員工資訊發放金鑰。"]
    return [f"首次在此 AgentBridge 工作階段報到時，請在 join_group 傳入 session_key={session_key}。"
            "若已有員工身分，先用原 agent_id 和 member_key 恢復。"]


def run_hook(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="AgentBridge PostToolUse hook")
    parser.add_argument("--agent", required=True, help="client alias or returned unique agent_id/agent_name")
    parser.add_argument("--event", default="post-tool-use", choices=("post-tool-use", "pre-invocation", "session-start"))
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args(argv)
    base_url = f"http://{args.host}:{args.port}"

    try:
        payload = json.load(sys.stdin)
        if not isinstance(payload, dict):
            payload = {}
    except (ValueError, OSError):
        payload = {}

    session_key = _session_key(payload)
    if args.event == "session-start":
        if session_key:
            response = _request_json(base_url + "/hooks/session-identity", {"client": args.agent, "session_key": session_key})
            _print_codex_context(_identity_context(response, session_key), "SessionStart")
        return 0
    bound_identity = _bound_agent_from_tool(payload)
    if session_key and bound_identity:
        _request_json(base_url + "/hooks/bind-session", {
            "session_key": session_key,
            "agent_id": bound_identity,
            "client": args.agent,
        })

    if args.event == "pre-invocation":
        result = _request_json(
            base_url + "/hooks/pre-invocation", {"agent_id": args.agent, "session_key": session_key}
        )
        messages = result.get("messages") or []
        if args.agent == "Antigravity":
            _print_antigravity_injection(_identity_context(result, session_key) + messages)
        elif messages:
            sys.stdout.write(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "additionalContext": "\n".join(messages),
                }
            }, ensure_ascii=False, separators=(",", ":")))
        return 0

    event = classify_tool_event(payload)
    if event or (session_key and not bound_identity):
        result = _request_json(base_url + "/hooks/post-tool-use", {
            "agent_id": args.agent,
            "session_key": session_key,
            **(event or {}),
        })
        messages = result.get("messages") or []
    else:
        messages = []

    if args.agent == "Antigravity":
        # Antigravity PostToolUse requires an empty object; inbound updates are injected by PreInvocation.
        sys.stdout.write("{}")
    else:
        _print_codex_context(messages)
    return 0
