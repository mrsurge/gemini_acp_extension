"""ACP SDK client bridge for the Gemini ACP scaffold."""

from __future__ import annotations

from typing import Any, Callable, Optional

from .vendor_sdk import ensure_sdk_on_path

ensure_sdk_on_path()

from acp import RequestError  # noqa: E402
from acp.schema import AllowedOutcome, DeniedOutcome, RequestPermissionResponse  # noqa: E402

UpdateCallback = Callable[[str, dict[str, Any]], None]
ApprovalPolicyResolver = Callable[[str], str]


class GeminiACPBridgeClient:
    def __init__(
        self,
        *,
        on_update: Optional[UpdateCallback] = None,
        approval_policy_resolver: Optional[ApprovalPolicyResolver] = None,
    ) -> None:
        self._conn: Any = None
        self._on_update = on_update
        self._approval_policy_resolver = approval_policy_resolver or (lambda _session_id: "cancel")

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        del kwargs
        if self._on_update is None:
            return
        if hasattr(update, "model_dump"):
            payload = update.model_dump(mode="json", by_alias=True)
        else:
            payload = {"raw": update}
        self._on_update(session_id, payload)

    async def request_permission(self, options: list[Any], session_id: str, tool_call: Any, **kwargs: Any) -> RequestPermissionResponse:
        del tool_call, kwargs
        approval_policy = self._approval_policy_resolver(session_id)
        if approval_policy == "auto-approve":
            preferred = _pick_preferred_option(options)
            if preferred is not None:
                return RequestPermissionResponse(
                    outcome=AllowedOutcome(option_id=str(preferred.option_id), outcome="selected")
                )
        return RequestPermissionResponse(outcome=DeniedOutcome(outcome="cancelled"))

    async def write_text_file(self, content: str, path: str, session_id: str, **kwargs: Any) -> Any:
        del content, path, session_id, kwargs
        raise RequestError.method_not_found("fs/write_text_file")

    async def read_text_file(self, path: str, session_id: str, limit: int | None = None, line: int | None = None, **kwargs: Any) -> Any:
        del path, session_id, limit, line, kwargs
        raise RequestError.method_not_found("fs/read_text_file")

    async def create_terminal(
        self,
        command: str,
        session_id: str,
        args: list[str] | None = None,
        cwd: str | None = None,
        env: list[Any] | None = None,
        output_byte_limit: int | None = None,
        **kwargs: Any,
    ) -> Any:
        del command, session_id, args, cwd, env, output_byte_limit, kwargs
        raise RequestError.method_not_found("terminal/create")

    async def terminal_output(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        del session_id, terminal_id, kwargs
        raise RequestError.method_not_found("terminal/output")

    async def release_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        del session_id, terminal_id, kwargs
        raise RequestError.method_not_found("terminal/release")

    async def wait_for_terminal_exit(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        del session_id, terminal_id, kwargs
        raise RequestError.method_not_found("terminal/wait_for_exit")

    async def kill_terminal(self, session_id: str, terminal_id: str, **kwargs: Any) -> Any:
        del session_id, terminal_id, kwargs
        raise RequestError.method_not_found("terminal/kill")

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        del params
        raise RequestError.method_not_found(f"_{method}")

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        del method, params


def _pick_preferred_option(options: list[Any]) -> Any | None:
    for option in options:
        option_kind = str(getattr(option, "kind", "")).strip().lower()
        if option_kind in {"allow_once", "allow", "allow_always", "allow_always_and_save"}:
            return option
    return options[0] if options else None
