# Gemini ACP Extension

This repository contains the Gemini extension for the `agent_log_server` extension system.

It runs the Gemini CLI in ACP mode through framework-shells, connects to it through the ACP Python SDK, and translates Gemini session updates into the shared conversation/transcript contracts used by the harness.

## What this extension does

- starts `gemini --acp` through framework-shells pipe mode
- connects through the vendored ACP Python SDK
- exposes schema-driven settings for:
  - working directory
  - session picker
  - model selection
  - approval policy
  - directory trust
  - developer instructions
  - debug trace
- lists resumable Gemini sessions
- binds a remote Gemini session to a local harness conversation
- hydrates transcript entries for port-in/import flows
- keeps ordinary existing-conversation reload transcript-first and backend-cold until the first new send
- streams assistant and reasoning updates into the shared live event contract
- records matching transcript rows for replay parity

## Runtime architecture

The main pieces are:

- `manifest.json`
  - extension identity, capabilities, default model, shellspec reference
- `settings_schema.json`
  - schema-driven settings surface used by the shared settings modal
- `client.py`
  - extension entry points used by `ext_loader`
  - session bind/hydrate/send flow
  - transcript and broadcast integration
- `transport.py`
  - framework-shell lifecycle
  - ACP SDK connection ownership
  - model/session operations
  - lazy cold-session resume/retry logic
- `router.py`
  - Gemini update normalization helpers
  - assistant/reasoning live and transcript shaping
- `bridge_client.py`
  - ACP client bridge / permission handling seam
- `dependencies.py`
  - local dependency checks
- `shellspec/gemini_acp.yaml`
  - framework-shell definitions for `gemini --acp`

## Session lifecycle contract

This extension follows the repo-wide session lifecycle contract.

### Existing harness conversation reload

When a conversation already exists on the harness:

1. the UI replays the local `transcript.jsonl`
2. the bound Gemini `session_id` stays cold
3. the first new send uses the normal send path
4. if Gemini reports a cold-session error, the extension reattaches and retries the buffered send

This extension must not eagerly import Gemini history during ordinary conversation reload/select.

### Port-in / import

For a new local conversation created from an existing remote Gemini session:

1. `resume_session_with_history(...)` binds the remote session
2. `hydrate_transcript(...)` loads Gemini history and returns flat transcript entries
3. the harness writes those entries into local `transcript.jsonl`

That is a different flow from ordinary reload/select.

### Gemini-specific cold-session behavior

In this environment, Gemini may advertise `loadSession` without advertising `session/resume`.

When that happens:

- `load_session()` acts as the synthetic reattach ack for the retry path
- replay updates from `load_session()` must stay suppressed until they go quiet
- only after that quiet period may the retried live prompt enter prompt-capture mode

This quiet-period barrier is important. Without it, delayed replay updates can spill into the retried live turn and pollute the new assistant transcript row.

## Live event and transcript behavior

Gemini ACP `session/update` payloads are normalized into the shared conversation contracts.

Current message lanes:

- `user_message_chunk` -> user transcript rows during hydration/import
- `agent_message_chunk` -> live `assistant_delta` / `assistant_finalize` and transcript `role: "assistant"`
- `agent_thought_chunk` -> live `reasoning_delta` / `reasoning_finalize` and transcript `role: "reasoning"`

Tool updates stay in the Gemini/ACP update stream and are handled through the shared harness tool-card behavior rather than provider-specific frontend branches.

## Validation workflow

The normal repo-first workflow for this extension is:

1. reproduce/test the installed extension behavior
2. investigate in this repo plus the relevant runtime/log surfaces
3. edit this repo
4. run repo-side validation
5. run small non-server smokes when the change affects session/update timing or transcript shaping
6. ensure `manifest.json` reflects the revision being shipped
7. commit and push this repo
8. install/update the user-installed extension from the pushed repo through the normal extension install workflow
9. test the installed extension again

Do not treat the installed extension directory as the source of truth for development changes.

Typical validation commands:

```bash
python -m py_compile __init__.py vendor_sdk.py bridge_client.py dependencies.py transport.py client.py router.py
basedpyright
```

## Installation / update

This repo is the source repository. The live installed extension normally lives under:

```text
~/.local/share/app_server/extensions/gemini-acp/
```

Use the normal extension install/update workflow after pushing repo changes. See:

- `THIRD_PARTY_EXTENSION_WORKFLOW.md`
- `acp/AGENT_EXTENSION_INTEGRATION.md`

## Current capabilities

Implemented:

- framework-shell owned Gemini ACP process
- schema-driven settings
- model listing
- session listing
- session bind/import hooks
- transcript hydration for import flows
- transcript-first lazy reload for ordinary existing conversations
- live assistant streaming
- live reasoning streaming
- replay-noise suppression with a quiet-period barrier before retry prompt capture

Still intentionally conservative in places:

- provider/account info in the settings UI is still minimal
- directory trust normalization is present in the shared runtime option shape, but Gemini-specific enforcement semantics remain conservative
- the local design docs in this repo may describe older implementation phases; the code and this README are the current source of truth

## Related docs

- `README.md` (this file) - Gemini repo overview
- `MINIMAL_CONTRACT.md` - local contract notes for the Gemini repo
- `MESSAGE_REASONING_CONTRACT_AUDIT.md` - local audit notes for message/reasoning contract alignment
- `acp/AGENT_EXTENSION_INTEGRATION.md` - canonical extension integration contract
- `CODEX_APP_SERVER_EXTENSION.md` - architecture/reference implementation manual
- `THIRD_PARTY_EXTENSION_WORKFLOW.md` - install/update workflow contract
