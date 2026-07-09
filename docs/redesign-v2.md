# Redesign v2 — Categorization, Chat, and the Desktop Portal

*Drafted 2026-07-08 from the ground-up rethink conversation.*

**Status 2026-07-08 (evening): Phases 1–3 SHIPPED.** Phase 1a chat_message
logging live in the bot; Phase 1b Chat portal + Phase 2 Inbox panel live in
the desktop app; Phase 3 auto-file (override/prior/order → commit with
`filed_by` provenance) + Amazon retro-bucket sweep live. Phase 4 (group-chat
cutover + deletions) blocks on Steven creating the Telegram group; Phase 5
(narrow-job pipeline replacing the v1 agent loop) follows it.

## Why

The v1 triage system grew three input surfaces (inline keyboard buttons, numbered
text replies, free-text LLM chat) that all share fragile per-chat conversation
state (`last_asked_id`, `last_batch_json`, `last_asked_message_id`). Failures we
have actually hit, all structural:

- **Stall on ignored DM** — the push loop gates on `last_asked_id`; one
  unanswered question blocks the queue forever.
- **Numbering desync** — numbered replies resolve against a snapshot
  (`last_batch_json`) that goes stale the moment the queue changes.
- **Contradictory counts** — "pending" is a six-way partition (two tables ×
  three lanes × per-user assignment) and every surface filters it differently
  (the "39 vs 1+7" incident, 2026-07-06).
- **Fabricated answers** — qwen3:32b ignores the "always call list_pending"
  instruction and invents queue listings from chat history (patched with a
  deterministic intercept 2026-07-08, but the incentive to fabricate remains
  as long as chat is a data-entry terminal).

The root causes: (1) queue truth lives in conversation state instead of data
state; (2) chat is doing a job a UI should do; (3) everything waits for a human
even when the system already knows the answer.

## Decisions made

| Question | Decision |
|---|---|
| Bot autonomy | **Auto-file high-confidence only** — strong priors (repeat payees, Amazon buckets) commit immediately with provenance; novel/ambiguous items go to the Inbox |
| Steven's triage surface | **Desktop Inbox panel is primary**; Telegram is notification + Q&A, not a triage terminal |
| Two-user model | **One bot, one three-way household group chat** (Steven + Allison + bot); no duplicate threads; the second bot is retired |
| Allison's review flow | **Steven curates batches from the desktop Inbox** — select items → "Send to Allison" → one numbered message; she answers in natural language ("1 personal, 2 household, 4 not sure"); the bot follows up on anything unclear and summarizes what it filed |
| Telegram buttons / numbering | **Buttons deleted. Dynamic-queue numbering deleted.** Numbering survives only inside dispatched review batches — immutable snapshots where 1..N is frozen at send time. Reply-to-message anchors single-item questions |
| Desktop chat portal | **Read-only interleaved timeline** — chat bubbles + collapsed engine-event rows in one stream; actions happen via the Inbox, which auto-resolves chat questions |
| Chat pipeline | **Bot-first UX, narrow-job implementation.** Users write natural language anywhere (no rigid reply formats) and receive conversational replies — but the model is a set of small specialists (parse this answer, classify this intent, phrase this fact sheet), never one general agent. All facts and actions come from deterministic code |
| Chat brain | **qwen3:32b stays** (local Ollama; no hosted API — decided 2026-07-08). Reliability comes from architecture, not the model: structural dispatch, one schema-constrained job per call, code executes all writes, compose-then-verify on outbound |

## Design principles

1. **Data state, not conversation state.** A transaction either needs review or
   it doesn't — a flag on the item, readable by every surface identically. No
   surface keeps a private snapshot of the queue.
2. **Review by exception.** The bot files what it knows; humans handle only what
   it can't. (This is already how the Amazon auto-bucket flow works — the one
   part of v1 that never generated complaints.)
3. **Humans route; the bot announces.** Nobody "owns a queue" and nothing is
   auto-assigned. Steven delegates deliberately (curated review batches from the
   Inbox); the bot's only self-initiated asks are attribution questions ("was
   this Amazon order yours?") posted to the shared group. First answer wins.
