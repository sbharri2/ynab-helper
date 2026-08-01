# All Transactions to Telegram + Auto-Sync Panel

**Date:** 2026-08-01
**Status:** Approved (Steven, 2026-08-01)
**Branch:** mvp1-implementation

## Problem

Four independent code paths can each decide a transaction never reaches
Steven's or Allison's phone. No single place knows what was suppressed, so
the suppression compounded silently for two months.

Measured over July 2026 (on-budget outflows, excluding `Transfer :` rows and
split parents): **213 transactions, 41 with a human's name on them.**

| Path | July count | Behavior |
|---|---|---|
| `ynab_sync` never queued | 40 | Not in `_PROMPT_USER_KINDS` (`ingest.py:68`) — no question is ever created |
| `auto_prior` / `auto_override` | 51 | High-confidence commit at ingest (`ingest.py:386-430`), status set to `categorized` immediately |
| Small Amazon auto-bucket | 17 | Sub-threshold Amazon charges skip the prompt queue |
| `close_stale_pending` janitor | 66 | Adopts whatever category the ledger row already had and closes the question (`ynab_full_sync.py:549`) |

### The stale-comment hole

`ingest.py:67` reads:

> `ynab_sync` is intentionally NOT in here — `ynab_watcher.poll_once` already
> writes its own `pending_txn` for those.

`bot/ynab_watcher.py` was deleted in the "Phase 7+" cleanup
(`telegram_bot.py:1754`). The comment now guards a hole rather than
documenting a decision. Every YNAB-sourced transaction since that removal has
had no question path at all.

### Observed damage

`Allison Personal Savings` accumulated 31 rows since 2026-06-01, almost all on
her Citi Double Cash card, with `last_pushed_at IS NULL` on all but six — never
DM'd. Restaurants (Dr. B's, Trophy on Maywood, Cook Out, Burger King) and
Venmo transfers landed in a personal envelope rather than Dining Out. Apple
charges flipped from `Apple Subscriptions` to `Allison Personal Savings` on
2026-06-07 and stayed there for seven July charges totaling $70.48. Three of
those were pinged to the group chat, unanswered, then closed by the janitor.

Historical majority for the normalized payee `APPLE` is
`Apple Subscriptions` 67/77. The drift is the anomaly, not the pattern.

## Decisions

1. **Every transaction reaches Telegram.** No signal kind is exempt.
2. **Rules you author fire as FYI, not as questions** — a one-line message
   with a `[Correct]` button, no reply expected, nothing blocks.
3. **Implicit auto-filing dies.** Learned priors stop committing.
4. **`auto_prior` becomes the discovery engine** for a new Auto-Sync panel,
   surfacing candidate rules instead of acting on them.
5. **Rules are payee-pattern → category.** No account scoping, no amount
   guards.
6. **Amazon is out of scope.** A separate effort owns the Amazon revamp; this
   work does not touch `_amazon_bucket_category`, the `Amazon - *` bucket
   categories, or the large-Amazon HOLD lane.

## Volume

Existing 40 hard-coded rules cover only 16 of 213 July transactions (7.5%).
Discovery over 365 days of history finds **32 candidate rules** (≥3
occurrences, ≥70% agreement on one category) covering **85 more**.

| | Today | After, before rules | After, with 32 rules built |
|---|---|---|---|
| FYI (no reply needed) | — | 16 | ~101 |
| Real questions | 41 answered / 180 suppressed | 180 | ~95 (~3/day) |

Top discovery candidates:

```
HARRIS TEETER        99x → Groceries                  (99/99)
MASSACHUSETTS MU     36x → Mass Mutual Insurances     (36/36)
OAK CITY PSYCHOLOGY  29x → Mental Health              (29/29)
THE CAR PARK         18x → Transportation             (18/18)
CVS PHARMACY         18x → Pharmacy                   (17/18)
CAROLINA ACADEMYPA   15x → CAPA Theater Club          (15/15)
CLICKPAY             14x → HOA - Woodcreek (14th)     (13/14)
DOMINOS PIZZA        14x → Dining Out/Entertainment   (14/14)
```

## Architecture

New module `bot/dispatch.py` owns every "what reaches Telegram" decision.

```
ingest_signal / ynab_full_sync._upsert_txn
        │  writes ledger_txn + pending_txn
        │  NO category decision
        ▼
   dispatch.classify(pending_txn)
        │
        ├── rule matches payee ──▶ file it, filed_by = 'rule:<id>'
        │                          write ledger_txn.category_id
        │                          bump fire_count / last_fired_at
        │                          send FYI + [Correct]
        │                          never enters HOT/COLD, never sets
        │                          last_asked_id
        │
        └── no rule ────────────▶ stays status='pending'
                                   suggested_category pre-fills buttons
                                   existing HOT-lane question flow
```

