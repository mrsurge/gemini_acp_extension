"""Gemini ACP routing helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


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


def _content_text(content: Any) -> str:
    if not isinstance(content, dict):
        return ""
    content_type = content.get("type")
    if content_type == "text":
        text = content.get("text")
        return text if isinstance(text, str) else ""
    return ""


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


def _collect_assistant_messages(updates: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    return [item for item in _collect_message_updates(updates) if item.get("role") == "assistant"]


class GeminiLiveTurnAccumulator:
    def __init__(self, *, conversation_id: str, turn_id: str) -> None:
        self.conversation_id = conversation_id
        self.turn_id = turn_id
        self.assistant_id = f"{turn_id}:assistant"
        self.reasoning_id = f"{turn_id}:reasoning"
        self._assistant_parts: List[str] = []
        self._reasoning_parts: List[str] = []

    def _consume_payload(self, payload: Dict[str, Any], *, emit_live: bool) -> List[Dict[str, Any]]:
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

    def consume_live_update(self, payload: Dict[str, Any]) -> List[Dict[str, Any]]:
        return self._consume_payload(payload, emit_live=True)

    def consume_batch_updates(self, updates: List[Dict[str, Any]]) -> None:
        for payload in updates:
            self._consume_payload(payload, emit_live=False)

    def has_content(self) -> bool:
        return bool(self._assistant_parts or self._reasoning_parts)

    def finalize(
        self,
        *,
        stop_reason: str,
        debug_trace: bool = False,
        updates: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        events: List[Dict[str, Any]] = []
        transcript_entries: List[Dict[str, Any]] = []

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
    for index, message in enumerate(_collect_message_updates(updates), start=1):
        role = str(message.get("role") or "").strip()
        text = str(message.get("text") or "").strip()
        if not role or not text:
            continue
        message_id = str(message.get("message_id") or f"{session_id}:history:{role}:{index}")
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
