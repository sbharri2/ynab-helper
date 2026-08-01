# Amazon parsing panel + the drain model

**Date:** 2026-08-01
**Status:** approved, ready for implementation plan
**Supersedes:** `2026-07-25-large-amazon-attention-design.md` (the ≥$150 hold
path is deleted, not amended)

## Problem

Steven and Allison, 2026-08-01:

> firstly, the desktop will need its own amazon parsing panel that explains
> which items are in 4 buckets, 1) credit card parsed, 2) amazon parsed
> 3) matched 4) unmatched ..... this will help me troubleshoot the matching
> process. unmatched will be if the items after 4 days have not matched. on the
> telegram side, we decided that we will get messages in our group chat for
> matched items that will tell us the charge and the description of items in it.
> we will also get messages if after 4 days a credit card item is unmatched and
> ask if we remember what the item was and can still categorize it or need to
> investigate on the desktop.

and:

> we decided we will have a amazon uncategorized category... we are going to
> remove the personal amazon categories. the goal is to get as many items out of
> the amazon uncategorized category

This is a reversal of the 2026-07-03 auto-bucket design. That design treated the
per-person bucket as a **destination** — the charge's final resting place, with
the receipt used only for attribution. The new model treats the holding category
as a **staging area** that the household actively drains, with matching as the
tool that makes draining easy.

Two things drove it:

1. **~$500/mo never reaches a real envelope.** June $497.40, July $491.41 across
   the three buckets — roughly $6k/yr of spending with no budget meaning.
2. **Matching is currently un-troubleshootable.** See below.

## The blocking finding: no match is ever written down

`_enrich_from_pending_order()` (`bot/ingest.py:860`) scores every recent
`pending_order` against an incoming charge, picks the best, merges its
`raw_summary` into the memo, and **returns it without persisting the link**.

- `ledger_txn` has no order reference (`id, account_id, posted_date,
  amount_cents, payee, memo, category_id, cleared, source_signal,
  source_email_id, ynab_txn_id, dedupe_key, created_at, updated_at,
  transfer_account_id, transfer_transaction_id, parent_txn_id, is_split`).
- `pending_order.status` is left `pending` — 58 of 74 rows, including orders
  that demonstrably matched.
- The only trace is an `audit_log` row (`ingest_enriched_from_order`) carrying
  `matched_order_id` **but not the ledger row it matched**, so it cannot even be
  joined after the fact.

"Matched" is therefore not a queryable state. The panel's third bucket has no
table to read, and no diagnosis of the matching process is possible today. This
is the foundation and must be built first.

## Sizing

Amazon CC charges vs. parsed orders, by month:

| | Apr | May | Jun | Jul |
|---|---|---|---|---|
| CC charges | 17 | 23 | 16 | 17 |
| Orders parsed | 8 | 5 | 10 | **18** |

May's 5-for-23 is the era before the multi-order parser fix (2026-07-26, commit
`2ff8bcf`); July is near-parity because that fix landed. **A match rate computed
over history will look far worse than what the system does now** — the panel
must show the month, not one lifetime number, or it will misrepresent the fix.

Telegram load at July volume: ~16–20 matched pings/mo plus unmatched pings —
about **5 messages/week** to the group.

## Decisions

1. **One holding category, not three.** `Amazon Uncategorized` replaces
   `Amazon - Steven` / `Amazon - Allison` / `Amazon - Unassigned`.
2. **Every Amazon charge lands there.** No dollar threshold, no HOLD lane, no
   exceptions.
3. **Person attribution survives** in the ledger memo and as a filterable column
   in the panel — not as a category. It now survives an item draining out to a
   real category, which the bucket model never allowed.
4. **The match gets persisted**, at every site that computes one.
5. **The panel is a drain surface with matching diagnostics attached**, not a
   tracker.
6. **Historical rows stay pointed at the old buckets.** June/July reporting is
   unchanged and `month_category` is not bulk-recomputed — the standing
   never-backfill rule (measured at +$10.5k phantom RTA) holds.

## Phasing

This is one design but four separable deliverables, and the order is forced by
the link:

1. **Link** — schema, write sites, backfill. Nothing user-visible; everything
   else depends on it.
2. **Cutover** — `Amazon Uncategorized`, the envelope moves, the ingest
   simplification, the deletions.
3. **Panel** — the four views.
4. **Telegram** — the two pings.

Phases 1 and 2 are independently shippable and independently reversible. The
panel is usable without the pings; the pings are not useful without the panel to
send people to.

## Architecture

### 1. The link — schema

