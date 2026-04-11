"""Framework-shells transport scaffold for Gemini ACP over the vendored SDK."""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .bridge_client import GeminiACPBridgeClient
from .vendor_sdk import ensure_sdk_on_path

ensure_sdk_on_path()

from acp import PROTOCOL_VERSION, RequestError, connect_to_agent, text_block  # noqa: E402
from acp.schema import ClientCapabilities, Implementation  # noqa: E402

_TRANSPORT_LABEL = "gemini-acp:extension"
_MODEL_DISCOVERY_CONVERSATION_ID = "__gemini_acp_model_discovery__"
_SESSION_LIST_CONVERSATION_ID = "__gemini_acp_session_list__"


@dataclass(frozen=True)
class GeminiPromptResult:
    session_id: str
    stop_reason: str
    usage: Optional[dict[str, Any]]
    updates: list[dict[str, Any]]
    user_message_id: Optional[str] = None


LiveUpdateCallback = Callable[[dict[str, Any]], Any]


def _field_value(value: Any, *names: str) -> Any:
    if isinstance(value, dict):
        for name in names:
            if name in value:
                return value.get(name)
        return None
    for name in names:
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _string_value(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _normalize_model_entry(model: Any) -> Optional[dict[str, Any]]:
    model_id = _string_value(_field_value(model, "model_id", "modelId", "id", "value"))
    if not model_id:
        return None
    name = _string_value(_field_value(model, "name", "label")) or model_id
    description = _string_value(_field_value(model, "description"))
    entry: dict[str, Any] = {"id": model_id, "name": name}
    if description:
        entry["description"] = description
    return entry


def _dedupe_model_entries(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for entry in entries:
        model_id = _string_value(entry.get("id"))
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        deduped.append(dict(entry))
    return deduped


def _normalize_model_entries_from_state(model_state: Any) -> tuple[list[dict[str, Any]], Optional[str]]:
    current_model = _string_value(_field_value(model_state, "current_model_id", "currentModelId"))
    available_models = _field_value(model_state, "available_models", "availableModels")
    if not isinstance(available_models, list):
        return ([], current_model)
    entries = [entry for item in available_models if (entry := _normalize_model_entry(item))]
    return (_dedupe_model_entries(entries), current_model)


def _normalize_model_entries_from_select_options(options: Any) -> list[dict[str, Any]]:
    if not isinstance(options, list):
        return []
    entries: list[dict[str, Any]] = []
    for item in options:
        nested_options = _field_value(item, "options")
        if isinstance(nested_options, list):
            entries.extend(_normalize_model_entries_from_select_options(nested_options))
            continue
        entry = _normalize_model_entry(item)
        if entry:
            entries.append(entry)
    return _dedupe_model_entries(entries)


def _normalize_model_entries_from_config_options(config_options: Any) -> tuple[list[dict[str, Any]], Optional[str]]:
    if not isinstance(config_options, list):
        return ([], None)
    for option in config_options:
        option_id = _string_value(_field_value(option, "id"))
        option_type = _string_value(_field_value(option, "type"))
        if option_id != "model" or option_type != "select":
            continue
        current_model = _string_value(_field_value(option, "current_value", "currentValue"))
        options = _field_value(option, "options")
        return (_normalize_model_entries_from_select_options(options), current_model)
    return ([], None)


def _normalize_pathish(value: Any) -> str:
    text = _string_value(value)
    if not text:
        return ""
    return str(Path(text).expanduser())


def _cwd_relevance(session_cwd: str, target_cwd: str) -> int:
    normalized_session = _normalize_pathish(session_cwd)
    normalized_target = _normalize_pathish(target_cwd)
    if not normalized_session or not normalized_target:
        return 9
    if normalized_session == normalized_target:
        return 0
    target_prefix = normalized_target.rstrip("/") + "/"
    session_prefix = normalized_session.rstrip("/") + "/"
    if normalized_session.startswith(target_prefix) or normalized_target.startswith(session_prefix):
        return 1
    return 9


def _normalize_session_entry(session: Any, *, active: bool = False) -> Optional[dict[str, Any]]:
    session_id = _string_value(_field_value(session, "session_id", "sessionId", "id"))
    if not session_id:
        return None
    cwd = _string_value(_field_value(session, "cwd"))
    title = _string_value(_field_value(session, "title", "summary", "name"))
    updated_at = _string_value(_field_value(session, "updated_at", "updatedAt", "modified_time", "modifiedTime"))
    entry: dict[str, Any] = {
        "session_id": session_id,
        "summary": title or cwd or session_id,
    }
    if updated_at:
        entry["modified_time"] = updated_at
    if cwd:
        entry["context"] = {"cwd": cwd}
    if active:
        entry["active"] = True
    return entry


def _looks_like_cold_session_error(exc: Exception) -> bool:
    message = str(exc).strip().lower()
    if isinstance(exc, RequestError):
        if exc.code == -32002:
            return True
        payload = exc.data if isinstance(exc.data, dict) else None
        payload_text = json.dumps(payload, sort_keys=True).lower() if payload is not None else ""
        combined = " ".join(part for part in (message, payload_text) if part)
    else:
        combined = message
    if "session" not in combined and "thread" not in combined:
        return False
    cold_markers = (
        "not found",
        "not loaded",
        "unknown",
        "missing",
        "resource not found",
        "no such",
    )
    return any(marker in combined for marker in cold_markers)


class _DrainProtocol(asyncio.Protocol):
    def __init__(self) -> None:
        self._transport: Optional[_FWSPipeWriteTransport] = None

    def attach_transport(self, transport: "_FWSPipeWriteTransport") -> None:
        self._transport = transport

    async def _drain_helper(self) -> None:
        transport = self._transport
        if transport is None:
            return
        await transport.drain()


class _FWSPipeWriteTransport(asyncio.WriteTransport):
    def __init__(self, shell_id: str, fws_getter: Callable[[], Awaitable[Any]]) -> None:
        super().__init__()
        self._shell_id = shell_id
        self._fws_getter = fws_getter
        self._queue: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._closed = False
        self._error: BaseException | None = None
        self._worker_task = asyncio.create_task(self._worker_loop(), name=f"gemini-acp-writer-{shell_id[:8]}")

    def write(self, data: bytes | bytearray | memoryview) -> None:
        if self._closed:
            raise RuntimeError("Gemini ACP write transport is closed")
        if self._error is not None:
            raise RuntimeError("Gemini ACP write transport failed") from self._error
        self._queue.put_nowait(bytes(data))

    async def drain(self) -> None:
        await self._queue.join()
        if self._error is not None:
            raise RuntimeError("Gemini ACP write transport failed") from self._error

    def is_closing(self) -> bool:
        return self._closed

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put_nowait(None)

    def can_write_eof(self) -> bool:
        return False

    def write_eof(self) -> None:
        self.close()

    def abort(self) -> None:
        self.close()

    def get_extra_info(self, name: str, default: Any = None) -> Any:
        if name == "shell_id":
            return self._shell_id
        return default

    async def wait_closed(self) -> None:
        with contextlib.suppress(asyncio.CancelledError):
            await self._worker_task

    async def _worker_loop(self) -> None:
        while True:
            item = await self._queue.get()
            try:
                if item is None:
                    return
                mgr = await self._fws_getter()
                await mgr.write_to_pipe(self._shell_id, item.decode("utf-8"))
            except BaseException as exc:
                self._error = exc
                self._closed = True
                while True:
                    try:
                        leftover = self._queue.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    self._queue.task_done()
                    if leftover is None:
                        break
                return
            finally:
                self._queue.task_done()


class _FWSStreamBridge:
    def __init__(self, shell_id: str, fws_getter: Callable[[], Awaitable[Any]]) -> None:
        self._shell_id = shell_id
        self._fws_getter = fws_getter
        self._subscription: Any = None
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer_transport: Optional[_FWSPipeWriteTransport] = None
        self._reader_task: Optional[asyncio.Task[None]] = None
        self._closed = False

    async def start(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        mgr = await self._fws_getter()
        self._subscription = await mgr.subscribe_output_bytes(self._shell_id)
        self._reader = asyncio.StreamReader()
        protocol = _DrainProtocol()
        self._writer_transport = _FWSPipeWriteTransport(self._shell_id, self._fws_getter)
        protocol.attach_transport(self._writer_transport)
        loop = asyncio.get_running_loop()
        writer = asyncio.StreamWriter(self._writer_transport, protocol, self._reader, loop)
        self._reader_task = asyncio.create_task(
            self._reader_loop(self._subscription), name=f"gemini-acp-reader-{self._shell_id[:8]}"
        )
        return self._reader, writer

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        task = self._reader_task
        self._reader_task = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        if self._reader is not None and not self._reader.at_eof():
            self._reader.feed_eof()
        if self._writer_transport is not None:
            self._writer_transport.close()
            await self._writer_transport.wait_closed()

    async def _reader_loop(self, subscription: Any) -> None:
        mgr = await self._fws_getter()
        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(subscription.get(), timeout=1.0)
                except asyncio.TimeoutError:
                    state = mgr.get_pipe_state(self._shell_id)
                    if not state or state.process.returncode is not None:
                        break
                    continue
                if not chunk:
                    state = mgr.get_pipe_state(self._shell_id)
                    if not state or state.process.returncode is not None:
                        break
                    continue
                if self._reader is not None:
                    self._reader.feed_data(bytes(chunk))
        finally:
            with contextlib.suppress(Exception):
                await mgr.unsubscribe_output_bytes(self._shell_id, subscription)
            if self._reader is not None and not self._reader.at_eof():
                self._reader.feed_eof()


class GeminiACPTransport:
    def __init__(
        self,
        *,
        extension_root: Path,
        fws_getter: Callable[[], Awaitable[Any]],
    ) -> None:
        self._extension_root = extension_root
        self._fws_getter = fws_getter
        self._lock = asyncio.Lock()
        self._shell_id: Optional[str] = None
        self._bridge: Optional[_FWSStreamBridge] = None
        self._connection: Any = None
        self._client: Optional[GeminiACPBridgeClient] = None
        self._initialized = False
        self._launch_cwd = str(Path.home())
        self._session_ids_by_conversation: dict[str, str] = {}
        self._conversation_by_session: dict[str, str] = {}
        self._approval_policy_by_conversation: dict[str, str] = {}
        self._updates_by_conversation: dict[str, list[dict[str, Any]]] = {}
        self._model_options_by_conversation: dict[str, list[dict[str, Any]]] = {}
        self._current_model_by_conversation: dict[str, str] = {}
        self._agent_capabilities: Any = None
        self._capture_mode_by_conversation: dict[str, str] = {}
        self._live_update_callbacks: dict[str, LiveUpdateCallback] = {}
        self._live_update_tasks: dict[str, set[asyncio.Task[None]]] = {}
        self._suppressed_update_counts: dict[str, int] = {}
        self._suppressed_history_updates: set[str] = set()

    def is_ready(self) -> bool:
        return bool(self._shell_id and self._initialized and self._connection is not None and self._bridge is not None)

    def runtime_instance_id(self) -> Optional[str]:
        return self._shell_id

    def session_id_for(self, conversation_id: str) -> Optional[str]:
        return self._session_ids_by_conversation.get(conversation_id)

    def _session_capabilities(self) -> Any:
        capabilities = self._agent_capabilities
        if capabilities is None:
            return None
        return getattr(capabilities, "session_capabilities", None) or getattr(capabilities, "sessionCapabilities", None)

    def supports_load_session(self) -> bool:
        capabilities = self._agent_capabilities
        if capabilities is None:
            return False
        return bool(getattr(capabilities, "load_session", None) or getattr(capabilities, "loadSession", None))

    def supports_list_sessions(self) -> bool:
        session_capabilities = self._session_capabilities()
        if session_capabilities is None:
            return False
        return getattr(session_capabilities, "list", None) is not None

    def supports_resume_session(self) -> bool:
        session_capabilities = self._session_capabilities()
        if session_capabilities is None:
            return False
        return getattr(session_capabilities, "resume", None) is not None

    async def stop(self) -> None:
        async with self._lock:
            shell_id = self._shell_id
            self._shell_id = None
            await self._close_connection()
            self._initialized = False
            self._session_ids_by_conversation.clear()
            self._conversation_by_session.clear()
            self._approval_policy_by_conversation.clear()
            self._updates_by_conversation.clear()
            self._model_options_by_conversation.clear()
            self._current_model_by_conversation.clear()
            self._agent_capabilities = None
            self._capture_mode_by_conversation.clear()
            self._live_update_callbacks.clear()
            self._live_update_tasks.clear()
            self._suppressed_update_counts.clear()
            self._suppressed_history_updates.clear()
            if shell_id:
                mgr = await self._fws_getter()
                with contextlib.suppress(Exception):
                    await mgr.terminate_shell(shell_id, force=True)

    async def ensure_ready(self, *, conversation_id: str, cwd: str, approval_policy: str) -> None:
        async with self._lock:
            self._launch_cwd = cwd
            shell_id = await self._get_or_start_shell(conversation_id)
            if not await self._pipe_available(shell_id):
                shell_id = await self._restart_shell(shell_id, conversation_id)
            await self._ensure_connection(shell_id)
            self._approval_policy_by_conversation[conversation_id] = approval_policy

    def _remember_session_configuration(
        self,
        conversation_id: str,
        *,
        models: Any = None,
        config_options: Any = None,
    ) -> None:
        entries, current_model = _normalize_model_entries_from_state(models)
        if not entries:
            entries, config_current_model = _normalize_model_entries_from_config_options(config_options)
            if current_model is None:
                current_model = config_current_model
        if entries:
            self._model_options_by_conversation[conversation_id] = entries
        if current_model:
            self._current_model_by_conversation[conversation_id] = current_model

    def _known_model_options(self) -> list[dict[str, Any]]:
        collected: list[dict[str, Any]] = []
        for entries in self._model_options_by_conversation.values():
            if isinstance(entries, list):
                collected.extend(dict(item) for item in entries if isinstance(item, dict))
        return _dedupe_model_entries(collected)

    def _remember_session_binding(self, conversation_id: str, session_id: str) -> None:
        previous_session_id = self._session_ids_by_conversation.get(conversation_id)
        if previous_session_id and previous_session_id != session_id:
            self._conversation_by_session.pop(previous_session_id, None)
        previous_conversation_id = self._conversation_by_session.get(session_id)
        if previous_conversation_id and previous_conversation_id != conversation_id:
            self._session_ids_by_conversation.pop(previous_conversation_id, None)
        self._session_ids_by_conversation[conversation_id] = session_id
        self._conversation_by_session[session_id] = conversation_id

    def _route_session_to_conversation(self, conversation_id: str, session_id: str) -> None:
        if not conversation_id or not session_id:
            return
        self._conversation_by_session[session_id] = conversation_id

    def _set_capture_mode(self, conversation_id: str, mode: Optional[str]) -> None:
        if mode:
            self._capture_mode_by_conversation[conversation_id] = mode
            return
        self._capture_mode_by_conversation.pop(conversation_id, None)

    def _dispatch_live_update(self, conversation_id: str, payload: dict[str, Any]) -> None:
        callback = self._live_update_callbacks.get(conversation_id)
        if callback is None:
            return

        async def _runner() -> None:
            result = callback(dict(payload))
            if inspect.isawaitable(result):
                await result

        task = asyncio.create_task(_runner(), name=f"gemini-acp-live-update-{conversation_id[:8]}")
        task_set = self._live_update_tasks.setdefault(conversation_id, set())
        task_set.add(task)

        def _discard(done_task: asyncio.Task[None]) -> None:
            task_set.discard(done_task)
            if not task_set:
                self._live_update_tasks.pop(conversation_id, None)

        task.add_done_callback(_discard)

    async def _wait_for_live_update_tasks(self, conversation_id: str) -> None:
        while True:
            pending = tuple(self._live_update_tasks.get(conversation_id, ()))
            if not pending:
                return
            await asyncio.gather(*pending)

    async def _wait_for_count_quiescence(
        self,
        conversation_id: str,
        *,
        count_getter: Callable[[str], int],
        stable_iterations: int = 3,
        max_iterations: int = 12,
    ) -> None:
        stable = 0
        last_count = count_getter(conversation_id)
        for _ in range(max_iterations):
            await asyncio.sleep(0)
            current_count = count_getter(conversation_id)
            if current_count == last_count:
                stable += 1
                if stable >= stable_iterations:
                    return
                continue
            stable = 0
            last_count = current_count

    async def _apply_model_selection(
        self,
        *,
        conversation_id: str,
        session_id: str,
        model: Optional[str],
    ) -> None:
        desired_model = _string_value(model)
        if not desired_model or self._connection is None:
            return
        current_model = self._current_model_by_conversation.get(conversation_id)
        if desired_model == current_model:
            return
        config_error: Optional[Exception] = None
        try:
            response = await self._connection.set_config_option(
                config_id="model",
                session_id=session_id,
                value=desired_model,
            )
            self._remember_session_configuration(
                conversation_id,
                config_options=getattr(response, "config_options", None),
            )
            current_model = self._current_model_by_conversation.get(conversation_id)
            if current_model == desired_model:
                return
        except Exception as exc:
            config_error = exc
        try:
            await self._connection.set_session_model(model_id=desired_model, session_id=session_id)
            self._current_model_by_conversation[conversation_id] = desired_model
        except Exception as exc:
            if config_error is not None:
                raise RuntimeError(
                    f"Failed to select Gemini model {desired_model}: {config_error}; fallback set_session_model also failed: {exc}"
                ) from exc
            raise RuntimeError(f"Failed to select Gemini model {desired_model}: {exc}") from exc

    async def _bind_existing_session(
        self,
        *,
        conversation_id: str,
        session_id: str,
        cwd: str,
        model: Optional[str] = None,
    ) -> str:
        if self._connection is None:
            raise RuntimeError("Gemini ACP connection not initialized")
        self._remember_session_binding(conversation_id, session_id)
        bind_response: Any = None
        resume_error: Optional[Exception] = None
        if self.supports_resume_session():
            try:
                bind_response = await self._connection.resume_session(
                    cwd=cwd,
                    session_id=session_id,
                    mcp_servers=[],
                )
            except Exception as exc:
                resume_error = exc
        if bind_response is None:
            try:
                bind_response = await self._connection.load_session(
                    cwd=cwd,
                    session_id=session_id,
                    mcp_servers=[],
                )
            except Exception as exc:
                if resume_error is not None:
                    raise RuntimeError(
                        f"Gemini ACP session resume failed: {resume_error}; session load fallback also failed: {exc}"
                    ) from exc
                raise RuntimeError(f"Gemini ACP session load failed: {exc}") from exc
        self._remember_session_configuration(
            conversation_id,
            models=getattr(bind_response, "models", None),
            config_options=getattr(bind_response, "config_options", None),
        )
        await self._apply_model_selection(
            conversation_id=conversation_id,
            session_id=session_id,
            model=model,
        )
        return session_id

    async def _resume_cold_session(
        self,
        *,
        conversation_id: str,
        session_id: str,
        cwd: str,
        model: Optional[str] = None,
    ) -> str:
        if self._connection is None:
            raise RuntimeError("Gemini ACP connection not initialized")
        self._route_session_to_conversation(conversation_id, session_id)
        bind_response: Any = None
        resume_error: Optional[Exception] = None
        self._set_capture_mode(conversation_id, "suppress")
        self._suppressed_update_counts[conversation_id] = 0
        self._suppressed_history_updates.add(conversation_id)
        try:
            if self.supports_resume_session():
                try:
                    bind_response = await self._connection.resume_session(
                        cwd=cwd,
                        session_id=session_id,
                        mcp_servers=[],
                    )
                except Exception as exc:
                    resume_error = exc
            if bind_response is None:
                try:
                    bind_response = await self._connection.load_session(
                        cwd=cwd,
                        session_id=session_id,
                        mcp_servers=[],
                    )
                except Exception as exc:
                    if resume_error is not None:
                        raise RuntimeError(
                            f"Gemini ACP session resume failed: {resume_error}; session load fallback also failed: {exc}"
                        ) from exc
                    raise RuntimeError(f"Gemini ACP session load failed: {exc}") from exc
            await self._wait_for_count_quiescence(
                conversation_id,
                count_getter=lambda cid: self._suppressed_update_counts.get(cid, 0),
            )
        finally:
            self._set_capture_mode(conversation_id, None)
            self._suppressed_update_counts.pop(conversation_id, None)
            self._suppressed_history_updates.discard(conversation_id)
        self._remember_session_binding(conversation_id, session_id)
        self._remember_session_configuration(
            conversation_id,
            models=getattr(bind_response, "models", None),
            config_options=getattr(bind_response, "config_options", None),
        )
        await self._apply_model_selection(
            conversation_id=conversation_id,
            session_id=session_id,
            model=model,
        )
        return session_id

    async def _prompt_once(
        self,
        *,
        conversation_id: str,
        session_id: str,
        text: str,
        message_id: Optional[str],
        on_update: Optional[LiveUpdateCallback] = None,
    ) -> GeminiPromptResult:
        if self._connection is None:
            raise RuntimeError("Gemini ACP connection not initialized")
        self._updates_by_conversation[conversation_id] = []
        self._set_capture_mode(conversation_id, "prompt")
        self._live_update_tasks.pop(conversation_id, None)
        if on_update is not None:
            self._live_update_callbacks[conversation_id] = on_update
        else:
            self._live_update_callbacks.pop(conversation_id, None)
        try:
            response = await self._connection.prompt(
                session_id=session_id,
                prompt=[text_block(text)],
                message_id=message_id,
            )
        except Exception:
            self._live_update_callbacks.pop(conversation_id, None)
            self._set_capture_mode(conversation_id, None)
            self._updates_by_conversation.pop(conversation_id, None)
            raise
        await self._wait_for_count_quiescence(
            conversation_id,
            count_getter=lambda cid: len(self._updates_by_conversation.get(cid, [])),
        )
        await self._wait_for_live_update_tasks(conversation_id)
        self._live_update_callbacks.pop(conversation_id, None)
        self._set_capture_mode(conversation_id, None)
        updates = list(self._updates_by_conversation.pop(conversation_id, []))
        self._remember_session_configuration(
            conversation_id,
            models=getattr(response, "models", None),
            config_options=getattr(response, "config_options", None),
        )
        usage_payload: Optional[dict[str, Any]] = None
        response_usage = getattr(response, "usage", None)
        if response_usage is not None:
            if hasattr(response_usage, "model_dump"):
                usage_payload = response_usage.model_dump(mode="json", by_alias=True)
            elif isinstance(response_usage, dict):
                usage_payload = dict(response_usage)
        stop_reason_raw = getattr(response, "stop_reason", None)
        if not isinstance(stop_reason_raw, str) or not stop_reason_raw.strip():
            stop_reason_raw = getattr(response, "stopReason", None)
        stop_reason = stop_reason_raw.strip() if isinstance(stop_reason_raw, str) and stop_reason_raw.strip() else ""
        response_user_message_id = _string_value(
            getattr(response, "user_message_id", None) or getattr(response, "userMessageId", None)
        )
        return GeminiPromptResult(
            session_id=session_id,
            stop_reason=stop_reason,
            usage=usage_payload,
            updates=updates,
            user_message_id=response_user_message_id,
        )

    async def ensure_session(
        self,
        *,
        conversation_id: str,
        cwd: str,
        approval_policy: str,
        model: Optional[str] = None,
        existing_session_id: Optional[str] = None,
    ) -> str:
        await self.ensure_ready(conversation_id=conversation_id, cwd=cwd, approval_policy=approval_policy)
        async with self._lock:
            if self._connection is None:
                raise RuntimeError("Gemini ACP connection not initialized")
            bound_session_id = _string_value(existing_session_id)
            session_id = self._session_ids_by_conversation.get(conversation_id)
            if bound_session_id and session_id != bound_session_id:
                return await self._bind_existing_session(
                    conversation_id=conversation_id,
                    session_id=bound_session_id,
                    cwd=cwd,
                    model=model,
                )
            if not session_id:
                session = await self._connection.new_session(cwd=cwd, mcp_servers=[])
                session_id = str(session.session_id)
                self._remember_session_binding(conversation_id, session_id)
                self._remember_session_configuration(
                    conversation_id,
                    models=getattr(session, "models", None),
                    config_options=getattr(session, "config_options", None),
                )
            await self._apply_model_selection(
                conversation_id=conversation_id,
                session_id=session_id,
                model=model,
            )
            return session_id

    async def list_models(self, *, cwd: str) -> list[dict[str, Any]]:
        known = self._known_model_options()
        if known:
            return known
        await self.ensure_session(
            conversation_id=_MODEL_DISCOVERY_CONVERSATION_ID,
            cwd=cwd,
            approval_policy="cancel",
        )
        async with self._lock:
            return self._known_model_options()

    async def list_sessions(self, *, cwd: Optional[str] = None) -> list[dict[str, Any]]:
        target_cwd = _normalize_pathish(cwd) or str(Path.home())
        await self.ensure_ready(
            conversation_id=_SESSION_LIST_CONVERSATION_ID,
            cwd=target_cwd,
            approval_policy="cancel",
        )
        async with self._lock:
            if self._connection is None:
                raise RuntimeError("Gemini ACP connection not initialized")
            cursor: Optional[str] = None
            items: list[dict[str, Any]] = []
            while True:
                response = await self._connection.list_sessions(cursor=cursor, cwd=target_cwd if cwd else None)
                sessions = getattr(response, "sessions", None)
                if isinstance(sessions, list):
                    for session in sessions:
                        entry = _normalize_session_entry(
                            session,
                            active=_string_value(_field_value(session, "session_id", "sessionId", "id"))
                            in self._conversation_by_session,
                        )
                        if entry:
                            items.append(entry)
                cursor = _string_value(getattr(response, "next_cursor", None) or getattr(response, "nextCursor", None))
                if not cursor:
                    break
            items.sort(key=lambda item: str(item.get("modified_time") or ""), reverse=True)
            if cwd:
                items.sort(
                    key=lambda item: _cwd_relevance(
                        _field_value(item.get("context") or {}, "cwd") or "",
                        target_cwd,
                    )
                )
            return items

    async def bind_session(
        self,
        *,
        conversation_id: str,
        session_id: str,
        cwd: str,
        approval_policy: str,
        model: Optional[str] = None,
    ) -> str:
        await self.ensure_ready(conversation_id=conversation_id, cwd=cwd, approval_policy=approval_policy)
        async with self._lock:
            return await self._bind_existing_session(
                conversation_id=conversation_id,
                session_id=session_id,
                cwd=cwd,
                model=model,
            )

    async def load_session_history(
        self,
        *,
        conversation_id: str,
        session_id: str,
        cwd: str,
        approval_policy: str,
    ) -> list[dict[str, Any]]:
        await self.ensure_ready(conversation_id=conversation_id, cwd=cwd, approval_policy=approval_policy)
        async with self._lock:
            if self._connection is None:
                raise RuntimeError("Gemini ACP connection not initialized")
            self._remember_session_binding(conversation_id, session_id)
            self._updates_by_conversation[conversation_id] = []
            self._set_capture_mode(conversation_id, "history")
            try:
                response = await self._connection.load_session(
                    cwd=cwd,
                    session_id=session_id,
                    mcp_servers=[],
                )
            except Exception:
                self._set_capture_mode(conversation_id, None)
                self._updates_by_conversation.pop(conversation_id, None)
                raise
            await self._wait_for_count_quiescence(
                conversation_id,
                count_getter=lambda cid: len(self._updates_by_conversation.get(cid, [])),
            )
            self._set_capture_mode(conversation_id, None)
            updates = list(self._updates_by_conversation.pop(conversation_id, []))
            self._remember_session_configuration(
                conversation_id,
                models=getattr(response, "models", None),
                config_options=getattr(response, "config_options", None),
            )
            return updates

    async def send_prompt(
        self,
        *,
        conversation_id: str,
        text: str,
        cwd: str,
        approval_policy: str,
        model: Optional[str] = None,
        message_id: Optional[str] = None,
        existing_session_id: Optional[str] = None,
        on_update: Optional[LiveUpdateCallback] = None,
    ) -> GeminiPromptResult:
        await self.ensure_ready(conversation_id=conversation_id, cwd=cwd, approval_policy=approval_policy)
        async with self._lock:
            if self._connection is None:
                raise RuntimeError("Gemini ACP connection not initialized")
            bound_session_id = _string_value(existing_session_id)
            live_session_id = self._session_ids_by_conversation.get(conversation_id)
            cold_bound_session = bool(bound_session_id and live_session_id != bound_session_id)
            if cold_bound_session and bound_session_id:
                session_id = bound_session_id
                self._route_session_to_conversation(conversation_id, session_id)
            else:
                session_id = live_session_id
                if not session_id:
                    session = await self._connection.new_session(cwd=cwd, mcp_servers=[])
                    session_id = str(session.session_id)
                    self._remember_session_binding(conversation_id, session_id)
                    self._remember_session_configuration(
                        conversation_id,
                        models=getattr(session, "models", None),
                        config_options=getattr(session, "config_options", None),
                    )
                await self._apply_model_selection(
                    conversation_id=conversation_id,
                    session_id=session_id,
                    model=model,
                )
            try:
                result = await self._prompt_once(
                    conversation_id=conversation_id,
                    session_id=session_id,
                    text=text,
                    message_id=message_id,
                    on_update=on_update,
                )
            except Exception as exc:
                if not (cold_bound_session and bound_session_id and _looks_like_cold_session_error(exc)):
                    raise
                self._updates_by_conversation.pop(conversation_id, None)
                resumed_session_id = await self._resume_cold_session(
                    conversation_id=conversation_id,
                    session_id=bound_session_id,
                    cwd=cwd,
                    model=model,
                )
                result = await self._prompt_once(
                    conversation_id=conversation_id,
                    session_id=resumed_session_id,
                    text=text,
                    message_id=message_id,
                    on_update=on_update,
                )
            else:
                if cold_bound_session and bound_session_id:
                    self._remember_session_binding(conversation_id, bound_session_id)
            return result

    def _approval_policy_for_session(self, session_id: str) -> str:
        conversation_id = self._conversation_by_session.get(session_id)
        if not conversation_id:
            return "cancel"
        return self._approval_policy_by_conversation.get(conversation_id, "cancel")

    def _handle_update(self, session_id: str, payload: dict[str, Any]) -> None:
        conversation_id = self._conversation_by_session.get(session_id)
        if not conversation_id:
            return
        kind = _string_value(payload.get("sessionUpdate")) or _string_value(payload.get("session_update")) or ""
        if kind == "config_option_update":
            self._remember_session_configuration(
                conversation_id,
                config_options=payload.get("configOptions"),
            )
        capture_mode = self._capture_mode_by_conversation.get(conversation_id)
        if capture_mode == "suppress":
            self._suppressed_update_counts[conversation_id] = self._suppressed_update_counts.get(conversation_id, 0) + 1
            return
        if capture_mode == "history":
            self._updates_by_conversation.setdefault(conversation_id, []).append(payload)
            return
        if capture_mode == "prompt":
            self._updates_by_conversation.setdefault(conversation_id, []).append(payload)
            self._dispatch_live_update(conversation_id, payload)
            return

    async def _close_connection(self) -> None:
        if self._connection is not None:
            with contextlib.suppress(Exception):
                await self._connection.close()
        self._connection = None
        self._client = None
        self._agent_capabilities = None
        self._capture_mode_by_conversation.clear()
        self._live_update_callbacks.clear()
        self._live_update_tasks.clear()
        self._suppressed_update_counts.clear()
        bridge = self._bridge
        self._bridge = None
        if bridge is not None:
            await bridge.close()

    async def _ensure_connection(self, shell_id: str) -> None:
        if self._initialized and self._connection is not None and self._bridge is not None:
            return
        await self._close_connection()
        bridge = _FWSStreamBridge(shell_id, self._fws_getter)
        reader, writer = await bridge.start()
        client = GeminiACPBridgeClient(
            on_update=self._handle_update,
            approval_policy_resolver=self._approval_policy_for_session,
        )
        connection = connect_to_agent(client, writer, reader, use_unstable_protocol=True)
        initialize_response = await connection.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(),
            client_info=Implementation(name="gemini-acp-extension", title="Gemini ACP Extension", version="0.1.0"),
        )
        self._bridge = bridge
        self._client = client
        self._connection = connection
        self._agent_capabilities = (
            getattr(initialize_response, "agent_capabilities", None)
            or getattr(initialize_response, "agentCapabilities", None)
        )
        self._initialized = True

    async def _pipe_available(self, shell_id: str) -> bool:
        mgr = await self._fws_getter()
        state = mgr.get_pipe_state(shell_id)
        return bool(state and state.process.stdin)

    async def _get_or_start_shell(self, conversation_id: str) -> str:
        mgr = await self._fws_getter()
        if self._shell_id:
            shell = await mgr.get_shell(self._shell_id)
            if shell and shell.status == "running" and getattr(shell, "spec_id", "") == "gemini_acp_observed":
                return self._shell_id
            self._shell_id = None
        adopted = await self._adopt_existing_shell(mgr)
        if adopted:
            self._set_shell(adopted)
            return adopted
        shell_id = await self._start_new_shell(mgr, conversation_id)
        self._set_shell(shell_id)
        return shell_id

    async def _adopt_existing_shell(self, mgr: Any) -> Optional[str]:
        try:
            records = await mgr.list_shells()
        except Exception:
            return None
        for record in records:
            if record.status != "running":
                continue
            if (record.label or "") != _TRANSPORT_LABEL:
                continue
            if getattr(record, "spec_id", "") != "gemini_acp_observed":
                continue
            return str(record.id)
        return None

    async def _start_new_shell(self, mgr: Any, conversation_id: str) -> str:
        import importlib

        orchestrator_mod = importlib.import_module("framework_shells.orchestrator")
        Orchestrator = getattr(orchestrator_mod, "Orchestrator")

        spec_path = self._extension_root / "shellspec" / "gemini_acp.yaml"
        orch = Orchestrator(mgr)
        shell = await orch.start_from_ref(
            f"{spec_path}#gemini_acp_observed",
            base_dir=spec_path.parent,
            ctx={"CWD": self._launch_cwd, "CONVERSATION_ID": conversation_id},
            label=_TRANSPORT_LABEL,
            wait_ready=False,
        )
        return str(shell.id)

    async def _restart_shell(self, shell_id: str, conversation_id: str) -> str:
        mgr = await self._fws_getter()
        await self._close_connection()
        self._initialized = False
        self._session_ids_by_conversation.clear()
        self._conversation_by_session.clear()
        self._approval_policy_by_conversation.clear()
        self._updates_by_conversation.clear()
        self._model_options_by_conversation.clear()
        self._current_model_by_conversation.clear()
        self._agent_capabilities = None
        self._capture_mode_by_conversation.clear()
        self._live_update_callbacks.clear()
        self._live_update_tasks.clear()
        self._suppressed_update_counts.clear()
        self._suppressed_history_updates.clear()
        with contextlib.suppress(Exception):
            await mgr.terminate_shell(shell_id, force=True)
        new_shell_id = await self._start_new_shell(mgr, conversation_id)
        self._set_shell(new_shell_id)
        return new_shell_id

    def _set_shell(self, shell_id: str) -> None:
        if self._shell_id == shell_id:
            return
        self._shell_id = shell_id
        self._initialized = False
        self._session_ids_by_conversation.clear()
        self._conversation_by_session.clear()
        self._approval_policy_by_conversation.clear()
        self._updates_by_conversation.clear()
        self._model_options_by_conversation.clear()
        self._current_model_by_conversation.clear()
        self._agent_capabilities = None
        self._capture_mode_by_conversation.clear()
        self._live_update_callbacks.clear()
        self._live_update_tasks.clear()
        self._suppressed_update_counts.clear()
        self._suppressed_history_updates.clear()