4. **Chat is opportunistic, never load-bearing.** Every ping (instant item,
   question, batch) is fire-and-forget: replying acts instantly, ignoring costs
   nothing — the item is already in the Inbox either way. Anchoring is
   reply-to-message (deterministic) or most-recent-open fallback; no shared
   mutable pointer. Nothing can stall.
5. **Truly acting as an AI — voice from the model, facts from code.** It must
   never feel like talking to an algorithm: users write natural language
   anywhere, and the model reads every message and writes every reply (with
   persona and conversation history — not template strings). Behind the
   voice, deterministic code owns every fact and every write. The hard rules:
   the model's outputs are schema-constrained data or verifier-checked prose;
   every action is echoed back as a receipt; and the portal's engine trail
   makes any violation visible (a bot message with no engine events behind it
   is, by definition, made up).
5. **Outcomes, not internals.** Engine transparency = decision narrative
   ("matched order #72 because recipient = Allison"), never scores/weights.
   (Per prior feedback rejecting the score-viz Reconciler.)
6. **Conversation is data; Telegram is a client.** Every message in/out is
   logged to the DB and linked to the engine events that produced it. The
   desktop portal renders the same stream with the engine trail interleaved.

## Target architecture

```
 emails ─┐
         ├─ ingest ─ match/enrich ─┬─ high confidence ──► auto-file (provenance: auto)
 YNAB  ──┘                         │                          │ (FYI ping, reply corrects)
                                   └─ low confidence ───► Inbox (state: open)
                                                            │      │
                                              instant ping ◄┘      │
                                              (reply = file now,   │
                                               ignore = no-op)     │
              ┌────────────────────────────────────────────────────┤
              ▼                                                    ▼
   Desktop Inbox panel ──"Send to Allison"──► numbered review batch
   (table, dropdowns, bulk)                   (frozen at dispatch; NL reply,
              │                                follow-up, summary receipt)
              └────────────► same item id ◄────────────────┘
                     resolution on any surface
                     resolves the others automatically
```

- **Bot remains the single writer**; desktop reads SQLite directly and writes
  through the existing 127.0.0.1:8765 API (unchanged pattern).
- **One Telegram bot** in a three-person group (privacy mode off so it sees all
  messages). Sender identity = `message.from.id` (replaces chat-per-person
  routing). DMs with the same bot remain for private summaries (Steven's 06:30
  household summary, Allison's 08:00 envelope check).

## Data model changes

### New: `chat_message`
Every inbound/outbound message.

```
id, ts, tg_chat_id, tg_message_id, reply_to_tg_message_id,
sender            -- 'steven' | 'allison' | 'bot'
direction         -- 'in' | 'out'
text,
item_kind, item_id   -- nullable link to the pending item it concerns
```

### New: `review_batch`
A human-curated set of items dispatched from the desktop Inbox as one numbered
message. **Immutable after dispatch** — the numbers 1..N are frozen at send
time, which is what makes numbering safe here when it wasn't in v1 (v1 numbered
a live queue that shifted under the reader).

```
id, created_by, recipient ('allison'|'steven'),
sent_message_id (fk chat_message), created_at,
state ('open'|'complete'|'recalled')
```

### New: `question`
One row per thing the bot is waiting to hear about — a standalone attribution
question, an instant-item ping, or one numbered line of a review batch.

```
id, kind ('attribution'|'instant'|'batch_item'),
batch_id (fk review_batch, null unless batch_item), n (1..N within batch),
item_kind, item_id, asked_message_id (fk chat_message),
asked_at, state ('open'|'answered'|'resolved_elsewhere'|'expired'),
answered_by, answer_text, resolved_at
```

A batch is `complete` when every child question leaves `open`. Items resolved
in the desktop Inbox before an answer arrives flip to `resolved_elsewhere` and
the bot notes it in the summary receipt ("3 was already handled").

