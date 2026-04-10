# Gemini ACP Minimal Contract (Draft)

This document defines the smallest useful contract for the Gemini ACP extension.

It is intentionally narrower than the full `copilot-sdk` or `codex-ext` implementations. The goal is to give the editable/symlink installer and the next implementation pass a stable target without overcommitting to unfinished Gemini-specific behavior.

## 1. Scope

This draft exists to support:

- editable/symlink extension install testing
- multi-root extension loading
- a minimal but real Gemini ACP send/receive path
- schema-driven settings owned by the extension
- live/replay parity for the first set of user-visible semantics

This draft does not promise full backend feature parity yet.

## 2. Non-negotiable repo contracts

The Gemini extension must follow the same repo-level rules as the existing extensions:

- extension-specific logic stays inside the extension root
- `server.py`, `static/codex_agent.js`, and `static/modals/settings_schema.js` remain platform-agnostic
- local `conversation_id` is our UI/transcript container id; remote `session_id` is only the backend binding handle
- local `transcript.jsonl` is the source of truth for replay
- if a live UI surface depends on a field later, replay must see the same semantic field later
- runtime UI/backend behavior stays on the existing Socket.IO contract; do not add HTTP fallback flows for runtime behavior

In practice, this means Gemini-specific transport details may stay in `transport.py` or `bridge_client.py`, but Gemini-specific card shapes, settings save logic, or frontend branching must not leak into shared platform files.

## 3. Minimal extension shape

The editable-installed Gemini extension should remain self-contained under its root and should be safe to symlink into the user extension area.

Minimum extension-owned files:

- `manifest.json`
- `client.py`
- `transport.py`
- `router.py`
- `settings_schema.json`
- `dependencies.py`
- `shellspec/gemini_acp.yaml`

Expected ownership:

- `manifest.json` declares capabilities conservatively
- `client.py` owns manager init, send path entry, and metadata persistence
- `transport.py` owns shell lifecycle, ACP session lifecycle, and SDK attachment
- `router.py` owns live-event to transcript/UI translation once streaming/live updates are exposed
- `settings_schema.json` or `get_settings_schema()` owns the extension settings surface
- `dependencies.py` reports whether the Gemini CLI and vendored SDK are actually available

Relative imports inside the extension package are fine. The extension should not rely on parent-relative imports into builtin `extensions.*`.

## 4. Session and binding semantics

The local conversation remains the primary unit of UI state.

- one local `conversation_id` may bind to one remote Gemini ACP `session_id`
- local replay uses `transcript.jsonl`, not remote Gemini history
- remote history should only matter later if we explicitly implement session browse/resume or port-in
- the extension should keep `sessionResume: false` until session listing, bind, and transcript hydration are real

Minimum expected send flow:

1. ensure the observed `gemini --acp` shell exists
2. attach the ACP Python SDK with `connect_to_agent(...)`
3. call `initialize(...)`
4. lazily create a remote session for the local conversation if needed
5. send the prompt
6. translate returned updates into generic local semantics

## 5. Minimal messaging semantics

The extension should normalize Gemini ACP events into shared local semantics instead of exposing raw provider event names directly to the frontend.

### 5.1 `session_update` is transport input, not a frontend contract

Gemini ACP `session_update` payloads are upstream transport events. They are not the final transcript/UI schema.

The extension should translate them into a small stable semantic set:

- assistant message content
- permission request / approval flow
- tool activity or tool result rows when applicable
- status or usage updates when they become stable enough to expose
- warning
- error

Optional raw debug capture may still exist, but it belongs on an internal/debug lane, not the normal conversation contract.

### 5.2 Minimal user-visible semantic set

The first implementation pass should support these semantics:

1. **User message**
   - written through the existing local conversation/transcript flow
   - no Gemini-specific card shape

2. **Assistant message**
   - final assistant text should render through the shared assistant/transcript path
   - markdown handling should rely on the existing generic renderer path

3. **Permission request**
   - use the shared approval plumbing instead of inventing a Gemini-specific approval card
   - if a live approval render payload must survive refresh/replay, persist the same render payload the live UI saw
   - approval decisions should resolve back through the extension hook surface

