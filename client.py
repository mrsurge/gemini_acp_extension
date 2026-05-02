"""Gemini ACP client module for multi-root loading tests."""

from __future__ import annotations

import asyncio
import difflib
import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from . import approval_state as approval_sm
from .dependencies import check_dependencies
from .router import GeminiLiveTurnAccumulator, build_history_transcript_entries, build_user_turn_output, utc_ts
from .transport import GeminiACPTransport
from .vendor_sdk import ensure_sdk_on_path

ensure_sdk_on_path()

from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse  # noqa: E402

_broadcast_fn: Optional[Callable[..., Any]] = None
_transcript_fn: Optional[Callable[..., Any]] = None
_meta_fns: Dict[str, Callable[..., Any]] = {}
_registered_extension_ids: set[str] = set()
_ready_extensions: set[str] = set()
_transport: Optional[GeminiACPTransport] = None
_EXTENSION_ROOT = Path(__file__).parent
_GEMINI_PERMISSION_REQUEST_METHOD = "gemini-acp/permission"
_pending_approvals: Dict[str, asyncio.Future[object]] = {}
_pending_request_specs: Dict[str, Dict[str, Any]] = {}
_pending_approval_conversations: Dict[str, str] = {}
_active_turn_ids: Dict[str, str] = {}


def init_gemini_acp_manager(
    extensions_dir: Path,
    server_root: Path,
    fws_getter: Callable[..., Any],
    broadcast_fn: Callable[..., Any],
    transcript_fn: Callable[..., Any],
    meta_fns: Optional[Dict[str, Callable[..., Any]]] = None,
    registered_extension_ids: Optional[List[str]] = None,
) -> None:
    del extensions_dir, server_root
    global _broadcast_fn, _transcript_fn, _meta_fns, _registered_extension_ids, _transport
    _broadcast_fn = broadcast_fn
    _transcript_fn = transcript_fn
    _meta_fns = dict(meta_fns or {})
    _registered_extension_ids = {
        ext_id for ext_id in (registered_extension_ids or []) if isinstance(ext_id, str) and ext_id
    }
    _transport = GeminiACPTransport(extension_root=_EXTENSION_ROOT, fws_getter=fws_getter)
    _transport.set_permission_request_callback(_handle_permission_request)
    print("[GeminiACP] Scaffold transport initialized")


def _settings_schema_path() -> Path:
    return _EXTENSION_ROOT / "settings_schema.json"


def _runtime_option_descriptor(
    setting_key: str,
    label: str,
    options: List[Dict[str, str]],
    current: Optional[str],
    default: str,
) -> Dict[str, Any]:
    return {
        "settingKey": setting_key,
        "label": label,
        "options": [dict(item) for item in options],
        "current": current or "",
        "default": default,
    }


def _load_meta(conversation_id: str) -> Dict[str, Any]:
    loader = _meta_fns.get("load")
    if not callable(loader):
        return {}
    meta = loader(conversation_id)
    return dict(meta) if isinstance(meta, dict) else {}


def _save_meta(conversation_id: str, meta: Dict[str, Any]) -> None:
    saver = _meta_fns.get("save")
    if callable(saver):
        saver(conversation_id, meta)


def _upsert_pending_approval(conversation_id: str, descriptor: Dict[str, Any]) -> None:
    if _meta_fns and "upsert_pending_approval" in _meta_fns:
        _meta_fns["upsert_pending_approval"](conversation_id, descriptor)
        return
    if _meta_fns and "load" in _meta_fns and "save" in _meta_fns:
        meta = _meta_fns["load"](conversation_id)
        pending = meta.get("pending_approvals") if isinstance(meta.get("pending_approvals"), dict) else {}
        pending[str(descriptor.get("request_id") or "")] = descriptor
        meta["pending_approvals"] = pending
        _meta_fns["save"](conversation_id, meta)


def _remove_pending_approval(conversation_id: str, request_id: str) -> None:
    if _meta_fns and "remove_pending_approval" in _meta_fns:
        _meta_fns["remove_pending_approval"](conversation_id, request_id)
        return
    if _meta_fns and "load" in _meta_fns and "save" in _meta_fns:
        meta = _meta_fns["load"](conversation_id)
        pending = meta.get("pending_approvals") if isinstance(meta.get("pending_approvals"), dict) else {}
        pending.pop(str(request_id or ""), None)
        meta["pending_approvals"] = pending
        _meta_fns["save"](conversation_id, meta)