```sql
ALTER TABLE ledger_txn ADD COLUMN pending_order_id INTEGER;  -- FK pending_order.id
ALTER TABLE ledger_txn ADD COLUMN match_score REAL;
CREATE INDEX IF NOT EXISTS ix_ledger_txn_pending_order
  ON ledger_txn(pending_order_id);
```

Nullable, no FK constraint (consistent with the rest of the schema). Many
charges may point at one order — split shipments are real and the panel must be
able to show them.

`pending_order.status` flips to `'matched'` when a link is written. Existing
statuses (`pending`, `matched`, `categorized`, `expired`) are unchanged
otherwise.

Not modelled: match method (ingest / retro / manual). It is already in
`audit_log` and no view needs to filter on it.

### 2. The link — write sites

All three paths that currently compute a match must persist it:

- **`bot/ingest.py:211`** — `_enrich_from_pending_order()` returns the order and
  its score; the caller writes both columns on the `ledger_txn` insert.
  `_enrich_from_pending_order` itself stays a pure scorer that also mutates
  `parsed["summary"]` — it does not gain DB writes.
- **`retro_bucket_amazon_order()` (`bot/ingest.py:939`)** — the sweep that
  matches a late-arriving order to an already-ingested charge. It writes the
  link; its ≥$150 skip guard is deleted (see §5).
- **Manual match from the panel** — `POST /amazon/match` (new).

### 3. The link — backfill

`scripts/backfill_amazon_matches.py`: re-run `bot.matcher.find_best_match` over
every Amazon `ledger_txn` and the `pending_order` rows within the matcher's
existing 21-day lookback, and write the links it finds.

- Writes **only** `pending_order_id`, `match_score`, and
  `pending_order.status`. No category changes, no `month_category` touch, no
  YNAB calls.
- `--dry-run` default, prints the count and a sample; `--apply` commits.
- Idempotent: skips rows that already carry a link.
- Per the standing rule on bulk DB work, run against a local DB copy and swap
  back if it is slow.

### 4. Cutover — buckets to `Amazon Uncategorized`

**Create** `Amazon Uncategorized` under the **Day to Day Expenses** group. Not
under Personal Spending — that group's panel infers an owner from the category
name, and this category has no owner by definition. `is_spending = 0`, so the
LLM cannot route anything into it (the flag means "LLM-suggestable", not "real
expense").

**Move the money.** `/envelope/move` rejects non-positive `cents`, and two of
the three buckets carry negative available, so this is three ordered moves, not
one sweep. Current values:

| Category | Available (Aug onward) |
|---|---|
| Amazon - Steven | +$1,288.18 |
| Amazon - Allison | −$305.78 |
| Amazon - Unassigned | −$192.50 |
| **net** | **+$789.90** |

Order:

1. Amazon - Steven → Amazon Uncategorized, $1,288.18. Steven goes to $0.
2. Amazon Uncategorized → Amazon - Allison, $305.78. Allison goes to $0.
3. Amazon Uncategorized → Amazon - Unassigned, $192.50. Unassigned goes to $0.

Amazon Uncategorized lands at **+$789.90**, all three buckets at $0.

$853.58 of Steven's balance is orphaned budget from the 2026-07-25 large-charge
repair — the $815.09 charge moved to Steven Personal Savings while the budgeted
amount stayed behind. Rolling it into the drain category is the point: as an
item moves out to a real envelope, budget can follow it.

**Known inconsistency — the script must not hardcode.** Amazon - Allison ends
July at −$39.64 but carries −$305.78 into August, which does not chain. The
cleanup script reads live `month_category` values at execution time, prints a
before/after table, and requires explicit `--apply` after Steven reads it.

**Hide, don't delete.** Set `category.hidden = 1` on all three. The 41
historical ledger rows (2021-11-09 → 2026-07-29) keep pointing at them.

### 5. Ingest — one path

`bot/ingest.py:231` becomes unconditional for Amazon:

```python
if is_amazon:
    ledger_category_id = _amazon_uncategorized_category(db_path)
```

The person still resolves from the matched order's `assigned_to_user_id` and is
written into the memo alongside the item summary. It no longer selects a
category.

**Deleted:**

- the `is_large_amazon` fork and `LARGE_AMAZON_DEFAULT_CENTS` /
  `amazon.large_charge_cents`
- the ≥$150 HOLD-lane path and its 24h TTL in `bot/queue_lane.py`
- `AMAZON_HOLD_TTL_DAYS = 14` and the `amazon_aged_out` branch of
  `abandon_stale_holds()`
- `retro_bucket_amazon_order()`'s threshold guard (the function itself stays and
  gains the link write)
