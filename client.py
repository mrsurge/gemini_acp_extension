"""Gemini ACP scaffold client module for multi-root loading tests."""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .dependencies import check_dependencies
from .router import build_history_transcript_entries, build_prompt_turn_output, build_user_turn_output, utc_ts
from .transport import GeminiACPTransport

_broadcast_fn: Optional[Callable[..., Any]] = None
_transcript_fn: Optional[Callable[..., Any]] = None
_meta_fns: Dict[str, Callable[..., Any]] = {}
_registered_extension_ids: set[str] = set()
_ready_extensions: set[str] = set()
_transport: Optional[GeminiACPTransport] = None
_EXTENSION_ROOT = Path(__file__).parent
_DEFAULT_APPROVAL_POLICY = "cancel"
_DEFAULT_SANDBOX_POLICY = "agent-default"
_APPROVAL_POLICY_OPTIONS = [
    {"value": "cancel", "label": "Cancel all requests"},
    {"value": "auto-approve", "label": "Auto-approve first allow option"},
]
_SANDBOX_POLICY_OPTIONS = [
    {"value": "agent-default", "label": "Gemini default (not normalized yet)"},
]


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
    raw = settings.get("approval_policy")
    if raw is None:
        raw = settings.get("approval_mode")
    value = str(raw or _DEFAULT_APPROVAL_POLICY).strip()
    allowed = {item["value"] for item in _APPROVAL_POLICY_OPTIONS}
    return value if value in allowed else _DEFAULT_APPROVAL_POLICY


def _normalize_sandbox_policy(settings: Dict[str, Any]) -> str:
    value = str(settings.get("sandbox_policy") or _DEFAULT_SANDBOX_POLICY).strip()
    allowed = {item["value"] for item in _SANDBOX_POLICY_OPTIONS}
    return value if value in allowed else _DEFAULT_SANDBOX_POLICY


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
    return {
        "agent": extension_id,
        "approval": _runtime_option_descriptor(
            "approval_policy",
            "Approval Policy",
            _APPROVAL_POLICY_OPTIONS,
            _normalize_approval_policy(merged),
            _DEFAULT_APPROVAL_POLICY,
        ),
        "sandbox": _runtime_option_descriptor(
            "sandbox_policy",
            "Directory Trust",
            _SANDBOX_POLICY_OPTIONS,
            _normalize_sandbox_policy(merged),
            _DEFAULT_SANDBOX_POLICY,
        ),
    }


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
                approval_policy=_DEFAULT_APPROVAL_POLICY,
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
    approval_policy = _normalize_approval_policy(merged_settings)
    debug_trace = bool(merged_settings.get("debug_trace"))
    meta = _load_meta(conversation_id)
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
    try:
        prompt_result = await transport.send_prompt(
            conversation_id=conversation_id,
            text=text,
            cwd=cwd,
            approval_policy=approval_policy,
            model=str(merged_settings.get("model") or "").strip() or None,
            message_id=user_message_id,
            existing_session_id=existing_session_id,
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

    routed = build_prompt_turn_output(
        conversation_id=conversation_id,
        session_id=prompt_result.session_id,
        turn_id=turn_id,
        updates=prompt_result.updates,
        stop_reason=prompt_result.stop_reason,
        debug_trace=debug_trace,
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
    _save_meta(conversation_id, meta)

    return {
        "ok": True,
        "session_id": prompt_result.session_id,
        "shell_id": transport.runtime_instance_id(),
        "stop_reason": prompt_result.stop_reason,
        "event_count": len(events) if isinstance(events, list) else 0,
        "transcript_entry_count": len(transcript_entries) if isinstance(transcript_entries, list) else 0,
        "scaffold": True,
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


async def shutdown_client() -> None:
    transport = _transport
    if transport is not None:
        await transport.stop()