### Changed: `pending_txn` → the Inbox + decision record
- `state`: `open` | `resolved` (replaces `status` pending/skipped/categorized —
  "skipped" becomes an Inbox item you simply haven't handled; it never blocks
  anything).
- `filed_by`: `auto_prior` | `auto_bucket` | `auto_llm` | `steven` | `allison`
  — provenance for every categorization, so "review what the bot did" is a
  query, and the portal can show it.
- **Deleted columns (after migration):** `queue_lane`, `lane_changed_at`,
  `assigned_to_user_id`, `digest_run_id`.

### Changed: `pending_order` → enrichment only
Orders stop being a user-facing queue. They exist to explain/attribute charges
(Amazon recipient, Venmo counterparty). An unmatched order past its window can
raise a *question*; it never sits in anyone's triage list.

### Shrunk: `bot_conversation`
`last_asked_kind/id`, `last_asked_message_id`, `last_batch_json`,
`last_turns_json` all die. Survivors: `quiet_until` (moves to `user_pref` if
that's cleaner). The LLM's short-term memory becomes the tail of
`chat_message` — real history, not a private scratchpad.

### `audit_log`
Gains a nullable `chat_message_id` so engine events link to the messages they
produced — this is what powers the interleaved portal timeline.

## Surfaces

### Desktop Inbox panel (new; primary triage)
- Table of `state = 'open'` items: date, payee, amount, evidence summary
  (matched order, prior), suggested category.
- Category dropdown per row, bulk-select + bulk-assign, keyboard-first.
- **"Send to Allison"**: select rows → dispatch as a numbered review batch.
  Rows show a "with Allison" badge while their batch question is open.
- A "recently auto-filed" section (last N days, `filed_by like 'auto%'`) with
  one-click recategorize — the safety net that makes auto-filing trustworthy.
- Writes via `POST` to the :8765 API; mutations invalidate the same query keys
  as the Budget panels.

### Desktop Chat portal (new; read-only)
- One interleaved timeline: chat bubbles (Steven/Allison/bot) + collapsed gray
  engine rows (parsed email → matched order → filed / question raised) in time
  order, expandable for detail.
- Decision *narrative*, not scores. A bot message with no engine rows behind it
  is itself a red flag — fabrication becomes visible at a glance.
- No send box. Acting happens in the Inbox; resolution shows up in the stream
  ("✓ resolved from desktop").

### Group chat (Telegram)
Chat's job, in priority order:

**1. Reports.** Daily and weekly summaries (private per-person digests stay in
DM; the group digest is short and aggregate: "Filed 12 yesterday (11 by prior,
1 Amazon). 2 in the Inbox. 1 batch open for Allison.").

**2. Instant items.** As each new charge lands, one ping — the *option* to act
in the moment, never an obligation:
- Low-confidence: `🆕 $69.50 Jansport Denver (7/5) — suggest Household Items.
  Reply with a category to file it now; otherwise it's in the Inbox.`
- Auto-filed (FYI): `✓ $12.49 Amazon Mktplace → Amazon-Steven` — reply to
  correct it.
Replying files instantly; ignoring is a no-op (the item is already in the
Inbox / already filed). Nothing waits, nothing stalls.

**3. Review batches.** Steven-dispatched from the Inbox:
- Bot sends one frozen numbered message: `Steven flagged 4 for you, Allison:
  1. $74.93 Chick-fil-A HOU airport (6/27) · 2. …`
- She answers in natural language: `1 personal, 2 household, 3 dining, 4 not
  sure`. Parse is deterministic-first (`n → category token` against known
  category names), LLM only for fuzzy leftovers.
- Targeted follow-up on anything unclear: `For 2 — Household Items or Home
  Improvement?`
- Summary receipt closes the loop and doubles as parse verification: `Filed:
  1→Steven Personal, 2→Household Items, 3→Dining Out. 4 is back in Steven's
  Inbox with your note.` "Not sure" answers return the item to the Inbox with
  her response attached as evidence.

**4. Questions & Q&A.** Bot-raised attribution questions (`❓ $255 Venmo from
Amanda Walter — what was this for? @Allison`; answer via reply-to, first answer
wins) and money questions to the LLM ("how much is left in dining?").

**5. Referee mode — humans talk to each other; the bot supplies evidence.**
Steven and Allison converse directly in the group; when their exchange concerns
an item the bot knows, it contributes facts, not opinions:

> **Steven:** @Allison — do you know what this is?
> **Bot:** That charge was 6/27 9:42pm, merchant located Higuey DOM, card
> flagged online / card-not-present. No matching order email. First time at
> this merchant.
> **Allison:** oh that's the excursion deposit
> **Steven:** file it to vacation
> **Bot:** ✓ Filed $50.00 CAICALI → Vacation.

Anchoring: reply-to an item's message (deterministic) or the item most recently
discussed in-thread (fallback). Restraint rule: the bot speaks only when
addressed, when a human message replies to an item thread, or when it holds
evidence directly relevant to a question one human asked the other — it is a
referee, not a chaperone. Everything it needs (email parses, order matches,
payee history, card metadata where the bank provides it) is already in the
ledger; referee mode is retrieval + formatting, not new intelligence.

Cross-surface rule everywhere: if an item is resolved in the desktop Inbox
while a ping/question/batch line is open, the bot annotates the chat message
("✓ resolved from desktop") — both surfaces are views of the same item id.

### The chat brain (qwen3:32b, narrow-job pipeline)
The brain stays local — qwen3:32b on Ollama (decided 2026-07-08). qwen cannot
be trusted as an open-ended tool-calling agent (the 2026-07-06 fabrication
incidents), so the design never asks it to be one. It *can* be trusted at
small, constrained jobs — extraction, classification, phrasing — so the
pipeline is built entirely from those. **The design bar: it must never feel
like talking to an algorithm.** No rigid input formats, no canned outputs, no
ignoring context — the model reads every message and writes every reply; code
owns every fact and every write.

```
inbound message
   │
   ▼
DISPATCHER (pure code) — routes by STRUCTURE, not language:
   ├─ reply to a question message → answer-parse job
   ├─ reply to a batch message    → batch-mapping job
   ├─ reply to an instant ping    → category-parse job
   └─ free-standing text          → intent-classify job (small enum) → one job
   ▼
ONE narrow qwen call per job — tiny focused prompt, temperature ~0,
Ollama format=json with a schema (available once tool-calling is dropped;
the two conflict). Extraction/classification only — qwen's strong suits.
   ▼
CODE executes the parsed intent — validated writes (category must resolve,
batch must be open, item must exist) → fact sheet → reply
   ▼
COMPOSE-THEN-VERIFY — qwen writes the user-facing reply from the fact sheet,
with conversation history and a consistent persona. A verifier checks every
number, date, and name in the prose against the fact sheet; on mismatch the
plain fact-sheet text is sent instead (rare fallback, not the default voice).
```

- **`reply_to_message_id` does the heavy lifting.** Most turns need zero
  language understanding to know what they're *about* — that's message
  metadata. The model only interprets the *content* of an answer.
- **No tool-calling anywhere.** qwen's weakest skill is removed from the
  system. The model returns data; code acts. Dropping tools also unlocks
  Ollama's schema-constrained output, which is qwen's most reliable mode.
- **Facts only from code.** Every number a user reads was placed there by
  deterministic code or survived the verifier. A wrong count is now
  structurally impossible, not just discouraged by prompt.
- **The AI feel is a feature, not a risk.** Outbound is model-voiced by
  default: persona + recent `chat_message` history means it can refer back to
  the conversation, vary its phrasing, react in the group, and banter.
  Referee mode (evidence contributions to human conversation) is composed the
  same way — retrieval by code, voice by model.
- **Q&A** ("how much is left in dining?"): intent-classify → the existing read
  tool runs → its output *is* the fact sheet → model phrases it. Never
  model-remembered numbers.
- **Maintainability win:** each narrow job is independently testable against a
  small eval set of real past messages — regressions become failing tests, not
  chat incidents. Smaller prompts also mean faster local inference (no 20-tool
  schema payload per call).

## What gets deleted

- Inline keyboards + all callback handlers (`bt:N` taps)
- v1 dynamic-queue numbering: `categorize_batch_numbered` + `_BATCH_REPLY_RE`
  resolving against `last_batch_json` snapshots. (Numbering itself survives —
  but only inside dispatched `review_batch` rows, where the mapping is frozen
  in the DB, not in conversation state.)
- `batch_processor` (`/batch`, cold-batch counting)
- `/amazon` as a queue (auto-bucket already handles it; leftovers raise questions)
- Queue lanes (`hot`/`cold`/`hold`) and `queue_lane.py`
- **Auto-assignment**: per-user queue routing dies entirely — Steven curates
  what Allison sees via batch dispatch; the bot never assigns items to anyone
- The second Telegram bot (@HarrisBudgetBot)
- `last_asked_*` / `last_batch_json` / `last_turns_json` state machine
- The push loop's ask-one-wait-for-answer gating (pings are fire-and-forget;
  nothing waits on a reply)

