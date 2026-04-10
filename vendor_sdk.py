"""Helpers for resolving the vendored ACP Python SDK."""

from __future__ import annotations

import sys
from pathlib import Path

_EXTENSION_ROOT = Path(__file__).parent
_SDK_SRC = _EXTENSION_ROOT / "_vendor" / "python-sdk" / "src"


def ensure_sdk_on_path() -> Path:
    if not _SDK_SRC.is_dir():
        raise RuntimeError(f"ACP Python SDK source not found: {_SDK_SRC}")
    sdk_src_text = str(_SDK_SRC)
    if sdk_src_text not in sys.path:
        sys.path.insert(0, sdk_src_text)
    return _SDK_SRC


def sdk_src_path() -> Path:
    return _SDK_SRC
