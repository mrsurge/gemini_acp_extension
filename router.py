"""Gemini ACP routing helpers."""

from __future__ import annotations

import difflib
import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from extensions.tool_card_contracts import build_tool_card_request, build_tool_card_response

_TOOL_STATE_KEYS = (
    "toolCallId",
    "kind",
    "title",
    "status",
    "content",
    "locations",
    "rawInput",
    "rawOutput",
)
_TERMINAL_TOOL_STATUSES = {"completed", "failed"}
_SUPPRESSED_TOOL_KINDS = {"think", "switch_mode"}


def utc_ts() -> str:
    return datetime.now(timezone.utc).isoformat()


def _build_message_event(
    *,
    conversation_id: str,
    turn_id: str,
    message_id: str,
    role: str,
    text: str,
) -> Dict[str, Any]:
    return {
        "type": "message",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "id": message_id,
        "role": role,
        "text": text,
    }


def _build_message_transcript_entry(
    *,
    turn_id: str,
    message_id: str,
    role: str,
    text: str,
) -> Dict[str, Any]:
    return {
        "role": role,
        "id": message_id,
        "item_id": message_id,
        "text": text,
        "timestamp": utc_ts(),
        "turn_id": turn_id,
    }


def _session_update_kind(payload: Dict[str, Any]) -> str:
    raw = payload.get("sessionUpdate")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    raw = payload.get("session_update")
    if isinstance(raw, str) and raw.strip():
        return raw.strip()
    return ""


def _string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _int_value(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _object_dict(value: Any) -> Dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _list_of_dicts(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, dict)]


