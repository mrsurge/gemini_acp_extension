# Message and Reasoning Contract Audit

This note records current progress and direction for the shared message/reasoning lane before Gemini ACP normalizes more of its runtime output.

It is **not** the canonical shared contract. The canonical shared contract still belongs in `TRANSCRIPT_CARD_CONTRACTS.md`. This file is the Gemini-side audit/direction note that explains what is currently implicit, where the rough edges are, and what should be pulled into the shared contract next.

## Why this exists

Tool cards already have a much clearer shared contract than message and reasoning cards.

By contrast, the message/reasoning lane is currently defined mostly by behavior spread across:

- `agent_log_server/static/js/codex_agent/events/router.js`
- `agent_log_server/static/codex_agent.js`
- `extensions/copilot_sdk/router.py`
- `extensions/codex_ext/router.py`
- `agent_log_server/server.py` send/draft-restore flow

That means the frontend already understands a partially shared vocabulary, but the rules around ordering, replay parity, user-send success, and reasoning/title handling still live in router code and comments instead of in the contract doc.

Gemini already started moving toward the shared path:

- assistant output now finalizes through `assistant_finalize` + transcript `role: "assistant"`
- failed sends already use the shared `restore_draft` + generic `error` path

But Gemini still needs a clearer shared contract for:

- user send / user event
- assistant ordering relative to tools and reasoning
- reasoning delta/finalization
- `thought` vs durable reasoning

## Current Gemini status

Current Gemini scaffolding in this repo already aligns with part of the shared lane:

- `client.py` emits generic failed-send results that can restore the composer draft and surface a shared generic error card
- `router.py` turns collected ACP assistant chunks into:
  - live `assistant_finalize`
  - transcript `role: "assistant"`

What Gemini does **not** normalize yet:

- a canonical live user event on accepted send
- transcript/live reasoning normalization
- `thought` / reasoning-title separation
- explicit ordering rules relative to tool cards

## Observed shared runtime vocabulary today

### Live frontend events already understood by the unified frontend

The shared frontend event router already handles these message/reasoning events generically:

- `message`
- `assistant_delta`
- `assistant_finalize`
- `reasoning_delta`
- `reasoning_finalize`
- `thought`

### Replay / transcript rows already understood by the unified frontend

Replay already has generic handling for:

- `role: "user"`
- `role: "assistant"`
- `role: "reasoning"`

There is **no** replay contract for `thought`. Today `thought` is live-only and feeds the activity/ribbon path.

## Rough edges found in the current contract

### 1. `TRANSCRIPT_CARD_CONTRACTS.md` does not explicitly define the message/reasoning lane

`TRANSCRIPT_CARD_CONTRACTS.md` already defines shared behavior for token/context state, tool cards, view/search cards, errors, warnings, and composer draft restore on failed send.

It does **not** yet define, with the same clarity:

- live `message`
- live `assistant_delta`
- live `assistant_finalize`
- live `reasoning_delta`
- live `reasoning_finalize`
- live-only `thought`
- replay `user` / `assistant` / `reasoning`
- ordering rules across those lanes

So the event names are partly shared, but the actual semantics are still de facto rather than explicitly contractized.

### 2. The success-side user-send contract is missing

The frontend `sendUserMessage(...)` path does **not** optimistically render a local user row. It only sends the Socket.IO request and waits for backend activity/results.

That means the visible user message currently depends on the backend/router emitting a live event such as:

```json
{ "type": "message", "role": "user", "id": "...", "text": "..." }
```

Observed behavior today:

- Copilot emits a live user `message` on turn start and records transcript `role: "user"`
- Codex emits a live user `message` when the upstream payload produces a user item/event and also records transcript `role: "user"`
- the generic server send contract documents the **failure** half (`restore_draft`, optional generic `error`) but not the **success** half

So the current user-send success path is extension-shaped instead of fully contract-shaped.

### 3. Message ordering is real, but currently implicit

Copilot already contains an explicit router comment saying replay should mirror live ordering by recording visible reasoning before assistant finalization and before later tool cards.

That is not a Copilot-only detail. It is a shared contract rule and should live in the shared contract doc, not only inside a router comment.

### 4. The assistant contract is only half-documented

Current behavior is already converging on a good pattern:

- live assistant text streams through `assistant_delta`
- live assistant completion arrives through `assistant_finalize`
- replay uses a single finalized transcript row: `role: "assistant"`

But the shared contract doc does not currently say that explicitly.

The missing rule is important:

- live deltas are a transient rendering aid
- the transcript row is the authoritative finalized assistant message for replay
- routers should use `assistant_finalize` even when the upstream runtime supplies a one-shot complete assistant message without prior deltas

### 5. The reasoning contract is split across two concepts, but that split is not written down

Current code already treats two different things as separate:

- `thought`
  - live-only
  - drives the ribbon/activity lane
  - often carries headings, titles, or short reasoning labels
- `reasoning_delta` / `reasoning_finalize`
  - live visible reasoning body
  - intended to mirror the durable reasoning card lane

Replay persists only the finalized reasoning body as:

- `role: "reasoning"`

That implies an important rule which is currently only implicit:

- title-only / whitespace-only reasoning should not become durable reasoning cards
- visible reasoning body should be scrubbed before persistence if the live stream included title/thought wrappers
- `thought` is not the transcript contract

### 6. Subagent nesting needs to be part of the message/reasoning contract

The frontend already knows how to place `message`, `assistant_*`, and `reasoning_*` events into subagent containers when `subagent_id` is present.

So the shared contract needs the same mirror rule that other cards already follow:

- if a live message/assistant/reasoning event is nested under `subagent_id`
- then the replayable transcript entry must carry that same `subagent_id`