- `bot/amazon_tracker.py` entirely, including `send_aged_out_alert_if_new`
- the `/amazon` command handler and its registration
  (`bot/telegram_bot.py:2181`, `:2401`)
- `batch_processor.build_amazon_batch` / `count_amazon_ready`, and the
  `header_label == "Amazon"` branch and `amazon_ready` footer counters
  (`bot/telegram_bot.py:1306`, `:1360`, `:1467`)
- `_amazon_bucket_category()`

The 5 stale HOLD rows from early July (pt 1011, 1042, 1068, 1069, 1070) are
resolved by the cutover script: they are re-filed to `Amazon Uncategorized` and
their `pending_txn` rows closed.

### 6. HTTP API

Read (all `GET`, token-guarded, backed by `bot/webui_queries.py`):

- `GET /amazon/overview?month=YYYY-MM` — header numbers: Amazon Uncategorized
  available, row count and dollars still to drain, and per-month
  charges/orders/matched counts for the last 6 months.
- `GET /amazon/charges?month=` — Amazon `ledger_txn` rows: date, amount,
  account, payee, category, person, match state, linked order.
- `GET /amazon/orders?month=` — `pending_order` rows where `source='amazon'`:
  date, total, items, person, match state, linked charges.
- `GET /amazon/matched?month=` — the join: charge + order items + person.
- `GET /amazon/unmatched` — **two-sided**: charges older than 4 days with
  `pending_order_id IS NULL`, and orders older than 4 days with no charge
  pointing at them.

Write:

- `POST /amazon/match {ledger_txn_id, pending_order_id}` — manual link.
- `POST /amazon/unmatch {ledger_txn_id}` — clear a bad link. Reverts
  `pending_order.status` to `pending` when no charges remain linked.

Recategorization reuses the existing `POST /categorize` — no new endpoint.

Per the standing FastAPI rule, every Pydantic body model lives at **module
scope**, never inside `build_app()`.

### 7. The panel

`src/pages/AmazonParsing.tsx`, route `/amazon`, in the **Core** sidebar group
next to Reconciler — it is primarily a diagnostic surface, and Reconciler is the
established precedent for read-plus-one-action diagnostics.

Header: Amazon Uncategorized available, count of rows still in it, dollars still
to drain, and a small per-month charges/orders/matched bar for the last 6
months (so the July parser fix is visible rather than averaged away).

Four views, tab-switched:

| View | Rows | Actions |
|---|---|---|
| **Charges** | every Amazon ledger row — date, amount, account, category, who, match state | recategorize |
| **Orders** | every parsed order — date, total, items, who, match state | — |
| **Matched** | the pairs: charge + item text + who | recategorize (**this is the drain**) |
| **Unmatched** | charges >4d unlinked **and** orders >4d unlinked | manual match, recategorize |

Unmatched is two-sided by design. Steven specified the charge side; a parsed
order with no charge is the other half of any matching bug and the panel cannot
diagnose matching with one side visible.

A **Who** column (Steven / Allison / —) is filterable and totals per person,
replacing what the per-person categories used to show — and unlike the buckets,
it keeps working after an item drains out to Household Items.

Recategorization uses `CategoryPicker` directly in a modal — **not** the shared
`CategoryActivityModal`, which is keyed on a category and a month, whereas these
views are keyed on a charge. Per the standing UI rule, the panel shows
transactions and outcomes — **not** matcher scores, weights, or breakdowns.
`match_score` is stored for diagnosis but is never rendered.

**Route collision.** The SPA route `/amazon` is served by the bot's catch-all
`GET /{full_path:path}`, while the API routes `/amazon/*` are declared earlier
in `build_app()` and therefore win. New API routes must stay above the catch-all
or the panel will silently receive `index.html` instead of JSON.

### 8. Telegram

Two pings, both to the household group, both fire-and-forget per the standing
rule that a question is not a lock.

**Matched** — fires when the receipt is linked, whether at ingest or later via
the retro sweep:

```
🅰 $47.32 Amazon (7/29) — 4 Drugstore, Kitchen items · Allison
Reply with a category to file it.
```

**Unmatched at 4 days** — a daily sweep, replacing the deleted
`send_aged_out_alert_if_new` hook in the same loop:

```
❓ $23.58 Amazon (7/25) — no receipt after 4 days.
Remember what this was? Reply with a category, or open the Amazon panel.
```

Replies file through the existing reply intercept. Ignoring costs nothing — the
charge is already in Amazon Uncategorized either way.

**Dedup:** one ping per charge, ever, per type. Audit events
`amazon_matched_pinged` / `amazon_unmatched_pinged` keyed on `ledger_txn_id` are
the dedup signal, matching the existing `amazon_aged_out_alerted` pattern.

