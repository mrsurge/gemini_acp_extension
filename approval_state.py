"""Harness-side approval policy state machine for Gemini ACP.

ACP permission requests are stateless, so the harness overlays a small approval
model that matches the agreed Gemini UX:

- Footer/modal:
  - Always approve
  - Ask
  - Always reject
- Ask-card actions:
  - Approve once
  - Approve all (sticky within Ask for the rest of the session)
  - Reject

The sticky "Approve all while still in Ask" state is represented as
`policy="ask"` plus `ask_session_approved=True`.
"""

from __future__ import annotations

from typing import Any, Dict

POLICY_ALWAYS_APPROVE = "always_approve"
POLICY_ASK = "ask"
POLICY_ALWAYS_REJECT = "always_reject"

SETTING_ASK = "ask"
SETTING_ASK_CLEAR = "ask_clear"

VALID_POLICIES = {
    POLICY_ALWAYS_APPROVE,
    POLICY_ASK,
    POLICY_ALWAYS_REJECT,
}

META_KEY = "approval_policy_state"

DEFAULT_STATE: Dict[str, Any] = {
    "policy": POLICY_ALWAYS_REJECT,
    "ask_session_approved": False,
}

_SETTING_TO_STATE: Dict[str, Dict[str, Any]] = {
    POLICY_ALWAYS_APPROVE: {"policy": POLICY_ALWAYS_APPROVE, "ask_session_approved": False},
    POLICY_ASK: {"policy": POLICY_ASK, "ask_session_approved": False},
    SETTING_ASK_CLEAR: {"policy": POLICY_ASK, "ask_session_approved": False},
    POLICY_ALWAYS_REJECT: {"policy": POLICY_ALWAYS_REJECT, "ask_session_approved": False},
    # Legacy settings from the earlier Gemini scaffold.
    "ask_every_time": {"policy": POLICY_ASK, "ask_session_approved": False},
    "auto_approve_session": {"policy": POLICY_ALWAYS_APPROVE, "ask_session_approved": False},
    "auto_approve_once": {"policy": POLICY_ASK, "ask_session_approved": False},
    "auto_deny": {"policy": POLICY_ALWAYS_REJECT, "ask_session_approved": False},
    "cancel": {"policy": POLICY_ALWAYS_REJECT, "ask_session_approved": False},
    "auto-approve": {"policy": POLICY_ALWAYS_APPROVE, "ask_session_approved": False},
}


def normalize_state(raw: Any) -> Dict[str, Any]:
    """Return a sanitized approval state."""
    if not isinstance(raw, dict):
        return dict(DEFAULT_STATE)

    policy = str(raw.get("policy") or "").strip()
    session_approved = raw.get("ask_session_approved") is True
    if policy in VALID_POLICIES:
        if policy != POLICY_ASK:
            session_approved = False
        return {
            "policy": policy,
            "ask_session_approved": session_approved,
        }

    # Legacy mode/scope state from the previous implementation.
    legacy_mode = str(raw.get("mode") or "").strip()
    if legacy_mode == "ask_every_time":
        return {"policy": POLICY_ASK, "ask_session_approved": False}
    if legacy_mode == "auto_approve_session":
        return {"policy": POLICY_ALWAYS_APPROVE, "ask_session_approved": False}
    if legacy_mode == "auto_approve_once":
        return {"policy": POLICY_ASK, "ask_session_approved": False}
    if legacy_mode == "auto_deny":
        return {"policy": POLICY_ALWAYS_REJECT, "ask_session_approved": False}

    return dict(DEFAULT_STATE)


def state_from_setting(setting_value: Any) -> Dict[str, Any]:
    """Translate a stored settings value into an initial approval state."""
    key = str(setting_value or "").strip()
    mapped = _SETTING_TO_STATE.get(key)
    if mapped is not None:
        return dict(mapped)
    return dict(DEFAULT_STATE)


def apply_setting_override(state: Dict[str, Any], setting_value: Any) -> Dict[str, Any]:
    """Apply footer/modal settings over persisted meta state.

    `ask` preserves a sticky Ask-session grant if one is already active.
    `ask_clear` explicitly clears that sticky grant.
    """
    current = normalize_state(state)
    key = str(setting_value or "").strip()
    if not key:
        return current
    if key == POLICY_ASK:
        if current["policy"] == POLICY_ASK and current["ask_session_approved"] is True:
            return current
        return state_from_setting(key)
    return state_from_setting(key)


def setting_from_state(state: Dict[str, Any]) -> str:
    """Return the top-level stored footer setting for a state."""
    normalized = normalize_state(state)
    policy = normalized["policy"]
    if policy == POLICY_ALWAYS_APPROVE:
        return POLICY_ALWAYS_APPROVE
    if policy == POLICY_ALWAYS_REJECT:
        return POLICY_ALWAYS_REJECT
    return POLICY_ASK


def runtime_current_value(state: Dict[str, Any]) -> str:
    """Return the runtime-option current value for footer/modal display."""
    normalized = normalize_state(state)
    if normalized["policy"] == POLICY_ASK and normalized["ask_session_approved"] is True:
        return SETTING_ASK_CLEAR
    return setting_from_state(normalized)


def runtime_options_for_state(state: Dict[str, Any]) -> list[dict[str, str]]:
    """Return the dynamic footer/modal options for the current state."""
    normalized = normalize_state(state)
    ask_label = "Ask ✅" if normalized["policy"] == POLICY_ASK and normalized["ask_session_approved"] else "Ask"
    ask_value = SETTING_ASK_CLEAR if normalized["policy"] == POLICY_ASK and normalized["ask_session_approved"] else POLICY_ASK
    return [
        {"value": POLICY_ALWAYS_APPROVE, "label": "Always approve"},
        {"value": ask_value, "label": ask_label},
        {"value": POLICY_ALWAYS_REJECT, "label": "Always reject"},
    ]


def after_serving_request(state: Dict[str, Any]) -> Dict[str, Any]:
    """No request-boundary decay is needed in the current Gemini model."""
    return normalize_state(state)


def after_turn_end(state: Dict[str, Any]) -> Dict[str, Any]:
    """No turn-boundary decay is needed in the current Gemini model."""
    return normalize_state(state)