Otherwise live and replay will diverge structurally.

### 7. Stable IDs are not written down

The current frontend row lifecycles assume a stable ID relationship:

- `assistant_delta` and `assistant_finalize` should share the same `id`
- `reasoning_delta` and `reasoning_finalize` should share the same `id`
- when known, the replay transcript row should keep the same semantic `id`
- user live `message` and replay `role: "user"` rows should also preserve a stable `id` when one exists

That rule matters for DOM row reuse, stream finalization, and future handoff/reconciliation work.

## Proposed shared contract direction

### 1. User send and user event

The transport ack from `send_message` should mean only:

- the backend accepted the request
- the send either started or failed under the backend-owned send contract

It should **not** be the thing that renders the user row.

The visible user row should remain backend-owned and use a generic live event:

```json
{
  "type": "message",
  "conversation_id": "...",
  "id": "...",
  "role": "user",
  "text": "...",
  "turn_id": "...",
  "subagent_id": "optional"
}
```

Replay should use the matching transcript row:

```json
{
  "role": "user",
  "id": "...",
  "text": "...",
  "timestamp": "...",
  "turn_id": "...",
  "subagent_id": "optional"
}
```

The existing failed-send behavior remains valid and should stay backend-owned:

- `restore_draft: true`
- `draft_update`
- optional generic `error`

### 2. Assistant message lane

The assistant lane should stay stream-capable:

- zero or more live `assistant_delta` events
- one live `assistant_finalize` event with the full final text
- one replay transcript row `role: "assistant"`

Expected live shape:

```json
{
  "type": "assistant_delta",
  "conversation_id": "...",
  "id": "...",
  "delta": "...",
  "turn_id": "...",
  "subagent_id": "optional"
}
```

```json
{
  "type": "assistant_finalize",
  "conversation_id": "...",
  "id": "...",
  "text": "...",
  "turn_id": "...",
  "subagent_id": "optional"
}
```

Expected replay row:

```json
{
  "role": "assistant",
  "id": "...",
  "text": "...",
  "timestamp": "...",
  "turn_id": "...",
  "subagent_id": "optional"
}
```

Direction:

- if the upstream runtime emits a one-shot complete assistant message, routers should still normalize it onto `assistant_finalize`
- `message` should not become the generic assistant success lane

### 3. Reasoning lane

The reasoning lane should explicitly separate:

- live-only thought/title/ribbon activity
- durable visible reasoning body

Recommended live-only `thought` shape:

```json
{
  "type": "thought",
  "conversation_id": "...",
  "text": "...",
  "turn_id": "...",
  "subagent_id": "optional"
}
```

Recommended live reasoning body shapes:

```json
{
  "type": "reasoning_delta",
  "conversation_id": "...",
  "id": "...",
  "delta": "...",
  "turn_id": "...",
  "subagent_id": "optional"
}
```

```json
{
  "type": "reasoning_finalize",
  "conversation_id": "...",
  "id": "...",
  "text": "...",
  "turn_id": "...",
  "subagent_id": "optional"
}
```

Replay row:

```json
{
  "role": "reasoning",
  "id": "...",
  "text": "...",
  "timestamp": "...",
  "turn_id": "...",
  "subagent_id": "optional"
}
```

Direction:

- transcript rows should exist only for visible reasoning body
- `thought` remains live-only
- title-only / heading-only reasoning should not create a durable reasoning row
- routers may scrub title wrappers and section labels before persisting `role: "reasoning"`

### 4. Ordering rules

The message/reasoning contract should explicitly state these ordering rules:

- user row must appear before assistant/reasoning/tool output for the same turn
- if visible reasoning is shown before assistant completion in live play, the transcript must record reasoning before the assistant row
- if assistant prose finalizes before a later tool/result card in live play, replay must keep the assistant row before that later tool/result
- live delta streams do not need transcript delta rows, but their final ordering must be reflected by the finalized transcript rows

This is the missing rule that currently lives only in router behavior/comments.

### 5. Subagent and ID propagation rules

The shared contract should also write down:

- live `message`, `assistant_*`, and `reasoning_*` events may carry `subagent_id`
- when a replayable counterpart exists, that transcript row must carry the same `subagent_id`
- delta/finalize events for the same logical row must share the same stable `id`
- replay rows should preserve that `id` when the backend can determine it

## What should be added to `TRANSCRIPT_CARD_CONTRACTS.md`

The next contract-doc pass should add explicit sections for:

- live `message`
- replay `role: "user"`
- live `assistant_delta`
- live `assistant_finalize`
- replay `role: "assistant"`
- live `reasoning_delta`
- live `reasoning_finalize`
- replay `role: "reasoning"`
- live-only `thought`
- user-send success/failure coupling
- message/reasoning ordering rules
- subagent/id mirror rules for these lanes

## Gemini implementation impact

For Gemini, this audit implies the next normalization slice should be:

1. Emit a canonical live user `message` + transcript `role: "user"` when a Gemini send is accepted for the turn.
2. Normalize ACP reasoning updates into:
   - `thought` for ribbon/title-like live activity
   - `reasoning_delta` / `reasoning_finalize` for visible reasoning body
   - transcript `role: "reasoning"` only when visible reasoning exists
3. Keep the current assistant path:
   - `assistant_finalize`
   - transcript `role: "assistant"`
4. Only after the message/reasoning contract is explicit should Gemini widen tool/permission normalization further.

## Current recommendation

Do **not** patch `TRANSCRIPT_CARD_CONTRACTS.md` yet blindly.

First, turn the above direction into an explicit shared proposal that can be checked against both:

- Copilot
- Codex/Codex-exp

Then move the agreed message/reasoning sections into the canonical contract doc and finish the Gemini adapter against that shared shape.