def _jsonish_value(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return _jsonish_value(value.model_dump(mode="json", by_alias=True))
    if isinstance(value, dict):
        return {str(key): _jsonish_value(inner) for key, inner in value.items()}
    if isinstance(value, list):
        return [_jsonish_value(item) for item in value]
    return value


def _object_dict(value: Any) -> Dict[str, Any]:
    converted = _jsonish_value(value)
    return dict(converted) if isinstance(converted, dict) else {}


def _string_value(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _tool_call_id(tool_call: Dict[str, Any]) -> str:
    return _string_value(tool_call.get("toolCallId") or tool_call.get("tool_call_id"))


def _tool_call_kind(tool_call: Dict[str, Any]) -> str:
    return _string_value(tool_call.get("kind")) or "permission"


def _tool_call_title(tool_call: Dict[str, Any]) -> str:
    return _string_value(tool_call.get("title"))


def _display_tool_name(tool_call: Dict[str, Any]) -> str:
    tool_kind = _tool_call_kind(tool_call).lower()
    if tool_kind == "edit":
        return "apply_patch"
    return tool_kind or "tool"


def _tool_call_locations(tool_call: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = tool_call.get("locations")
    if not isinstance(raw, list):
        return []
    return [item for item in (_object_dict(entry) for entry in raw) if item]


def _permission_option_id(option: Any) -> str:
    if hasattr(option, "option_id"):
        return _string_value(getattr(option, "option_id"))
    if hasattr(option, "optionId"):
        return _string_value(getattr(option, "optionId"))
    return _string_value(_object_dict(option).get("optionId") or _object_dict(option).get("option_id"))


def _permission_option_kind(option: Any) -> str:
    if hasattr(option, "kind"):
        return _string_value(getattr(option, "kind")).lower()
    return _string_value(_object_dict(option).get("kind")).lower()


def _permission_option_name(option: Any) -> str:
    if hasattr(option, "name"):
        return _string_value(getattr(option, "name"))
    return _string_value(_object_dict(option).get("name"))


def _normalize_permission_options(options: List[Any]) -> List[Dict[str, str]]:
    normalized: List[Dict[str, str]] = []
    for option in options:
        option_id = _permission_option_id(option)
        option_kind = _permission_option_kind(option)
        option_name = _permission_option_name(option)
        if not option_id or not option_kind:
            continue
        normalized.append({
            "optionId": option_id,
            "kind": option_kind,
            "name": option_name or option_kind,
        })
    return normalized


def _pick_option_by_kind(
    options: List[Dict[str, str]],
    preferred_kinds: List[str],
) -> Optional[Dict[str, str]]:
    for option_kind in preferred_kinds:
        for option in options:
            if option.get("kind") == option_kind:
                return option
    return None


def _selected_option_response(option: Dict[str, str]) -> RequestPermissionResponse:
    return RequestPermissionResponse(
        outcome=AllowedOutcome(
            option_id=str(option.get("optionId") or ""),
            outcome="selected",
        )
    )


def _cancelled_permission_response() -> RequestPermissionResponse:
    return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))


def _auto_permission_resolution(
    state: Dict[str, Any],
    options: List[Dict[str, str]],
) -> Optional[Dict[str, Any]]:
    normalized_state = approval_sm.normalize_state(state)
    policy = _string_value(normalized_state.get("policy"))
    option: Optional[Dict[str, str]] = None
    if policy == approval_sm.POLICY_ALWAYS_APPROVE:
        option = _pick_option_by_kind(options, ["allow_always", "allow_once"])
    elif policy == approval_sm.POLICY_ALWAYS_REJECT:
        option = _pick_option_by_kind(options, ["reject_always", "reject_once"])
        if option is None:
            return {
                "response": _cancelled_permission_response(),
                "next_state": approval_sm.after_serving_request(normalized_state),
            }
    elif policy == approval_sm.POLICY_ASK and normalized_state.get("ask_session_approved") is True:
        option = _pick_option_by_kind(options, ["allow_always", "allow_once"])
    if option is None:
        return None
    return {
        "response": _selected_option_response(option),
        "next_state": approval_sm.after_serving_request(normalized_state),
    }


def _decision_string(resolution: object) -> str:
    if isinstance(resolution, dict):
        for key in ("decision", "action", "kind"):
            value = resolution.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def _manual_permission_resolution(
    resolution: object,
    request_spec: Dict[str, Any],
) -> Dict[str, Any]:
    options = [
        dict(item)
        for item in request_spec.get("options", [])
        if isinstance(item, dict)
    ]
    current_state = approval_sm.normalize_state(request_spec.get("current_state"))
    decision = _decision_string(resolution)
    option: Optional[Dict[str, str]] = None
    next_state: Dict[str, Any] = current_state
    if decision == "acceptForSession":
        option = _pick_option_by_kind(options, ["allow_always", "allow_once"])
        next_state = {
            "policy": approval_sm.POLICY_ASK,
            "ask_session_approved": True,
        }
    elif decision == "accept":
        option = _pick_option_by_kind(options, ["allow_once", "allow_always"])
        next_state = {
            "policy": approval_sm.POLICY_ASK,
            "ask_session_approved": False,
        }
    elif decision in {"decline", "reject"}:
        option = _pick_option_by_kind(options, ["reject_once", "reject_always"])
        next_state = {
            "policy": approval_sm.POLICY_ASK,
            "ask_session_approved": False,
        }
    elif decision == "cancel":
        return {
            "response": _cancelled_permission_response(),
            "next_state": current_state,
        }
    if option is None:
        return {
            "response": _cancelled_permission_response(),
            "next_state": current_state,
        }
    return {
        "response": _selected_option_response(option),
        "next_state": approval_sm.after_serving_request(next_state),
    }


def _persist_manual_permission_state(
    request_spec: Dict[str, Any],
    result: Dict[str, Any],
) -> None:
    conversation_id = _string_value(request_spec.get("conversation_id"))
    next_state = result.get("next_state")
    if conversation_id and isinstance(next_state, dict):
        _persist_approval_state(conversation_id, next_state)


def _cancel_pending_approvals_for_conversation(conversation_id: str) -> None:
    for request_id, request_conversation_id in tuple(_pending_approval_conversations.items()):
        if request_conversation_id != conversation_id:
            continue
        pending_future = _pending_approvals.pop(request_id, None)
        _pending_request_specs.pop(request_id, None)
        _pending_approval_conversations.pop(request_id, None)
        _remove_pending_approval(conversation_id, request_id)
        if isinstance(pending_future, asyncio.Future) and not pending_future.done():
            pending_future.set_result({
                "response": _cancelled_permission_response(),
                "next_state": None,
            })


def _render_diff(path: str, old_text: Optional[str], new_text: str) -> str:
    target_path = path or "file"
    before_lines = old_text.splitlines() if isinstance(old_text, str) else []
    after_lines = new_text.splitlines()
    from_file = f"a/{target_path}" if isinstance(old_text, str) else "/dev/null"
    to_file = f"b/{target_path}"
    return "\n".join(
        difflib.unified_diff(
            before_lines,
            after_lines,
            fromfile=from_file,
            tofile=to_file,
            lineterm="",
        )
    )


def _tool_call_diff_changes(tool_call: Dict[str, Any]) -> List[Dict[str, str]]:
    contents = tool_call.get("content")
    if not isinstance(contents, list):
        return []
    changes: List[Dict[str, str]] = []
    for entry in contents:
        content = _object_dict(entry)
        if content.get("type") != "diff":
            continue
        path = _string_value(content.get("path"))
        new_text = content.get("newText")
        if not isinstance(new_text, str):
            continue
        old_text_raw = content.get("oldText")
        old_text = old_text_raw if isinstance(old_text_raw, str) else None
        diff_text = _render_diff(path, old_text, new_text)
        changes.append({
            "path": path,
            "diff": diff_text,
            "unified_diff": diff_text,
        })
    return changes


def _tool_call_path(tool_call: Dict[str, Any], raw_input: Dict[str, Any], locations: List[Dict[str, Any]]) -> str:
    if locations:
        path = _string_value(locations[0].get("path"))
        if path:
            return path
    for key in ("path", "file_path", "filePath", "target", "destination"):
        value = raw_input.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _approval_payload_for_tool_call(
    conversation_id: str,
    session_id: str,
    tool_call: Dict[str, Any],
    options: List[Dict[str, str]],
) -> Dict[str, Any]:
    tool_kind = _tool_call_kind(tool_call)
    tool_name = _display_tool_name(tool_call)
    title = _tool_call_title(tool_call)
    raw_input = _object_dict(tool_call.get("rawInput"))
    locations = _tool_call_locations(tool_call)
    path = _tool_call_path(tool_call, raw_input, locations)
    changes = _tool_call_diff_changes(tool_call)
    possible_paths: List[str] = []
    for location in locations:
        candidate = _string_value(location.get("path"))
        if candidate and candidate not in possible_paths:
            possible_paths.append(candidate)
    payload: Dict[str, Any] = {
        "kind": tool_kind,
        "message": title or f"Gemini ACP requests permission for {tool_kind}",
        "intention": title or f"Gemini ACP requests permission for {tool_kind}",
        "tool_name": tool_name,
        "session_id": session_id,
        "conversation_id": conversation_id,
        "arguments": raw_input,
        "request": raw_input,
    }
    if path:
        payload["path"] = path
    command = raw_input.get("command")
    if isinstance(command, str) and command.strip():
        payload["command"] = command.strip()
    elif isinstance(command, list):
        payload["command"] = [str(item) for item in command if str(item).strip()]
    cwd = raw_input.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        payload["cwd"] = cwd.strip()
    if changes:
        payload["changes"] = changes
        payload["diff"] = changes[0].get("diff") or ""
        if not payload.get("path"):
            payload["path"] = changes[0].get("path") or ""
    if possible_paths:
        payload["possible_paths"] = possible_paths
    if any(option.get("kind") == "allow_always" for option in options):
        payload["can_offer_session_approval"] = True
    return payload


def _approval_kind_for_tool_call(tool_call: Dict[str, Any], payload: Dict[str, Any]) -> str:
    if payload.get("diff") or payload.get("changes"):
        return "diff"
    return _tool_call_kind(tool_call)


def _build_permission_descriptor(
    conversation_id: str,
    session_id: str,
    request_id: str,
    tool_call: Dict[str, Any],
    options: List[Dict[str, str]],
    current_state: Dict[str, Any],
) -> Dict[str, Any]:
    turn_id = _active_turn_ids.get(conversation_id, "")
    payload = _approval_payload_for_tool_call(conversation_id, session_id, tool_call, options)
    kind = _approval_kind_for_tool_call(tool_call, payload)
    request_params: Dict[str, Any] = {
        "sessionId": session_id,
        "toolCallId": _tool_call_id(tool_call),
        "options": [dict(item) for item in options],
        "kind": kind,
        "tool_name": payload.get("tool_name"),
        "intention": payload.get("intention"),
        "request": payload.get("request"),
        "arguments": payload.get("arguments"),
        "command": payload.get("command"),
        "cwd": payload.get("cwd"),
        "path": payload.get("path"),
        "changes": payload.get("changes"),
        "diff": payload.get("diff"),
        "possible_paths": payload.get("possible_paths"),
        "availableDecisions": (
            ["accept", "acceptForSession", "decline"]
            if payload.get("can_offer_session_approval")
            else ["accept", "decline"]
        ),
        "currentApprovalPolicy": approval_sm.runtime_current_value(current_state),
    }
    created_at = utc_ts()
    render_event: Dict[str, Any] = {
        "type": "approval",
        "conversation_id": conversation_id,
        "id": request_id,
        "request_id": request_id,
        "kind": kind,
        "payload": payload,
        "turn_id": turn_id,
        "request_method": _GEMINI_PERMISSION_REQUEST_METHOD,
        "request_params": request_params,
        "created_at": created_at,
    }
    return {
        "request_id": request_id,
        "agent": "gemini-acp",
        "kind": kind,
        "payload": payload,
        "request_method": _GEMINI_PERMISSION_REQUEST_METHOD,
        "request_params": request_params,
        "thread_id": session_id,
        "turn_id": turn_id,
        "runtime_signature": _transport.runtime_instance_id() if _transport is not None else None,
        "runtime_instance_id": _transport.runtime_instance_id() if _transport is not None else None,
        "transcript_anchor": {"turn_id": turn_id},
        "source": "live",
        "created_at": created_at,
        "current_state": approval_sm.normalize_state(current_state),
        "render_event": render_event,
    }


def _bound_session_id(meta: Dict[str, Any]) -> Optional[str]:
    for key in ("thread_id", "gemini_acp_session_id"):
        raw = meta.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return None


def _merge_runtime_settings(conversation_id: str, settings: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    if conversation_id:
        meta = _load_meta(conversation_id)
        meta_settings = meta.get("settings")
        if isinstance(meta_settings, dict):
            merged.update(meta_settings)
    if isinstance(settings, dict):
        merged.update(settings)
    return merged


def _normalize_approval_policy(settings: Dict[str, Any]) -> str:
    """Return the top-level approval policy string used by the transport layer.

    Derived from the harness policy state machine; pure function over `settings`
    so callers that only need a transport-friendly hint can stay unchanged.
    """
    state = approval_sm.state_from_setting(
        settings.get("approval_policy") or settings.get("approval_mode")
    )
    return approval_sm.setting_from_state(state)


def _approval_state_for_conversation(
    conversation_id: str,
    merged_settings: Dict[str, Any],
) -> Dict[str, Any]:
    """Resolve the live approval state for a conversation.

    Order of precedence:
      1. Persisted state in conversation meta (`approval_policy_state`).
      2. Initial state derived from the merged settings field.
      3. Module default.
    """
    setting_value = merged_settings.get("approval_policy") or merged_settings.get("approval_mode")
    if conversation_id:
        meta = _load_meta(conversation_id)
        if setting_value == approval_sm.SETTING_ASK_CLEAR:
            meta[approval_sm.META_KEY] = approval_sm.state_from_setting(approval_sm.POLICY_ASK)
            meta_settings = meta.get("settings")
            if isinstance(meta_settings, dict):
                changed = False
                for key in ("approval_policy", "approval_mode"):
                    if meta_settings.get(key) == approval_sm.SETTING_ASK_CLEAR:
                        meta_settings[key] = approval_sm.POLICY_ASK
                        changed = True
                if changed:
                    meta["settings"] = meta_settings
            _save_meta(conversation_id, meta)
            return approval_sm.state_from_setting(approval_sm.POLICY_ASK)
        persisted = meta.get(approval_sm.META_KEY)
        if isinstance(persisted, dict):
            return approval_sm.apply_setting_override(persisted, setting_value)
    return approval_sm.state_from_setting(setting_value)


def _persist_approval_state(conversation_id: str, state: Dict[str, Any]) -> None:
    if not conversation_id:
        return
    meta = _load_meta(conversation_id)
    meta[approval_sm.META_KEY] = approval_sm.normalize_state(state)
    _save_meta(conversation_id, meta)


async def get_settings_schema(extension_id: str) -> Dict[str, Any]:
    del extension_id
    schema = json.loads(_settings_schema_path().read_text(encoding="utf-8"))
    schema["cache"] = "none"
    return schema


async def get_splash_schema(extension_id: str) -> Dict[str, Any]:
    return await get_settings_schema(extension_id)


async def get_runtime_options(
    extension_id: str,
    conversation_id: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    merged = _merge_runtime_settings(conversation_id or "", settings=settings)
    state = _approval_state_for_conversation(conversation_id or "", merged)
    approval_descriptor = _runtime_option_descriptor(
        "approval_policy",
        "Approval Policy",
        approval_sm.runtime_options_for_state(state),
        approval_sm.runtime_current_value(state),
        approval_sm.POLICY_ALWAYS_REJECT,
    )
    approval_descriptor["footerLabel"] = "Approval"
    approval_descriptor["accents"] = {
        approval_sm.POLICY_ALWAYS_APPROVE: "ok",
        approval_sm.POLICY_ALWAYS_REJECT: "err",
        approval_sm.SETTING_ASK_CLEAR: "ok",
    }
    return {
        "agent": extension_id,
        "approval": approval_descriptor,
    }


async def _handle_permission_request(
    conversation_id: str,
    session_id: str,
    options: List[Any],
    tool_call: Any,
) -> RequestPermissionResponse:
    normalized_options = _normalize_permission_options(options)
    merged_settings = _merge_runtime_settings(conversation_id)
    current_state = _approval_state_for_conversation(conversation_id, merged_settings)
    automatic = _auto_permission_resolution(current_state, normalized_options)
    if automatic is not None:
        next_state = automatic.get("next_state")
        if isinstance(next_state, dict):
            _persist_approval_state(conversation_id, next_state)
        response = automatic.get("response")
        if isinstance(response, RequestPermissionResponse):
            return response
        return _cancelled_permission_response()

    normalized_tool_call = _object_dict(tool_call)
    tool_call_id = _tool_call_id(normalized_tool_call) or f"tool_{uuid.uuid4().hex[:10]}"
    request_id = f"approval_{conversation_id[:8]}_{tool_call_id}"
    descriptor = _build_permission_descriptor(
        conversation_id,
        session_id,
        request_id,
        normalized_tool_call,
        normalized_options,
        current_state,
    )
    loop = asyncio.get_running_loop()
    future: asyncio.Future[object] = loop.create_future()
    _pending_approvals[request_id] = future
    _pending_request_specs[request_id] = {
        "type": "permission",
        "conversation_id": conversation_id,
        "session_id": session_id,
        "tool_call_id": tool_call_id,
        "options": [dict(item) for item in normalized_options],
        "current_state": approval_sm.normalize_state(current_state),
    }
    _pending_approval_conversations[request_id] = conversation_id
    _upsert_pending_approval(conversation_id, descriptor)
    render_event = descriptor.get("render_event")
    if callable(_broadcast_fn) and isinstance(render_event, dict):
        await _broadcast_fn(render_event)
    result = await future
    if isinstance(result, dict):
        next_state = result.get("next_state")
        if isinstance(next_state, dict):
            _persist_approval_state(conversation_id, next_state)
        response = result.get("response")
        if isinstance(response, RequestPermissionResponse):
            return response
    return _cancelled_permission_response()


def resolve_approval(request_id: str, resolution: object) -> bool:
    pending_future = _pending_approvals.pop(request_id, None)
    request_spec = _pending_request_specs.pop(request_id, None)
    _pending_approval_conversations.pop(request_id, None)
    if isinstance(pending_future, asyncio.Future) and not pending_future.done():
        result = _manual_permission_resolution(resolution, request_spec or {})
        _persist_manual_permission_state(request_spec or {}, result)
        pending_future.set_result(result)
        return True
    return False


def validate_pending_approval(conversation_id: str, request_id: str, descriptor: Dict[str, Any]) -> bool:
    if not isinstance(descriptor, dict):
        return False
    if request_id not in _pending_approvals:
        return False
    if _pending_approval_conversations.get(request_id) != conversation_id:
        return False
    transport = _transport
    if transport is None:
        return False
    descriptor_thread_id = descriptor.get("thread_id")
    current_session_id = transport.session_id_for(conversation_id)
    if descriptor_thread_id and current_session_id and descriptor_thread_id != current_session_id:
        return False
    if descriptor_thread_id and not current_session_id:
        return False
    descriptor_runtime = descriptor.get("runtime_instance_id") or descriptor.get("runtime_signature")
    current_runtime = transport.runtime_instance_id()
    if descriptor_runtime and current_runtime and descriptor_runtime != current_runtime:
        return False
    return True


async def list_models() -> List[Dict[str, Any]]:
    transport = _transport
    if transport is None:
        return []
    try:
        models = await transport.list_models(cwd=str(Path.home()))
        return [
            {
                "id": str(model.get("id") or ""),
                "name": str(model.get("name") or model.get("id") or ""),
                "description": str(model.get("description") or ""),
            }
            for model in models
            if isinstance(model, dict) and str(model.get("id") or "").strip()
        ]
    except Exception as exc:
        print(f"[GeminiACP] list_models failed: {exc}")
        return []


async def list_sessions(cwd: Optional[str] = None) -> List[Dict[str, Any]]:
    transport = _transport
    if transport is None:
        return []
    try:
        return await transport.list_sessions(cwd=str(Path(cwd).expanduser()) if cwd else None)
    except Exception as exc:
        print(f"[GeminiACP] list_sessions failed: {exc}")
        return []


async def warm_up_all_extensions(timeout: float = 60.0) -> Dict[str, bool]:
    results = {ext_id: False for ext_id in sorted(_registered_extension_ids)}
    transport = _transport
    if transport is None:
        return results
    try:
        await asyncio.wait_for(
            transport.ensure_ready(
                conversation_id="__gemini_acp_warmup__",
                cwd=str(Path.home()),
                approval_policy="auto_deny",
            ),
            timeout=timeout,
        )
        for extension_id in results:
            results[extension_id] = True
            _ready_extensions.add(extension_id)
    except Exception as exc:
        print(f"[GeminiACP] warm-up failed: {exc}")
    return results


def is_extension_ready(extension_id: str) -> bool:
    return extension_id in _ready_extensions and _transport is not None and _transport.is_ready()


async def wait_extension_ready(extension_id: str, timeout: float = 60.0) -> bool:
    if is_extension_ready(extension_id):
        return True
    results = await warm_up_all_extensions(timeout=timeout)
    return bool(results.get(extension_id))


async def handle_message(
    conversation_id: str,
    text: str,
    agent_type: str,
    settings: Dict[str, Any],
) -> Dict[str, Any]:
    extension_id = agent_type or "gemini-acp"
    transport = _transport
    if transport is None:
        return {"ok": False, "error": "Gemini ACP transport not initialized"}
    if not conversation_id or not text.strip():
        return {"ok": False, "error": "conversation_id and text required"}
    merged_settings = _merge_runtime_settings(conversation_id, settings=settings)
    cwd = str(merged_settings.get("cwd") or Path.home())
    approval_state = _approval_state_for_conversation(conversation_id, merged_settings)
    approval_policy = approval_sm.setting_from_state(approval_state)
    debug_trace = bool(merged_settings.get("debug_trace"))
    meta = _load_meta(conversation_id)
    meta[approval_sm.META_KEY] = approval_state
    existing_session_id = _bound_session_id(meta)
    turn_counter_raw = meta.get("gemini_acp_turn_counter")
    turn_counter = turn_counter_raw if isinstance(turn_counter_raw, int) else 0
    turn_counter += 1
    turn_id = f"gemini_turn_{turn_counter}"
    user_message_id = str(uuid.uuid4())
    meta["gemini_acp_turn_counter"] = turn_counter
    user_turn_output = build_user_turn_output(
        conversation_id=conversation_id,
        turn_id=turn_id,
        user_message_id=user_message_id,
        text=text,
    )
    user_transcript_entries = user_turn_output.get("transcript_entries")
    if callable(_transcript_fn) and isinstance(user_transcript_entries, list):
        for entry in user_transcript_entries:
            if isinstance(entry, dict):
                await _transcript_fn(conversation_id, entry)
    user_events = user_turn_output.get("events")
    if callable(_broadcast_fn) and isinstance(user_events, list):
        for event in user_events:
            if isinstance(event, dict):
                await _broadcast_fn(event)
    turn_accumulator = GeminiLiveTurnAccumulator(
        conversation_id=conversation_id,
        turn_id=turn_id,
    )

    async def _handle_live_update(payload: Dict[str, Any]) -> None:
        events = turn_accumulator.consume_live_update(payload)
        if callable(_broadcast_fn):
            for event in events:
                await _broadcast_fn(event)

    _active_turn_ids[conversation_id] = turn_id
    try:
        prompt_result = await transport.send_prompt(
            conversation_id=conversation_id,
            text=text,
            cwd=cwd,
            approval_policy=approval_policy,
            model=str(merged_settings.get("model") or "").strip() or None,
            message_id=user_message_id,
            existing_session_id=existing_session_id,
            on_update=_handle_live_update,
        )
    except Exception as exc:
        message = f"Gemini ACP send failed: {exc}"
        if callable(_broadcast_fn):
            await _broadcast_fn({
                "type": "error",
                "conversation_id": conversation_id,
                "turn_id": turn_id,
                "message": message,
                "source": "gemini-acp",
            })
        if callable(_transcript_fn):
            await _transcript_fn(conversation_id, {
                "role": "error",
                "message": message,
                "text": message,
                "timestamp": utc_ts(),
                "turn_id": turn_id,
                "source": "gemini-acp",
            })
        meta["status"] = "error"
        meta["last_error"] = message
        _save_meta(conversation_id, meta)
        return {"ok": False, "error": str(exc), "restore_draft": True}
    finally:
        _active_turn_ids.pop(conversation_id, None)

    if not turn_accumulator.has_content():
        turn_accumulator.consume_batch_updates(prompt_result.updates)
    routed = turn_accumulator.finalize(
        stop_reason=prompt_result.stop_reason,
        debug_trace=debug_trace,
        updates=prompt_result.updates,
    )
    transcript_entries = routed.get("transcript_entries")
    if callable(_transcript_fn) and isinstance(transcript_entries, list):
        for entry in transcript_entries:
            if isinstance(entry, dict):
                await _transcript_fn(conversation_id, entry)

    events = routed.get("events")
    if callable(_broadcast_fn) and isinstance(events, list):
        for event in events:
            if isinstance(event, dict):
                await _broadcast_fn(event)

    _ready_extensions.add(extension_id)

    meta["thread_id"] = prompt_result.session_id
    meta["gemini_acp_session_id"] = prompt_result.session_id
    meta["gemini_acp_shell_id"] = transport.runtime_instance_id()
    meta["gemini_acp_last_stop_reason"] = prompt_result.stop_reason
    meta["gemini_acp_last_user_message_id"] = prompt_result.user_message_id or user_message_id
    meta["status"] = "active"
    current_approval_state = _approval_state_for_conversation(conversation_id, merged_settings)
    meta[approval_sm.META_KEY] = approval_sm.after_turn_end(current_approval_state)
    _save_meta(conversation_id, meta)

    return {
        "ok": True,
        "session_id": prompt_result.session_id,
        "shell_id": transport.runtime_instance_id(),
        "stop_reason": prompt_result.stop_reason,
        "event_count": len(events) if isinstance(events, list) else 0,
        "transcript_entry_count": len(transcript_entries) if isinstance(transcript_entries, list) else 0,
    }


async def resume_session_with_history(
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
    extension_id: Optional[str] = None,
) -> Dict[str, Any]:
    transport = _transport
    if transport is None:
        return {"ok": False, "error": "Gemini ACP transport not initialized"}
    if not session_id or not conversation_id:
        return {"ok": False, "error": "session_id and conversation_id are required"}
    meta = _load_meta(conversation_id)
    existing_session_id = _bound_session_id(meta)
    if existing_session_id and existing_session_id != session_id:
        return {
            "ok": False,
            "error": f"Conversation already bound to session {existing_session_id[:8]}",
        }
    merged_settings = _merge_runtime_settings(conversation_id, settings=settings)
    if cwd:
        merged_settings["cwd"] = cwd
    if model:
        merged_settings["model"] = model
    resolved_cwd = str(merged_settings.get("cwd") or Path.home())
    approval_policy = _normalize_approval_policy(merged_settings)
    resolved_model = str(merged_settings.get("model") or "").strip() or None
    try:
        bound_session_id = await transport.bind_session(
            conversation_id=conversation_id,
            session_id=session_id,
            cwd=resolved_cwd,
            approval_policy=approval_policy,
            model=resolved_model,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Gemini ACP session bind failed: {exc}"}
    meta["thread_id"] = bound_session_id
    meta["gemini_acp_session_id"] = bound_session_id
    meta["gemini_acp_shell_id"] = transport.runtime_instance_id()
    meta["status"] = "active"
    persisted_settings = dict(meta.get("settings") or {}) if isinstance(meta.get("settings"), dict) else {}
    persisted_settings["agent"] = extension_id or "gemini-acp"
    for key, value in merged_settings.items():
        if value is None or value == "":
            persisted_settings.pop(key, None)
        else:
            persisted_settings[key] = value
    meta["settings"] = persisted_settings
    _save_meta(conversation_id, meta)
    _ready_extensions.add(extension_id or "gemini-acp")
    return {
        "ok": True,
        "session_id": bound_session_id,
        "conversation_id": conversation_id,
    }


async def hydrate_transcript(
    session_id: str,
    conversation_id: str,
    cwd: Optional[str] = None,
    model: Optional[str] = None,
    settings: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    transport = _transport
    if transport is None or not session_id or not conversation_id:
        return []
    merged_settings = _merge_runtime_settings(conversation_id, settings=settings)
    if cwd:
        merged_settings["cwd"] = cwd
    if model:
        merged_settings["model"] = model
    resolved_cwd = str(merged_settings.get("cwd") or Path.home())
    approval_policy = _normalize_approval_policy(merged_settings)
    try:
        updates = await transport.load_session_history(
            conversation_id=conversation_id,
            session_id=session_id,
            cwd=resolved_cwd,
            approval_policy=approval_policy,
        )
    except Exception as exc:
        print(f"[GeminiACP] hydrate_transcript failed: {exc}")
        return []
    return build_history_transcript_entries(session_id=session_id, updates=updates)


async def abort_session(conversation_id: str) -> bool:
    transport = _transport
    if transport is None:
        return False
    had_pending = any(cid == conversation_id for cid in _pending_approval_conversations.values())
    _cancel_pending_approvals_for_conversation(conversation_id)
    try:
        cancelled = await transport.cancel_session(conversation_id)
    except Exception as exc:
        print(f"[GeminiACP] abort_session failed: {exc}")
        return had_pending
    return bool(cancelled or had_pending)


async def compact_session(conversation_id: str) -> Dict[str, Any]:
    return {
        "ok": False,
        "conversation_id": conversation_id,
        "error": "Gemini ACP does not support compact",
    }


async def shutdown_client() -> None:
    transport = _transport
    if transport is not None:
        await transport.stop()