### Ingest stops deciding

The `auto_commit` block at `ingest.py:386-430` no longer writes
`chosen_category`, `chosen_at`, `status='categorized'`, `filed_by`, or
`ledger_txn.category_id`. It still computes a suggestion and stores it in
`suggested_category` — that is what pre-fills the question's buttons.

`_PROMPT_USER_KINDS` gains `ynab_sync`. Because `ynab_full_sync._upsert_txn`
writes ledger rows directly rather than through `ingest_signal`, it also needs
its own `pending_txn` insert for newly-created rows.

Rows that must never produce a question, unchanged from today's filters:
`Transfer :%` payees, split parents (`is_split = 1`), split children
(`parent_txn_id IS NOT NULL`), and off-budget accounts.

### Delivery path: group pings, not DMs

`telegram_bot._push_loop` — the one-at-a-time DM drip gated on
`bot_conversation.last_asked_id` — **is not running.** `_post_init`
(`telegram_bot.py:1699`) starts the daily, weekly, gmail, ynab_full_sync,
lane_sweep, group_ping, and ui_api loops; there is no push task. The DM ask
loop was switched off in the redesign-v2 cutover on 2026-07-09.

The live delivery path is `group_chat.group_ping_loop` → `_sweep_once`, which
is **stateless**: "has this item been pinged?" is a `question`-table lookup,
not a conversation pointer (`group_chat.py:109`). Questions therefore do not
serialize, and the queue-stall failure mode does not apply to them.

**The constraint that does apply: an FYI must never INSERT a `question` row.**
`question` rows drive the loose-reply heuristic (`_handle_question_reply`
assumes a small number of open questions) and the 7-day expiry sweep at
`_sweep_once`'s head. Injecting ~101 no-reply-expected rows per month into
that table would degrade reply matching for the ~95 that do need answers.

FYIs get their own sweep with its own cap, writing no `question` rows. This is
the highest-risk detail in the change and gets a dedicated regression test.

### A fifth suppression path

`_PING_MAX_AGE_HOURS = 72` (`group_chat.py:50`) means a `pending_txn` whose
`created_at` is older than 72 hours is never pinged at all. Combined with
`_MAX_PINGS_PER_SWEEP = 4` every 180s, throughput is ~80/hour — far above the
~7/day this change produces, so the window is not a practical constraint today.
It is left unchanged, but noted: if volume ever exceeds the drain rate, rows
age out silently rather than queueing.

### FYI delivery

An FYI goes to `pending_txn.assigned_to_user_id` — the same routing questions
use today, so an Allison-card charge lands on her phone, not Steven's.

FYIs honor the existing `user_pref` columns: suppressed entirely when
`receives_per_txn = 0`, and held until the end of `quiet_hours` (default
`22:00-07:00`) rather than waking someone for a charge that needs no reply.
Held FYIs coalesce — a single morning message listing what filed overnight
beats seven separate pings. Questions keep their current quiet-hours behavior;
this change does not touch it.

### The Correct button

Reuses the existing category-resolution path in `group_chat.py`
(`_process_answer` → `_file_item`). Correcting an FYI:

- sets `chosen_category` to the new pick and `filed_by` to the tapping user,
- clears `synced_to_ynab_at` so `ynab_writer` re-pushes the correction,
- offers a follow-up: *disable the rule that filed this?*

### `close_stale_pending` narrows

Delete the category-adoption `UPDATE` at `ynab_full_sync.py:562-579`. Once
every row is dispatched, there is nothing legitimate left to adopt — it only
ever fired because rows fell through the cracks.

Keep the `dismissed` branch for genuinely undecidable rows: the ledger txn was
deleted by dedupe/orphan cleanup, or became a linked transfer.

### YNAB-set categories

A category Allison sets directly in the YNAB app still wins — YNAB is upstream
for transaction categories within the rolling 7-day sync window. But it is no
longer silent: `full_sync` adopting a category onto a row with an open question
stamps `filed_by = 'ynab'` and sends an FYI with a `[Correct]` button, same
shape as a rule FYI.

## Data model

One new table, following the `income_source_override` pattern
(`storage.py:99`):

```sql
CREATE TABLE IF NOT EXISTS auto_rule (
  id            INTEGER PRIMARY KEY,
  pattern       TEXT NOT NULL UNIQUE,   -- regex, case-insensitive, vs raw payee
  category_id   TEXT NOT NULL REFERENCES category(id),
  enabled       INTEGER NOT NULL DEFAULT 1,
  note          TEXT,
  created_by    TEXT,                   -- 'seed' | 'steven' | 'allison'
  created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  last_fired_at TIMESTAMP,
  fire_count    INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_auto_rule_enabled ON auto_rule(enabled);
```

