"""Gemini ACP scaffold client module for multi-root loading tests."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from .dependencies import check_dependencies
from .transport import GeminiACPTransport

_broadcast_fn: Optional[Callable[..., Any]] = None
_transcript_fn: Optional[Callable[..., Any]] = None
_meta_fns: Dict[str, Callable[..., Any]] = {}
_registered_extension_ids: set[str] = set()
_ready_extensions: set[str] = set()
_transport: Optional[GeminiACPTransport] = None
_EXTENSION_ROOT = Path(__file__).parent


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


async def get_settings_schema(extension_id: str) -> Dict[str, Any]:
    del extension_id
    return json.loads(_settings_schema_path().read_text(encoding="utf-8"))


async def get_splash_schema(extension_id: str) -> Dict[str, Any]:
    return await get_settings_schema(extension_id)


async def list_models() -> List[Dict[str, Any]]:
    schema = await get_settings_schema("gemini-acp")
    fields_obj = schema.get("fields") if isinstance(schema, dict) else []
    fields = fields_obj if isinstance(fields_obj, list) else []
    for field in fields:
        if isinstance(field, dict) and field.get("id") == "model":
            options_obj = field.get("options")
            options = options_obj if isinstance(options_obj, list) else []
            return [
                {"id": option.get("value", ""), "displayName": option.get("label", option.get("value", ""))}
                for option in options
                if isinstance(option, dict)
            ]
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
                approval_mode="cancel",
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
    del agent_type
    transport = _transport
    if transport is None:
        return {"ok": False, "error": "Gemini ACP transport not initialized"}
    if not conversation_id or not text.strip():
        return {"ok": False, "error": "conversation_id and text required"}
    cwd = str(settings.get("cwd") or Path.home())
    approval_mode = str(settings.get("approval_mode") or "cancel")
    try:
        session_id = await transport.send_prompt(
            conversation_id=conversation_id,
            text=text,
            cwd=cwd,
            approval_mode=approval_mode,
        )
    except Exception as exc:
        return {"ok": False, "error": str(exc)}

    _ready_extensions.add("gemini-acp")

    load_meta = _meta_fns.get("load")
    save_meta = _meta_fns.get("save")
    if callable(load_meta) and callable(save_meta):
        meta = load_meta(conversation_id)
        if not isinstance(meta, dict):
            meta = {}
        meta["gemini_acp_session_id"] = session_id
        meta["gemini_acp_shell_id"] = transport.runtime_instance_id()
        meta["status"] = "active"
        save_meta(conversation_id, meta)

    return {
        "ok": True,
        "session_id": session_id,
        "shell_id": transport.runtime_instance_id(),
        "scaffold": True,
    }


async def shutdown_client() -> None:
    transport = _transport
    if transport is not None:
        await transport.stop()
