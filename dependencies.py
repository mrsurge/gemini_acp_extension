"""Dependency checks for the Gemini ACP scaffold."""

from __future__ import annotations

import shutil
from typing import Any, Dict, Optional

from .vendor_sdk import sdk_src_path


def _gemini_binary_path() -> str | None:
    return shutil.which("gemini")


async def check_dependencies(*, extension_id: str, extension_info: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    del extension_id, extension_info
    binary = _gemini_binary_path()
    sdk_src = sdk_src_path()
    if not sdk_src.is_dir():
        return {
            "ok": False,
            "status": "unmet",
            "message": f"ACP Python SDK source not found at {sdk_src}",
            "details": {"sdk_src": str(sdk_src)},
        }
    if not binary:
        return {
            "ok": False,
            "status": "unmet",
            "message": "gemini not found on PATH",
            "details": {"sdk_src": str(sdk_src)},
        }
    return {
        "ok": True,
        "status": "met",
        "message": f"gemini available at {binary}",
        "details": {"binary": binary, "sdk_src": str(sdk_src)},
    }
