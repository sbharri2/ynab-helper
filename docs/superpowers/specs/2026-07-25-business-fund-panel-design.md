# Business Fund gets its own panel

**Date:** 2026-07-25
**Status:** approved, ready for implementation plan

## Problem

The `Business Fund` budget group showed Available **$0.00** while BUSINESS
CHECKING **9649 held **$2,988.53**. That account exists to fund business
expenses, so the budget should reflect what is in it.

Investigation found the envelope was not the real problem.

### The account is clean

Both accounts reconcile exactly to their latest bank observation
(2026-07-25):

| Account | Ledger sum | Observed |
|---|---|---|
| BUSINESS CHECKING **9649 | $2,988.53 | $2,988.53 |
| JOINT CHECKING **9649 | $8,620.06 | $8,620.06 |

No data-integrity problem. Every figure below is bank-confirmed.

### Why Available was $0

July: budgeted $11,320.75 against activity −$8,981.02. A UI budget-set on
2026-07-19 moved the category from $1,310.00 to $8,981.02 — sized to land
Available on exactly $0.00. The overspend was covered, not funded.

July's −$8,981.02 = $779.91 mortgage + $8,201.11 to "Alabama Rental M".

### The actual finding: no income since October 2025

| Period | Inflow | Outflow |
|---|---|---|
| 2025-06 → 2025-10 | $390 / $1,762 / $974 / $211 / $596 | ~−$843/mo |
| 2025-11 → 2026-06 | **$0** every month | −$780 to −$843/mo |
| 2026-07 | $7,000 (transfer from Joint) | −$8,981.02 |

Nine consecutive months of zero rental income against a mortgage drawing
~$780/mo. The only 2026 inflow is the $7,000 moved in from Joint on 07-17 —
itself funded by a $7,000 Marcus withdrawal the same day — to cover the
$8,201.11 payment.

The account is not an under-budgeted business fund. It is being subsidized by
family cash, ~$7,000 in 2026, and the $0 envelope was accidentally telling the
truth. A panel showing income against expense over time would have surfaced
this in December.

### Ready-to-Assign context

July RTA is **−$2,576.77** (on-budget cash $10,825.75 against $13,402.52
promised across envelopes). The business $2,988.53 is part of that cash, so
business money is currently backing family envelopes. Some of
`available_cents` is known-stale `month_category` data, so treat the gap as a
shape, not a precise figure.

## Decisions

1. **Business tracking gets its own panel**, not a synthetic envelope that
   mirrors the account balance.
2. **No nightly anchor job.** The panel reads the balance directly, so a
   mirrored envelope has no job left. This removes the anchor-write background
   task that was otherwise required.
3. **Business cash and the Business Fund group leave the Ready-to-Assign
   identity.** Family RTA reflects only family money; the business fund never
   competes for it.
4. **Panel shows:** balance + runway, income vs expense by month, transaction
   list. (Cumulative family-subsidy total was considered and dropped.)

## Architecture

### Panel

`/budget/business` → `ynabhelper-ui/src/pages/BusinessFund.tsx`, sidebar under
the Budget group alongside Reimbursables. The same bundle serves the mobile
SPA, so it also needs a `/q/` registry entry. No new deploy path.

### Query — `q_business_fund(db_path, month)` in `bot/webui_queries.py`

- **Balance** from `account_balance_observed`, latest `as_of_date` — bank is
  the source of truth. Ledger sum is the fallback. The response carries
  `balance_source` so the panel can print "as of 2026-07-25".
- **Monthly in/out**, trailing 24 months, **transfers excluded from both
  columns** and reported on their own line. Transfers are identified by
  `transfer_account_id IS NOT NULL`, not guessed. This matters: counted as
  income, the $7,000 would make July read as a $1,981 loss instead of the
  $8,981 it was.
- **Runway** = balance ÷ **median** monthly net, transfers excluded. The
  trailing-6 *mean* is −$2,157/mo because the $8,201.11 one-off dominates it,
  giving a misleading 1.4 months. The median is −$779.91 → **3.8 months**,
  which describes the ongoing situation; the one-off gets its own callout. If
  net turns positive the panel reports "funded" rather than a month count.
- **Transactions** — register, newest first, with category name and a transfer
  marker.

### RTA exclusion — the only budget-engine change

In `q_ready_to_assign`:

- `cash_cents` excludes the business account.
- `available_cents` and `assigned_cents` exclude the `Business Fund` group —
  the same mechanism already applied to `Internal Master Category`.

Account id and group name come from config, not hardcoded.

Side benefit: the phantom future-month rows and the $2,339.73 of Joint
Checking mortgage charged to this envelope stop polluting family RTA, because
the entire group leaves the identity.

### Deliberately unchanged

- **No `envelope.py` change.** The panel reads `ledger_txn` and
  `account_balance_observed`, never `month_category`, so stale envelope rows
  cannot reach it.
- The `Business Fund` group is **hidden from the main Budget page** — the
  panel owns it now.

## Known issues left in place

These become cosmetic once the group leaves the RTA identity, and the panel
cannot read them. Listed so they are not forgotten:

- **$2,339.73 of Joint Checking spending charged to the business envelope.**
  Mar 2, Apr 1, May 1 — $779.91 each, paid from *Joint*, categorized to
  `Business Checking`, while Business Checking separately paid its own mortgage
  those months. Both accounts reconcile, so the money genuinely left Joint;
  only the category is in question. This is exactly the negative the category
  carried into June.
- **Phantom future months.** Aug 2026 → Jan 2027 each carry Available
  −$1,559.82 with zero budgeted and zero activity.

## Related finding

The $8,201.11 to "Alabama Rental M" is the wrong sign versus its own history:
that payee appears 39 times since 2022, every one an *inflow* ($96–$2,188 of
rental income). This one is an outflow 4× larger than the biggest prior, and it
was **auto-filed** by memory_v2 as a "strong prior" — an $8k movement no human
reviewed.

This is the same failure mode as
[2026-07-25-large-amazon-attention-design.md](2026-07-25-large-amazon-attention-design.md),
and it is the argument for that spec's deferred "Approach B" (a generic
large-purchase gate across every auto-file path, not just Amazon). Worth
revisiting after the Amazon work lands.

## Testing

`tests/test_webui_queries.py` already exists. Add for `q_business_fund`:

- Balance falls back to ledger sum when no observation exists; `balance_source`
  reports which was used.
- Transfers are excluded from income/expense and reported separately.
- Runway uses the median, not the mean — assert the $8,201.11 one-off does not
  drag the figure to 1.4 months.
- Zero-income runway path, and the "funded" path when net is positive.

Plus an RTA test asserting business cash and the Business Fund group are out of
the identity.

## Deployment

Query changes are picked up by the bot's HTTP API on restart (scheduled task —
do not start it in-session). The UI panel ships via the normal
`npm run tauri build` + exe hot-swap, and the mobile bundle needs no restart.
