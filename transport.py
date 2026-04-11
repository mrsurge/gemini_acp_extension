"""Framework-shells transport scaffold for Gemini ACP over the vendored SDK."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .bridge_client import GeminiACPBridgeClient
from .vendor_sdk import ensure_sdk_on_path

ensure_sdk_on_path()

from acp import PROTOCOL_VERSION, connect_to_agent, text_block  # noqa: E402
from acp.schema import ClientCapabilities, Implementation  # noqa: E402

_TRANSPORT_LABEL = "gemini-acp:extension"


@dataclass(frozen=True)
class GeminiPromptResult:
    session_id: str
    stop_reason: str
    usage: Optional[dict[str, Any]]
    updates: list[dict[str, Any]]


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

    def is_ready(self) -> bool:
        return bool(self._shell_id and self._initialized and self._connection is not None and self._bridge is not None)

    def runtime_instance_id(self) -> Optional[str]:
        return self._shell_id

    def session_id_for(self, conversation_id: str) -> Optional[str]:
        return self._session_ids_by_conversation.get(conversation_id)

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

    async def send_prompt(self, *, conversation_id: str, text: str, cwd: str, approval_policy: str) -> GeminiPromptResult:
        await self.ensure_ready(conversation_id=conversation_id, cwd=cwd, approval_policy=approval_policy)
        async with self._lock:
            if self._connection is None:
                raise RuntimeError("Gemini ACP connection not initialized")
            session_id = self._session_ids_by_conversation.get(conversation_id)
            if not session_id:
                session = await self._connection.new_session(cwd=cwd, mcp_servers=[])
                session_id = str(session.session_id)
                self._session_ids_by_conversation[conversation_id] = session_id
                self._conversation_by_session[session_id] = conversation_id
            self._updates_by_conversation[conversation_id] = []
            try:
                response = await self._connection.prompt(session_id=session_id, prompt=[text_block(text)])
            except Exception:
                self._updates_by_conversation.pop(conversation_id, None)
                raise
            updates = list(self._updates_by_conversation.pop(conversation_id, []))
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
            return GeminiPromptResult(
                session_id=session_id,
                stop_reason=stop_reason,
                usage=usage_payload,
                updates=updates,
            )

    def _approval_policy_for_session(self, session_id: str) -> str:
        conversation_id = self._conversation_by_session.get(session_id)
        if not conversation_id:
            return "cancel"
        return self._approval_policy_by_conversation.get(conversation_id, "cancel")

    def _handle_update(self, session_id: str, payload: dict[str, Any]) -> None:
        conversation_id = self._conversation_by_session.get(session_id)
        if not conversation_id:
            return
        self._updates_by_conversation.setdefault(conversation_id, []).append(payload)

    async def _close_connection(self) -> None:
        if self._connection is not None:
            with contextlib.suppress(Exception):
                await self._connection.close()
        self._connection = None
        self._client = None
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
        connection = connect_to_agent(client, writer, reader)
        await connection.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=ClientCapabilities(),
            client_info=Implementation(name="gemini-acp-extension", title="Gemini ACP Extension", version="0.1.0"),
        )
        self._bridge = bridge
        self._client = client
        self._connection = connection
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