## Migration path

Ordered so each phase ships value alone and nothing breaks mid-stream:

1. **Observability first (no behavior change).** Add `chat_message` logging to
   every send/receive; link `audit_log` events. Build the read-only Chat portal
   on top. *We instrument the old system before rewiring it — remaining v1
   flakiness becomes visible instead of anecdotal.*
2. **Desktop Inbox panel.** Reads the existing `pending_txn` queue (all lanes,
   both users, one table — the six-way partition collapses visually before it
   collapses in the schema). Writes via :8765. Numbering pain ends here.
3. **Auto-file high-confidence.** Extend `categorize_via_prior` into a commit
   path with provenance + the portal/Inbox "recently auto-filed" safety net.
   Queue volume drops ~80%.
4. **Group-chat cutover.** Create the group, flip privacy mode, ship the new
   chat flows (instant pings, review-batch dispatch + NL parse loop, reply-to
   questions), retire the second bot, then delete buttons/v1-numbering/lanes/
   batch_processor/auto-assignment and shrink `bot_conversation`.
5. **Cleanup.** Drop dead columns; retire the v1 tool-calling agent loop in
   `agent.py` in favor of the narrow-job pipeline (dispatcher + per-job
   prompts + composer/verifier). Build the per-job eval sets from real
   captured messages as each job ships — Phase 1's `chat_message` logging is
   what makes those eval sets possible.

