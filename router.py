"""Router stub for the Gemini ACP scaffold."""

from __future__ import annotations

from typing import Any, Dict, Optional


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
        "error": "Gemini ACP scaffold uses SDK callbacks inside the client/transport layer; no external router is wired yet.",
    }