def _content_text(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    content_type = content.get("type")
    if content_type == "text":
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return ""


def _tool_content_text(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    content_type = _string_value(content.get("type"))
    if content_type == "content":
        nested = content.get("content")
        if isinstance(nested, dict):
            nested_type = _string_value(nested.get("type"))
            if nested_type == "text":
                text = nested.get("text")
                return text if isinstance(text, str) else ""
            text = nested.get("text")
            return text if isinstance(text, str) else ""
        text = content.get("text")
        return text if isinstance(text, str) else ""
    if content_type == "text":
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return ""


def _tool_contents_text(contents: Any) -> str:
    parts: List[str] = []
    for item in _list_of_dicts(contents):
        text = _tool_content_text(item)
        if text:
            parts.append(text)
    return "\n".join(part for part in parts if part)


def _message_role_for_update(kind: str) -> Optional[str]:
    normalized = kind.strip()
    if normalized == "user_message_chunk":
        return "user"
    if normalized == "agent_message_chunk":
        return "assistant"
    if normalized == "agent_thought_chunk":
        return "reasoning"
    return None


def _collect_message_updates(updates: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    messages: List[Dict[str, str]] = []
    current_kind: Optional[str] = None
    current_message_id: Optional[str] = None
    current_parts: List[str] = []

    def flush() -> None:
        nonlocal current_kind, current_message_id, current_parts
        role = _message_role_for_update(current_kind or "")
        text = "".join(current_parts).strip()
        if role and text:
            item: Dict[str, str] = {"role": role, "text": text}
            if current_message_id:
                item["message_id"] = current_message_id
            messages.append(item)
        current_kind = None
        current_message_id = None
        current_parts = []

    for payload in updates:
        kind = _session_update_kind(payload)
        role = _message_role_for_update(kind)
        if role is None:
            flush()
            continue
        message_id_raw = payload.get("messageId")
        if not isinstance(message_id_raw, str) or not message_id_raw.strip():
            message_id_raw = payload.get("message_id")
        message_id = message_id_raw.strip() if isinstance(message_id_raw, str) and message_id_raw.strip() else None
        if current_parts and (
            kind != current_kind or (message_id and current_message_id and message_id != current_message_id)
        ):
            flush()
        if current_kind is None:
            current_kind = kind
        if current_message_id is None and message_id:
            current_message_id = message_id
        text = _content_text(payload.get("content"))
        if text:
            current_parts.append(text)
    flush()
    return messages


def _tool_call_id(state: Dict[str, Any]) -> str:
    return _string_value(state.get("toolCallId") or state.get("tool_call_id"))


def _tool_kind(state: Dict[str, Any]) -> str:
    return _string_value(state.get("kind")).lower() or "other"


def _tool_status(state: Dict[str, Any]) -> str:
    return _string_value(state.get("status")).lower() or "pending"


def _tool_title(state: Dict[str, Any], fallback: str) -> str:
    return _string_value(state.get("title")) or fallback


def _tool_raw_input(state: Dict[str, Any]) -> Dict[str, Any]:
    return _object_dict(state.get("rawInput"))


def _tool_raw_output(state: Dict[str, Any]) -> Dict[str, Any]:
    return _object_dict(state.get("rawOutput"))


def _tool_path_line(state: Dict[str, Any]) -> Tuple[str, Optional[int]]:
    locations = _list_of_dicts(state.get("locations"))
    if locations:
        path = _string_value(locations[0].get("path"))
        line = _int_value(locations[0].get("line"))
        if path:
            return path, line
    raw_input = _tool_raw_input(state)
    for key in ("path", "file_path", "filePath", "target", "destination"):
        path_value = raw_input.get(key)
        if isinstance(path_value, str) and path_value.strip():
            return path_value.strip(), None
    return "", None


def _tool_view_range(state: Dict[str, Any], line: Optional[int]) -> Optional[List[int]]:
    raw_input = _tool_raw_input(state)
    raw_view_range = raw_input.get("view_range") or raw_input.get("viewRange")
    if isinstance(raw_view_range, list) and len(raw_view_range) == 2:
        first = _int_value(raw_view_range[0])
        second = _int_value(raw_view_range[1])
        if first is not None and second is not None:
            return [first, second]
    if isinstance(line, int) and line > 0:
        return [line, line]
    return None


def _command_label(state: Dict[str, Any]) -> str:
    raw_input = _tool_raw_input(state)
    command = raw_input.get("command")
    if isinstance(command, str) and command.strip():
        args = raw_input.get("args")
        if isinstance(args, list) and args:
            parts = [command.strip(), *[str(item) for item in args if str(item).strip()]]
            return " ".join(parts)
        return command.strip()
    if isinstance(command, list):
        parts = [str(item) for item in command if str(item).strip()]
        if parts:
            return " ".join(parts)
    return _tool_title(state, "command")


def _find_url(value: Any) -> str:
    if isinstance(value, str):
        text = value.strip()
        if text.startswith("http://") or text.startswith("https://"):
            return text
        return ""
    if isinstance(value, list):
        for item in value:
            found = _find_url(item)
            if found:
                return found
        return ""
    if isinstance(value, dict):
        for item in value.values():
            found = _find_url(item)
            if found:
                return found
        return ""
    return ""


def _search_mode_and_pattern(state: Dict[str, Any]) -> Tuple[str, str]:
    raw_input = _tool_raw_input(state)
    tool_kind = _tool_kind(state)
    if tool_kind == "fetch":
        url = _find_url(raw_input)
        if url:
            return "web_search", url
    pattern = _string_value(raw_input.get("pattern"))
    if not pattern:
        pattern = _string_value(raw_input.get("query"))
    if not pattern:
        pattern = _string_value(raw_input.get("url"))
    mode = _string_value(raw_input.get("mode")) or ("web_search" if tool_kind == "fetch" and pattern else "search")
    return mode, pattern


def _tool_output_text(state: Dict[str, Any]) -> str:
    raw_output = _tool_raw_output(state)
    stdout = _string_value(raw_output.get("stdout"))
    stderr = _string_value(raw_output.get("stderr"))
    if stdout or stderr:
        return "\n".join(part for part in (stdout, stderr) if part)
    for key in ("output", "text", "content", "result"):
        value = raw_output.get(key)
        if isinstance(value, str) and value.strip():
            return value
    content_text = _tool_contents_text(state.get("content"))
    if content_text:
        return content_text
    if raw_output:
        return json.dumps(raw_output, indent=2, ensure_ascii=False)
    return ""


def _tool_command_output_parts(state: Dict[str, Any]) -> Tuple[str, str, int]:
    raw_output = _tool_raw_output(state)
    stdout = _string_value(raw_output.get("stdout"))
    stderr = _string_value(raw_output.get("stderr"))
    if not stdout and not stderr:
        stdout = _tool_contents_text(state.get("content"))
    exit_code = _int_value(raw_output.get("exitCode"))
    if exit_code is None:
        exit_code = _int_value(raw_output.get("exit_code"))
    if exit_code is None:
        exit_code = 1 if _tool_status(state) == "failed" else 0
    return stdout, stderr, exit_code


def _render_diff(path: str, old_text: Optional[str], new_text: str) -> str:
    resolved_path = path or "file"
    before_lines = old_text.splitlines() if isinstance(old_text, str) else []
    after_lines = new_text.splitlines()
    from_file = f"a/{resolved_path}" if isinstance(old_text, str) else "/dev/null"
    to_file = f"b/{resolved_path}"
    return "\n".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=from_file,
            tofile=to_file,
            lineterm="",
        )
    )


def _tool_diff_payloads(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    payloads: List[Dict[str, Any]] = []
    tool_call_id = _tool_call_id(state)
    fallback_path, _ = _tool_path_line(state)
    for index, content in enumerate(_list_of_dicts(state.get("content")), start=1):
        if _string_value(content.get("type")) != "diff":
            continue
        path = _string_value(content.get("path")) or fallback_path
        new_text = content.get("newText")
        if not isinstance(new_text, str):
            continue
        old_text_raw = content.get("oldText")
        old_text = old_text_raw if isinstance(old_text_raw, str) else None
        diff_text = _render_diff(path, old_text, new_text)
        payloads.append({
            "id": f"{tool_call_id}:diff:{index}",
            "path": path,
            "text": diff_text,
        })
    return payloads


def _generic_tool_name(state: Dict[str, Any]) -> str:
    tool_kind = _tool_kind(state)
    if tool_kind == "edit":
        return "apply_patch"
    return tool_kind or "tool"


def _generic_tool_request(state: Dict[str, Any]) -> Dict[str, Any]:
    tool_name = _generic_tool_name(state)
    raw_input = _tool_raw_input(state)
    request = build_tool_card_request("", tool_name, raw_input)
    return request if isinstance(request, dict) else {}


def _generic_tool_arguments(state: Dict[str, Any]) -> Dict[str, Any]:
    return _tool_raw_input(state)


def _generic_tool_result(state: Dict[str, Any]) -> Any:
    raw_output = _tool_raw_output(state)
    if raw_output:
        return raw_output
    output_text = _tool_output_text(state)
    return output_text if output_text else None


def _generic_tool_response(state: Dict[str, Any]) -> Any:
    tool_name = _generic_tool_name(state)
    result_payload = _generic_tool_result(state)
    response_source: Any = result_payload if result_payload is not None else {}
    return build_tool_card_response("", tool_name, response_source)


def _generic_tool_change_preview(state: Dict[str, Any]) -> Tuple[str, List[Dict[str, str]]]:
    changes: List[Dict[str, str]] = []
    primary_diff = ""
    for diff_payload in _tool_diff_payloads(state):
        diff_text = diff_payload["text"]
        if not primary_diff and diff_text:
            primary_diff = diff_text
        changes.append({
            "path": diff_payload["path"],
            "diff": diff_text,
            "unified_diff": diff_text,
        })
    return primary_diff, changes


def _token_usage_event_and_entry(
    *,
    conversation_id: str,
    turn_id: str,
    payload: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    used = _int_value(payload.get("used"))
    if used is None:
        return None, None
    size = _int_value(payload.get("size"))
    event: Dict[str, Any] = {
        "type": "token_count",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "total": used,
    }
    entry: Dict[str, Any] = {
        "role": "token_usage",
        "turn_id": turn_id,
        "total": used,
        "timestamp": utc_ts(),
    }
    if size is not None:
        event["context_window"] = size
        entry["context_window"] = size
    return event, entry


def _tool_transcript_entries(turn_id: str, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    tool_call_id = _tool_call_id(state)
    tool_kind = _tool_kind(state)
    status = _tool_status(state)
    is_error = status == "failed"
    path, line = _tool_path_line(state)
    entries: List[Dict[str, Any]] = []

    if tool_kind == "execute":
        stdout, stderr, exit_code = _tool_command_output_parts(state)
        output = "\n".join(part for part in (stdout, stderr) if part)
        entry: Dict[str, Any] = {
            "role": "command",
            "id": tool_call_id,
            "item_id": tool_call_id,
            "turn_id": turn_id,
            "command": _command_label(state),
            "output": output,
            "status": "error" if is_error else "completed",
            "timestamp": utc_ts(),
            "exit_code": exit_code,
        }
        if path:
            entry["path"] = path
        if line is not None:
            entry["line"] = line
        entries.append(entry)
        return entries

    if tool_kind == "read":
        view_range = _tool_view_range(state, line)
        output_text = _tool_output_text(state)
        entry = {
            "role": "view",
            "id": tool_call_id,
            "item_id": tool_call_id,
            "turn_id": turn_id,
            "title": _tool_title(state, "view"),
            "path": path,
            "content": output_text,
            "timestamp": utc_ts(),
        }
        if view_range is not None:
            entry["view_range"] = view_range
        entries.append(entry)
        return entries

    if tool_kind in {"search", "fetch"}:
        mode, pattern = _search_mode_and_pattern(state)
        entry = {
            "role": "search",
            "id": tool_call_id,
            "item_id": tool_call_id,
            "turn_id": turn_id,
            "title": "web search" if mode == "web_search" else _tool_title(state, "search"),
            "mode": mode,
            "path": path,
            "pattern": pattern,
            "arguments": _tool_raw_input(state),
            "content": _tool_output_text(state),
            "timestamp": utc_ts(),
        }
        entries.append(entry)
        return entries

    generic_entry: Dict[str, Any] = {
        "role": "tool",
        "id": tool_call_id,
        "item_id": tool_call_id,
        "turn_id": turn_id,
        "tool": _generic_tool_name(state),
        "arguments": _generic_tool_arguments(state),
        "request": _generic_tool_request(state),
        "result": _generic_tool_result(state),
        "response": _generic_tool_response(state),
        "output": _tool_output_text(state),
        "status": "error" if is_error else "completed",
        "is_error": is_error,
        "timestamp": utc_ts(),
    }
    if path:
        generic_entry["path"] = path
    diff_text, changes = _generic_tool_change_preview(state)
    if diff_text:
        generic_entry["diff"] = diff_text
    if changes:
        generic_entry["changes"] = changes
    entries.append(generic_entry)
    for diff_payload in _tool_diff_payloads(state):
        entries.append({
            "role": "diff",
            "id": diff_payload["id"],
            "item_id": diff_payload["id"],
            "turn_id": turn_id,
            "text": diff_payload["text"],
            "path": diff_payload["path"],
            "timestamp": utc_ts(),
        })
    return entries


def _command_live_events(conversation_id: str, turn_id: str, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    path, line = _tool_path_line(state)
    command = _command_label(state)
    raw_input = _tool_raw_input(state)
    stdout, stderr, exit_code = _tool_command_output_parts(state)
    combined_output = "\n".join(part for part in (stdout, stderr) if part)
    tool_call_id = _tool_call_id(state)
    shell_begin: Dict[str, Any] = {
        "type": "shell_begin",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "id": tool_call_id,
        "command": command,
        "activity": "executing",
        "cwd": _string_value(raw_input.get("cwd")),
    }
    if path:
        shell_begin["path"] = path
    if line is not None:
        shell_begin["line"] = line

    shell_end: Dict[str, Any] = {
        "type": "shell_end",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "id": tool_call_id,
        "command": command,
        "stdout": stdout,
        "stderr": stderr,
        "exitCode": exit_code,
    }
    if path:
        shell_end["path"] = path
    if line is not None:
        shell_end["line"] = line

    events = [shell_begin]
    if combined_output or exit_code != 0:
        command_result: Dict[str, Any] = {
            "type": "command_result",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "id": tool_call_id,
            "command": command,
            "cwd": _string_value(raw_input.get("cwd")),
            "output": combined_output,
            "exit_code": exit_code,
        }
        if path:
            command_result["path"] = path
        if line is not None:
            command_result["line"] = line
        events.append(command_result)
    events.append(shell_end)
    return events


def _view_live_event(conversation_id: str, turn_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    path, line = _tool_path_line(state)
    view_range = _tool_view_range(state, line)
    event: Dict[str, Any] = {
        "type": "view",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "id": _tool_call_id(state),
        "title": _tool_title(state, "view"),
        "path": path,
        "content": _tool_output_text(state),
    }
    if view_range is not None:
        event["view_range"] = view_range
    return event


def _search_live_event(conversation_id: str, turn_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    path, _ = _tool_path_line(state)
    mode, pattern = _search_mode_and_pattern(state)
    return {
        "type": "search",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "id": _tool_call_id(state),
        "title": "web search" if mode == "web_search" else _tool_title(state, "search"),
        "mode": mode,
        "path": path,
        "pattern": pattern,
        "arguments": _tool_raw_input(state),
        "content": _tool_output_text(state),
    }


def _generic_tool_begin_event(conversation_id: str, turn_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    path, _ = _tool_path_line(state)
    diff_text, changes = _generic_tool_change_preview(state)
    event: Dict[str, Any] = {
        "type": "tool_begin",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "id": _tool_call_id(state),
        "tool": _generic_tool_name(state),
        "request": _generic_tool_request(state),
        "arguments": _generic_tool_arguments(state),
    }
    if path:
        event["path"] = path
    if diff_text:
        event["diff"] = diff_text
    if changes:
        event["changes"] = changes
    return event


def _generic_tool_end_event(conversation_id: str, turn_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    path, _ = _tool_path_line(state)
    is_error = _tool_status(state) == "failed"
    diff_text, changes = _generic_tool_change_preview(state)
    event: Dict[str, Any] = {
        "type": "tool_end",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "id": _tool_call_id(state),
        "tool": _generic_tool_name(state),
        "request": _generic_tool_request(state),
        "arguments": _generic_tool_arguments(state),
        "result": _generic_tool_result(state),
        "response": _generic_tool_response(state),
        "output": _tool_output_text(state),
        "status": "error" if is_error else "completed",
        "is_error": is_error,
    }
    if path:
        event["path"] = path
    if diff_text:
        event["diff"] = diff_text
    if changes:
        event["changes"] = changes
    return event


def _diff_live_events(conversation_id: str, turn_id: str, state: Dict[str, Any]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for diff_payload in _tool_diff_payloads(state):
        events.append({
            "type": "diff",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "id": diff_payload["id"],
            "text": diff_payload["text"],
            "path": diff_payload["path"],
        })
    return events


def _merge_tool_state(state: Dict[str, Any], payload: Dict[str, Any], *, is_update: bool) -> None:
    if not is_update:
        state.update(payload)
        return
    for key in _TOOL_STATE_KEYS:
        if key in payload:
            state[key] = payload[key]


class GeminiLiveTurnAccumulator:
    def __init__(self, *, conversation_id: str, turn_id: str) -> None:
        self.conversation_id = conversation_id
        self.turn_id = turn_id
        self.assistant_id = f"{turn_id}:assistant"
        self.reasoning_id = f"{turn_id}:reasoning"
        self._assistant_parts: List[str] = []
        self._reasoning_parts: List[str] = []
        self._tool_calls: Dict[str, Dict[str, Any]] = {}
        self._shell_started: set[str] = set()
        self._generic_tool_started: set[str] = set()
        self._terminal_tool_ids: set[str] = set()
        self._pending_transcript_entries: List[Dict[str, Any]] = []

    def _consume_text_payload(self, payload: Dict[str, Any], *, emit_live: bool) -> List[Dict[str, Any]]:
        kind = _session_update_kind(payload)
        text = _content_text(payload.get("content"))
        if not text:
            return []
        if kind == "agent_message_chunk":
            self._assistant_parts.append(text)
            if not emit_live:
                return []
            return [{
                "type": "assistant_delta",
                "conversation_id": self.conversation_id,
                "turn_id": self.turn_id,
                "id": self.assistant_id,
                "delta": text,
            }]
        if kind == "agent_thought_chunk":
            self._reasoning_parts.append(text)
            if not emit_live:
                return []
            return [{
                "type": "reasoning_delta",
                "conversation_id": self.conversation_id,
                "turn_id": self.turn_id,
                "id": self.reasoning_id,
                "delta": text,
            }]
        return []


    def _consume_usage_payload(self, payload: Dict[str, Any], *, emit_live: bool) -> List[Dict[str, Any]]:
        event, entry = _token_usage_event_and_entry(
            conversation_id=self.conversation_id,
            turn_id=self.turn_id,
            payload=payload,
        )
        if entry is not None:
            self._pending_transcript_entries.append(entry)
        return [event] if emit_live and event is not None else []


    def _consume_tool_payload(
        self,
        payload: Dict[str, Any],
        *,
        is_update: bool,
        emit_live: bool,
    ) -> List[Dict[str, Any]]:
        tool_call_id = _string_value(payload.get("toolCallId") or payload.get("tool_call_id"))
        if not tool_call_id:
            return []
        state = self._tool_calls.setdefault(tool_call_id, {"toolCallId": tool_call_id})
        _merge_tool_state(state, payload, is_update=is_update)
        tool_kind = _tool_kind(state)
        if tool_kind in _SUPPRESSED_TOOL_KINDS:
            return []

        status = _tool_status(state)
        is_terminal = status in _TERMINAL_TOOL_STATUSES
        live_events: List[Dict[str, Any]] = []

        if tool_kind == "execute":
            if emit_live and tool_call_id not in self._shell_started:
                live_events.append(_command_live_events(self.conversation_id, self.turn_id, state)[0])
                self._shell_started.add(tool_call_id)
            if is_terminal and tool_call_id not in self._terminal_tool_ids:
                command_events = _command_live_events(self.conversation_id, self.turn_id, state)
                if emit_live:
                    if tool_call_id not in self._shell_started:
                        live_events.append(command_events[0])
                        self._shell_started.add(tool_call_id)
                    live_events.extend(command_events[1:])
                self._pending_transcript_entries.extend(_tool_transcript_entries(self.turn_id, state))
                self._terminal_tool_ids.add(tool_call_id)
            return live_events

        if tool_kind == "read":
            if is_terminal and tool_call_id not in self._terminal_tool_ids:
                if emit_live:
                    live_events.append(_view_live_event(self.conversation_id, self.turn_id, state))
                self._pending_transcript_entries.extend(_tool_transcript_entries(self.turn_id, state))
                self._terminal_tool_ids.add(tool_call_id)
            return live_events

        if tool_kind in {"search", "fetch"}:
            mode, _ = _search_mode_and_pattern(state)
            if mode == "web_search" or tool_kind == "search":
                if is_terminal and tool_call_id not in self._terminal_tool_ids:
                    if emit_live:
                        live_events.append(_search_live_event(self.conversation_id, self.turn_id, state))
                    self._pending_transcript_entries.extend(_tool_transcript_entries(self.turn_id, state))
                    self._terminal_tool_ids.add(tool_call_id)
                return live_events

        if emit_live and not is_terminal and tool_call_id not in self._generic_tool_started:
            live_events.append(_generic_tool_begin_event(self.conversation_id, self.turn_id, state))
            self._generic_tool_started.add(tool_call_id)
        if is_terminal and tool_call_id not in self._terminal_tool_ids:
            if emit_live:
                if tool_call_id not in self._generic_tool_started:
                    live_events.append(_generic_tool_begin_event(self.conversation_id, self.turn_id, state))
                    self._generic_tool_started.add(tool_call_id)
                live_events.append(_generic_tool_end_event(self.conversation_id, self.turn_id, state))
                live_events.extend(_diff_live_events(self.conversation_id, self.turn_id, state))
            self._pending_transcript_entries.extend(_tool_transcript_entries(self.turn_id, state))
            self._terminal_tool_ids.add(tool_call_id)
        return live_events


    def _consume_payload(self, payload: Dict[str, Any], *, emit_live: bool) -> List[Dict[str, Any]]:
        kind = _session_update_kind(payload)
        if kind in {"agent_message_chunk", "agent_thought_chunk"}:
            return self._consume_text_payload(payload, emit_live=emit_live)
        if kind == "usage_update":
            return self._consume_usage_payload(payload, emit_live=emit_live)
        if kind == "tool_call":
            return self._consume_tool_payload(payload, is_update=False, emit_live=emit_live)
        if kind == "tool_call_update":
            return self._consume_tool_payload(payload, is_update=True, emit_live=emit_live)
        return []


    def consume_live_update(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        return self._consume_payload(payload, emit_live=True)


    def consume_batch_updates(self, updates: List[Dict[str, Any]]) -> None:
        for payload in updates:
            self._consume_payload(payload, emit_live=False)


    def has_content(self) -> bool:
        return bool(
            self._assistant_parts
            or self._reasoning_parts
            or self._pending_transcript_entries
            or self._tool_calls
        )


    def finalize(
        self,
        *,
        stop_reason: str,
        debug_trace: bool = False,
        updates: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        events: List[Dict[str, Any]] = []
        transcript_entries: List[Dict[str, Any]] = list(self._pending_transcript_entries)

        reasoning_text = "".join(self._reasoning_parts).strip()
        if reasoning_text:
            events.append({
                "type": "reasoning_finalize",
                "conversation_id": self.conversation_id,
                "turn_id": self.turn_id,
                "id": self.reasoning_id,
                "text": reasoning_text,
            })
            transcript_entries.append(
                _build_message_transcript_entry(
                    turn_id=self.turn_id,
                    message_id=self.reasoning_id,
                    role="reasoning",
                    text=reasoning_text,
                )
            )

        assistant_text = "".join(self._assistant_parts).strip()
        if assistant_text:
            events.append({
                "type": "assistant_finalize",
                "conversation_id": self.conversation_id,
                "turn_id": self.turn_id,
                "id": self.assistant_id,
                "text": assistant_text,
            })
            transcript_entries.append(
                _build_message_transcript_entry(
                    turn_id=self.turn_id,
                    message_id=self.assistant_id,
                    role="assistant",
                    text=assistant_text,
                )
            )

        warning_event = _warning_for_stop_reason(
            conversation_id=self.conversation_id,
            turn_id=self.turn_id,
            stop_reason=stop_reason,
        )
        if warning_event is not None:
            events.append(warning_event)

        if debug_trace and isinstance(updates, list):
            transcript_entries.extend(_debug_trace_entries(
                conversation_id=self.conversation_id,
                turn_id=self.turn_id,
                updates=updates,
            ))

        return {
            "events": events,
            "transcript_entries": transcript_entries,
        }


def _debug_trace_entries(
    *,
    conversation_id: str,
    turn_id: str,
    updates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    for index, payload in enumerate(updates, start=1):
        kind = _session_update_kind(payload) or "session_update"
        entries.append({
            "role": "debug_raw",
            "type": "debug_raw",
            "internal": True,
            "visibility": "internal",
            "source": "gemini-acp.raw",
            "direction": "recv",
            "conversation_id": conversation_id,
            "turn_id": turn_id,
            "timestamp": utc_ts(),
            "debug_index": index,
            "summary": kind,
            "payload": payload,
        })
    return entries


def _warning_for_stop_reason(
    *,
    conversation_id: str,
    turn_id: str,
    stop_reason: str,
) -> Optional[Dict[str, Any]]:
    normalized = stop_reason.strip().lower()
    if not normalized or normalized == "end_turn":
        return None
    if normalized == "cancelled":
        message = "Gemini turn cancelled."
    else:
        message = f"Gemini turn stopped: {stop_reason.strip()}."
    return {
        "type": "warning",
        "conversation_id": conversation_id,
        "turn_id": turn_id,
        "message": message,
    }


def build_user_turn_output(
    *,
    conversation_id: str,
    turn_id: str,
    user_message_id: str,
    text: str,
) -> Dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        return {"events": [], "transcript_entries": []}
    return {
        "events": [
            _build_message_event(
                conversation_id=conversation_id,
                turn_id=turn_id,
                message_id=user_message_id,
                role="user",
                text=stripped,
            ),
        ],
        "transcript_entries": [
            _build_message_transcript_entry(
                turn_id=turn_id,
                message_id=user_message_id,
                role="user",
                text=stripped,
            ),
        ],
    }


def build_prompt_turn_output(
    *,
    conversation_id: str,
    session_id: str,
    turn_id: str,
    updates: List[Dict[str, Any]],
    stop_reason: str,
    debug_trace: bool = False,
) -> Dict[str, Any]:
    del session_id
    accumulator = GeminiLiveTurnAccumulator(
        conversation_id=conversation_id,
        turn_id=turn_id,
    )
    accumulator.consume_batch_updates(updates)
    return accumulator.finalize(
        stop_reason=stop_reason,
        debug_trace=debug_trace,
        updates=updates,
    )


def build_history_transcript_entries(
    *,
    session_id: str,
    updates: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    transcript_entries: List[Dict[str, Any]] = []
    tool_states: Dict[str, Dict[str, Any]] = {}
    completed_tool_ids: set[str] = set()
    history_turn_id = f"{session_id}:history"

    for payload in updates:
        kind = _session_update_kind(payload)
        if kind == "usage_update":
            _, entry = _token_usage_event_and_entry(
                conversation_id="",
                turn_id=history_turn_id,
                payload=payload,
            )
            if entry is not None:
                transcript_entries.append(entry)
            continue
        if kind not in {"tool_call", "tool_call_update"}:
            continue
        tool_call_id = _string_value(payload.get("toolCallId") or payload.get("tool_call_id"))
        if not tool_call_id:
            continue
        state = tool_states.setdefault(tool_call_id, {"toolCallId": tool_call_id})
        _merge_tool_state(state, payload, is_update=kind == "tool_call_update")
        if _tool_status(state) in _TERMINAL_TOOL_STATUSES and tool_call_id not in completed_tool_ids:
            transcript_entries.extend(_tool_transcript_entries(history_turn_id, state))
            completed_tool_ids.add(tool_call_id)

    for index, message in enumerate(_collect_message_updates(updates), start=1):
        role = _string_value(message.get("role"))
        text = _string_value(message.get("text"))
        if not role or not text:
            continue
        message_id = _string_value(message.get("message_id")) or f"{session_id}:history:{role}:{index}"
        transcript_entries.append(
            _build_message_transcript_entry(
                turn_id=f"{session_id}:history:{index}",
                message_id=message_id,
                role=role,
                text=text,
            )
        )
    return transcript_entries


async def route_event(
    *,
    extension_id: str,
    label: Optional[str],
    payload: Any,
    conversation_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    turn_id: Optional[str] = None,
    request_id: Optional[str] = None,
) -> Dict[str, Any]:
    del extension_id, label, payload, conversation_id, thread_id, turn_id, request_id
    return {
        "ok": False,
        "error": "Gemini ACP uses SDK callbacks inside the client/transport layer; no external router is wired yet.",
    }