**Matching:** first `enabled` rule by ascending `id` wins. No priority column,
no specificity scoring. Overlaps are rejected at save time with the collision
shown, rather than resolved by a hidden algorithm — a wrong filing must always
be explainable by pointing at one rule.

**Discovery is a query, not a table.** `dispatch.suggest_rules()` runs the
majority-vote logic currently in `suggest.py` over 365 days of ledger history,
restricted to payees no enabled rule already matches, requiring ≥3 occurrences
and ≥70% agreement. Nothing is cached; the list recomputes on panel load.

**Normalization** reuses the existing payee-key logic in `suggest.py` — strips
`TST*` / `SQ *` / `PAYPAL *` prefixes, store numbers, and trailing city + `USA`.
A suggested rule's pattern is generated from the normalized key and is editable
before saving.

## Auto-Sync panel

Sidebar: **Core → Auto-Sync**. New route `/auto-sync` in
`ynabhelper-ui/src/App.tsx`, page at `src/pages/AutoSync.tsx`.

**Rules tab** — table of rules sorted by `fire_count` descending, so
workhorses surface and dead rules sink. Columns: pattern, category, fired N
times, last fired, enable/disable toggle, edit, delete. A rule that has not
fired in 6 months is visually de-emphasized.

**Suggestions tab** — the ranked discovery list. Each row: payee, proposed
category, how often it has been filed that way, average amount, last seen, and
a `[Create rule]` button.

**Preview before save** is the safety feature. Creating or editing a rule shows
*the actual transactions it would have matched* over the last 90 days — the
list of payees, dates, and amounts. Not a confidence score, not a match count
in isolation. Per the standing UI principle, the panel shows outcomes and
transactions, never algorithm internals.

**Writes go through the localhost HTTP API** so the bot stays the single
writer. New endpoints in `bot/http_api.py`, all behind `_require_token`:

| Method | Path | Purpose |
|---|---|---|
| GET | `/rules` | list rules with fire stats |
| POST | `/rules` | create (rejects overlapping pattern) |
| PATCH | `/rules/{id}` | edit pattern / category / enabled |
| DELETE | `/rules/{id}` | delete |
| GET | `/rules/suggestions` | discovery list |
| POST | `/rules/preview` | transactions a candidate pattern would match |

Per the known FastAPI constraint, every Pydantic body model lives at module
scope, never inside the `build_app()` closure.

## Migration

1. Add `auto_rule` DDL to `storage.py`.
2. One-shot seed script writes the 40 `payee_overrides.OVERRIDES` entries into
   `auto_rule` with `created_by='seed'`, resolving each `category_name` to an
   id. A rule whose category no longer exists is skipped and reported, not
   silently dropped.
3. `payee_overrides.resolve_payee_override()` reads `auto_rule`.
4. Delete the `OVERRIDES` literal — it is the last thing in the system needing
   a code edit plus a bot restart to change a category.

Existing `pending_txn` rows are left alone. No historical re-filing: bulk
`month_category` recomputes produce phantom Ready-to-Assign movement, and this
change has no reason to touch history.

## Testing

- `dispatch.classify` table test: rule hit → filed + FYI; miss → stays pending
  with `suggested_category` set.
- **Regression guard:** an FYI send inserts no row into `question`. Assert
  `SELECT COUNT(*) FROM question` is unchanged across the FYI sweep, and that
  `_sweep_once` still pings a genuinely unruled row in the same pass.
- A `ynab_sync`-sourced ledger row produces a `pending_txn`.
- `Transfer :%` payees, split parents, split children, and off-budget accounts
  produce no `pending_txn`.
- Rule precedence: lowest enabled `id` wins; disabled rules never fire.
- Overlapping-pattern create is rejected.
- Seed migration produces 40 enabled rules, each resolving to a live
  `category.id`.
- `close_stale_pending` no longer adopts categories; still dismisses rows whose
  ledger txn is gone.
- Existing suites must stay green: `test_categorizer.py`, `test_telegram_bot.py`,
  `test_matcher.py`, `test_storage.py`.

Run `scripts/eval_suggestions.py` before and after — suggestion quality feeds
the pre-filled buttons on ~95 questions/month and must not regress.

## Out of scope

- Amazon: bucket logic, attribution, thresholds, HOLD lane. Owned elsewhere.
- The 2-hour HOT→COLD TTL. Unchanged; COLD plus `/batch` remains a valid
  resting state.
- Backfilling categories on historical transactions.
- Account-scoped or amount-guarded rules.

## Deployment

Code goes live only when the `YNAB-Helper-Bot` scheduled task restarts — the
bot's lifecycle is not managed from a session. The UI panel ships via
`npm run tauri build` plus an exe hot-swap; the mobile web bundle deploys with
`deploy_webui.ps1` and needs no restart.
