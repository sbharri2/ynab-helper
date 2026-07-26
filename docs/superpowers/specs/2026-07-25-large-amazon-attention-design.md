# Large Amazon purchases get a real category

**Date:** 2026-07-25
**Status:** approved, ready for implementation plan

## Problem

Amazon charges auto-file into per-person buckets (`Amazon - Steven` /
`Amazon - Allison` / `Amazon - Unassigned`) with no confirm step. That trade —
zero friction for zero categorization — is right for the ~95% of Amazon
activity that is small and unremarkable. It stops paying above roughly $150.

The trigger case: a **$815.09** charge on 2026-07-22. The pipeline handled it
exactly as designed — ingested from a Chase alert with no order match, then
`amazon_retro_bucket` re-filed it to `Amazon - Steven` eight minutes later when
the order email arrived. Attribution worked. But `Amazon - Steven` is a holding
pen, not a budget category, so an $815 electronics purchase never touched a real
envelope.

Compare how the same-size charges were handled before the buckets existed:

| Date | Amount | Category |
|---|---|---|
| 2026-01-22 | $182.31 | Steven Personal Savings |
| 2026-03-25 | $233.78 | Household Items |
| 2026-04-29 | $250.69 | Home Maintenance and Improvement |
| 2026-07-06 | $278.82 | **Amazon - Unassigned** |
| 2026-07-22 | $815.09 | **Amazon - Steven** |

## Sizing

Amazon charges at or above $150, by year:

| 2021 | 2022 | 2023 | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|
| 9 | 7 | 6 | 6 | 5 | 5 |

**One question every 6–10 weeks.** That is the entire ongoing cost. The buckets
keep absorbing everything else.

## Decisions

1. **A large Amazon charge lands in a real budget category**, not a person
   bucket. Person attribution becomes secondary — it survives in the memo.
2. **It is held uncategorized until answered.** No auto-file to a bucket with a
   later upgrade. A $400 hole in the budget is supposed to nag.
3. **The bot waits for the receipt before asking**, up to 24h, so the question
   can carry item text and the person. After 24h it asks anyway with amount and
   date alone.
4. **The two existing over-threshold rows are raised** for real categorization.

This is a deliberate, narrow reversal of the redesign-v2 auto-file rule. The
2026-06-26 HOLD-lane directive ("never auto-process Amazon without order
details") was abandoned on 2026-07-03 because applying it to *all* Amazon
manufactured a manual queue. Scoped to ~6 charges a year, the same mechanism is
cheap.

## Architecture

The HOLD lane already implements "wait for the receipt, then ask." It still runs
every 30 minutes via `sweep_lanes()`; nothing has fed it since 2026-07-03. This
design revives it, gated on dollar amount.

### Threshold

`amazon.large_charge_cents` in `config.yaml`, default `15000`.

The test is `amount_cents <= -15000` — signed, not `abs()`, so refunds never
trigger a question. Per **charge**, not per order.

### Ingest fork — `bot/ingest.py`

- `:225` — `if is_amazon:` becomes `if is_amazon and not is_large:`. Small
  charges keep the current bucket path unchanged.
- Large charges fall through to the normal categorizer path:
  `ledger_txn.category_id` stays NULL, a suggestion is computed for the eventual
  prompt.
- `:335` — the `not is_amazon` gate becomes `not is_amazon or is_large`, so a
  large charge gets a `pending_txn`, placed in the **HOLD** lane.
- **Nothing auto-commits above the threshold** — not a confirmed prior, not the
  matched order's `chosen_category`, not a payee override. This is a real (if
  small) loss of convenience, accepted knowingly.

### Waiting — `bot/queue_lane.py`

`promote_holds_to_hot()` already re-runs the matcher, copies enriched item text
onto the row, reassigns to the ordering person (`:227`), and flips HOLD→HOT.
Two changes:

- Adopt the matched order's `suggested_category` when there is no confirmed
  `chosen_category`. Order 83 carried a 0.6-confidence suggestion that never
  reached the user.
- Large-Amazon holds expire at **24h**, not the 14-day Amazon TTL. The
  distinction is derivable from payee + amount — **no schema migration**.

### Expiry

Today an expired Amazon hold drops to COLD with an `amazon_aged_out` alert. A
large charge instead promotes to **HOT** — ask anyway. It then follows the
normal HOT→COLD path after 2h, so an unanswered question settles in the Inbox
and stays there.

### Retro-bucket guard — `bot/ingest.py:939`

`retro_bucket_amazon_order()` is what filed the $815 eight minutes after the
charge. It must skip charges at or above the threshold.

### One-shot repair

Clear `category_id` on ledger rows **24938** ($815.09, `Amazon - Steven`) and
**24779** ($278.82, `Amazon - Unassigned`), enqueue both as COLD `pending_txn`
rows so they surface in the Inbox, then call
`envelope.apply_activity_delta(db, '2026-07', [amazon_steven, amazon_unassigned])`.

`apply_activity_delta` (`envelope.py:282`) is documented as the
anchor-preserving replacement for chain recompute after a recategorization,
which is exactly this case. Deliberately **not** `recompute_month` — that call
rebuilds `available` from the identity and trampled the anchor writes on
2026-07-25 (`envelope.py:301`), and bulk month recomputes are barred by
standing rule.

## Consequences

- **The LLM cannot undo this.** The buckets are `is_spending=0`, so they are
  excluded from the categorizer's candidate pool. A large charge on the normal
  path structurally cannot be suggested back into `Amazon - Steven`.
- **Person survives in the memo** when the receipt matched. The bucket rollup is
  lost for big items — that is the point — but the fact is not.
- **YNAB push waits.** `q_unsynced` reads `ledger_txn.category_id`, so a held
  charge stays out of the Sync-to-YNAB panel until answered.
- **Dead code.** `AMAZON_HOLD_TTL_DAYS = 14` and the `amazon_aged_out` branch of
  `abandon_stale_holds()` become unreachable; delete both. `amazon_tracker.py`
  and `/amazon` go fully dark, but retiring them is a separate decision
  (redesign-v2 already wants them gone) and is out of scope here.

## Known gaps

- **Refund asymmetry.** A returned $815 item would credit `Amazon - Steven`
  while the original charge sits in a real category. Measured: every Amazon
  inflow since 2024 is $21.44, $16.08, $10.71, $1.46. Not built for.
- **Split shipments.** A $600 order shipping as three $200 charges yields three
  questions, none of which match the order (the matcher's ±min(10%, $10)
  tolerance rejects a $200 charge against a $600 order), so all three ask blind
  after 24h. Conversely a $170 order arriving as $90 + $80 slips under entirely.
  Both are accepted consequences of per-charge thresholding.
- **Amazon-only.** The same complaint could arise at Target or Costco. A generic
  large-purchase gate over every auto-file path is the natural follow-on; not
  built now.

## Testing

There is no `test_ingest.py` and no `test_queue_lane.py` — the lane state
machine is entirely untested, which matters because this revives it. New
`tests/test_large_amazon.py`:

- **Boundary:** $149.99 buckets; $150.00 holds; a +$815 refund buckets.
- **Fork:** a large charge leaves `ledger_txn.category_id` NULL and creates a
  HOLD `pending_txn`; a small charge does the opposite.
- **No auto-commit above threshold**, even when the matched order carries a
  `chosen_category`.
- **`retro_bucket_amazon_order()` skips** a large charge.
- **Lane transitions:** receipt arrives → HOT with item text and the correct
  assignee; no receipt after 24h → HOT, not COLD.

## Deployment

Ingest and lane changes go live only when the **YNAB-Helper-Bot scheduled task**
restarts. Do not PID-kill or start the bot in-session. The one-shot repair
script runs independently and needs no restart.