4. **Tool activity / tool result**
   - use generic tool-card semantics
   - prefer canonical `request` / `response` fields
   - replay must preserve any tool metadata the live UI depends on later

5. **Warning**
   - use the shared live-only `warning` contract
   - do not synthesize warning transcript rows unless the warning actually becomes part of durable history

6. **Error**
   - use the shared generic error contract
   - live: `type: "error"`
   - replay: `role: "error"`

7. **Debug trace**
   - optional and internal only
   - if persisted, mark both live and transcript rows with `internal: true`

## 6. Transcript and replay requirements

Replay correctness is part of the contract, not a later polish step.

- local `transcript.jsonl` remains the durable replay source
- the router owns normalization from Gemini payloads into the shared transcript/event contracts
- if a live event is replayable, every `_emit()` must have a matching `_record()` with the same meaningful fields
- replay must rebuild the same user-visible state without guessing missing data
- internal/debug rows must stay hidden from normal replay and preview paths unless explicitly requested

For the first pass, a smaller semantic set is fine, but every semantic we do expose must honor live/replay parity.

## 7. Workspace write and sandbox policy

The Gemini extension should adopt the repo's shared policy vocabulary rather than inventing Gemini-only names.

### 7.1 Stable user-facing policy fields

The target stable field names should be:

- `approval_policy`
- `sandbox_policy`

This aligns Gemini with the shared runtime-options and settings-modal path already used by the other extensions.

### 7.2 Minimal sandbox semantics

The target normalized sandbox meanings should be:

- `read-only`
- `workspace-write`
- `danger-full-access`

At minimum:

- `read-only` means no write-capable tool/file operation is allowed
- `workspace-write` means writes are limited to the selected working tree / approved writable roots
- `danger-full-access` means the extension may perform unrestricted local writes

If the backend cannot honestly enforce one of these modes yet, the extension must not advertise it as supported.

### 7.3 Current scaffold reality

Today, the scaffold's bridge rejects local ACP file/terminal helper methods with `method_not_found`. That means the current bridge is not yet a true workspace-write bridge, even if the upstream Gemini runtime can do other work internally.

So the contract for the next implementation pass is:

- be honest about what is and is not enforced
- do not claim workspace-write semantics until the extension can actually normalize and honor them
- prefer a conservative default over a misleading permissive one

## 8. Settings schema contract

Settings should remain extension-owned and schema-driven.

Minimum desired settings surface:

- `cwd`
- `model`
- `approval_policy`
- `sandbox_policy`
- `developer_instructions`
- `debug_trace`

Optional but recommended:

- an `Information` section using shared `section` / `info` fields
- live provider/account information when available

If the schema includes live information, use a dynamic schema and set `cache: "none"` so modal opens refetch fresh values.

`session_picker` should stay out of scope until real Gemini session browse/resume support exists.

## 9. Router responsibilities

Once Gemini updates are promoted into the UI, `router.py` should own the translation seam.

That router should:

- translate Gemini/ACP payloads into generic repo contracts
- keep live `_emit()` and transcript `_record()` in parity
- avoid leaking raw provider event names into shared frontend files
- optionally emit internal `debug_trace` or `debug_raw` rows when debug tracing is enabled

`server.py` should only call the generic extension hooks. It should not learn Gemini event names directly.

## 10. Minimal success criteria for the next implementation pass

The next implementation pass should be considered successful when all of the following are true:

- the extension can be installed through the editable/symlink path and loaded as a user extension
- the settings modal renders from the extension schema without builtin special-casing
- the first prompt starts or adopts `gemini --acp`, attaches the SDK, and sends successfully
- the extension surfaces at least assistant text, generic errors, and permission handling through shared contracts
- whatever is shown live is also correctly replayable from local transcript data
- no Gemini-specific logic is added to platform-agnostic core files

## 11. Known intentional gaps right now

The current scaffold is still intentionally incomplete:

- `router.py` is still a stub
- `session_update` payloads are currently collected internally, not yet translated into live/transcript output
- `sessionResume` is still false
- model listing is still conservative
- sandbox/write policy is not yet normalized to the shared field names and runtime-options path
- live provider information blocks are not wired yet

That is acceptable for the scaffold phase. This document exists to define the minimal contract the next pass should implement against.