**The 4-day window is an asking deadline, not a matching deadline.** The matcher
looks back 21 days, so a late order email can still link a charge that was
already pinged as unmatched. The panel will show a row move from Unmatched to
Matched after the fact. That is correct behaviour, and the matched ping still
fires (it is deduped separately from the unmatched one).

## Consequences

- **Every Amazon charge is now a hole in the budget until someone acts.** That
  is the intent — it is what makes the category drain — but it is a real
  reversal of "review by exception" for this one merchant, and Amazon
  Uncategorized will carry a visible balance most of the time.
- **The 2026-07-25 large-charge work is deleted 6 days after shipping.** It
  solved "a bucket is a holding pen, not a budget category" with a threshold;
  this solves it for every charge, making the threshold redundant.
- **Person rollups change shape.** Per-person Amazon totals come from the panel
  and memo rather than from category activity, so they are no longer visible in
  the Personal Spending tab.
- **The buckets leave the category picker for free.** `/categories` filters on
  `hidden = 0`, so hiding them removes them as a manual filing target with no
  extra work. `Amazon Uncategorized` does appear in the picker (the endpoint
  does not filter on `is_spending`), which is wanted — it lets a mis-filed
  charge be put back.
- **YNAB push behaviour is unchanged.** `q_unsynced` reads
  `ledger_txn.category_id`, which is now always set for Amazon, so charges
  appear in the Sync-to-YNAB panel immediately — categorized as Amazon
  Uncategorized until drained.

## Known gaps

- **Item text is often useless.** Amazon's confirmation emails frequently give
  rollups ("4 Apparel and Arts & Crafts items", "4 Drugstore, Kitchen, and other
  items") rather than product names. A matched ping carrying that text is barely
  more actionable than no text. Nothing in this design fixes it; the panel makes
  it visible, which is a prerequisite for anyone deciding to.
- **Refunds.** An Amazon inflow lands in Amazon Uncategorized like everything
  else and must be drained by hand. Measured volume is trivial (every Amazon
  inflow since 2024: $21.44, $16.08, $10.71, $1.46).
- **Split shipments still defeat the matcher.** A $600 order arriving as three
  $200 charges matches none of them — the tolerance is ±min(10%, $10) against
  the order total. The schema now permits many charges per order, so a manual
  match in the panel can express it, but nothing detects it automatically.
- **Amazon-only.** Target and Costco have the same shape. Not built.

## Testing

`tests/test_amazon_matching.py` (new). The lane state machine was untested and
is now largely deleted, so the tests target the link and the sweeps:

- **Link persistence:** an ingest with a matching order writes
  `pending_order_id` + `match_score` and flips the order to `matched`; an ingest
  with no match leaves both NULL.
- **Retro link:** an order arriving after the charge links it, and pings.
- **No threshold:** an $815 charge and a $12 charge both land in Amazon
  Uncategorized, and neither creates a HOLD `pending_txn`.
- **Unmatched sweep:** a charge at 3 days does not ping; at 4 days it does; at 5
  days it does not ping a second time.
- **Ping dedup:** matched and unmatched pings for the same charge are
  independent — a late match still pings after an unmatched ping fired.
- **Manual match/unmatch:** `POST /amazon/match` links, `POST /amazon/unmatch`
  clears and reverts order status only when no charges remain linked.
- **Backfill idempotence:** a second run writes nothing.

Existing `tests/test_large_amazon.py` is deleted with the code it covers.

## Deployment

Ordered, because the cutover crosses both processes:

1. Schema migration + `scripts/backfill_amazon_matches.py --apply`. No restart
   needed; read-mostly.
2. `scripts/retire_amazon_buckets.py --apply` — creates the category, does the
   three ordered envelope moves, hides the buckets, re-files the 5 stale HOLD
   rows. Prints before/after and requires explicit approval.
3. Bot code (ingest, sweeps, Telegram, HTTP API) goes live **only** when the
   `YNAB-Helper-Bot` scheduled task restarts. Never PID-kill or start the bot
   in-session — a parallel agent may be deploying.
4. Desktop UI: `npm run tauri build`, hot-swap the exe over the install dir.
   Mobile web: `deploy_webui.ps1`, no restart.

Steps 1 and 2 are safe to run before the restart: the old ingest path writes to
`_amazon_bucket_category()`, which after step 2 resolves to hidden categories —
so between step 2 and step 3 any new Amazon charge files into a hidden bucket
and must be re-filed. Keep the gap short, or run steps 1–2 immediately before
the restart window.