## Open questions

1. **"High confidence" operationally.** Proposal: payee has ≥N (3?) prior
   human-confirmed filings to the same category AND amount within a tolerance
   band; Amazon bucket matches always qualify. Where do Venmo counterparty
   priors fit?
2. **Do Allison's answers train priors?** ("Amanda Walter Venmo → beach house →
   Vacation" should make the next one auto-file.) Lean yes — it's how her
   question volume tapers over time.
3. **Backlog disposition.** 34 skipped + cold-lane rows at cutover: bulk-resolve
   session in the new Inbox, or amnesty (auto-file with today's priors and let
   the safety net catch errors)?
4. **Quiet hours in a group.** Per-user quiet no longer maps cleanly to a shared
   chat. Simplest: bot respects a household quiet window for group posts; DM
   digests keep per-user times.
5. **Portal liveness.** Polling the DB (matches existing panels) vs a small SSE
   endpoint on :8765. Polling is probably fine for v1.
6. **Do Allison's batch answers commit immediately?** Lean yes (Steven asked
   because he wants her call; provenance `filed_by=allison` + the recently-filed
   safety net cover mistakes) — vs. staging her answers for Steven's approval.
7. **Instant-ping volume.** Do auto-filed FYI pings go to the group, DM, or
   nowhere (visible only in the portal/daily digest)? A busy card day could be
   noisy; maybe FYIs batch into one rolling message per hour, while
   low-confidence pings post individually.
8. **Batch destination.** Group (Steven sees her answers live, fits
   one-thread principle) vs her DM (less noise for Steven). Default: group.
