# Personal Expenses panel

**Date:** 2026-07-25
**Status:** approved

## Problem

Nothing in the app totals budgeted or activity per person. Two views
aggregate by owner — the Budget page's Personal Spending group header and
the `/budget/personal` panel — and **both sum `available_cents` only**.
Neither answers "what did Steven spend on himself this month, and is that
typical?"

Both are also single-month, so a spike is invisible: Steven's January 2026
personal spend was $2,924 against a ~$450 median. Nothing surfaces that.

## Scope

Categories in the **`Personal Spending`** and **`Individual Vacations`**
groups. Owner is inferred from the category name.

Deliberately excluded:

- **`Reimbursables`** — passthrough. Steven's shows $11,782 outflow over
  12 months, nearly all of which came back. Including it would make him
  look like he outspends Allison 6:1.
- **`Investments`** — `Steven Retirement Investment` is a single $20,000
  transfer. Not spending.
- **`Personal Business`** — `Allison Cross Stitch` and `Steven Writing`
  are side-project ledgers, not personal consumption (Steven, 2026-07-25).

Scoping by name alone across all groups gives Steven $40,680 vs Allison
$6,919, which is meaningless. The chosen scope gives **Steven $8,898 vs
Allison $6,457** over the trailing 12 months.

## Layout

Replaces `/budget/personal`. `PersonalSpending.tsx` becomes
`PersonalExpenses.tsx`; the sidebar item is renamed. No other route links
to the old path.

1. **Stat tiles** — Steven / Allison / gap for the selected month.
2. **12-month trend** — grouped bars, one pair of bars per month.
3. **Per-owner sections** — an owner header carrying a subtotal of
   **budgeted, activity and available** (today's views total available
   only; this is the gap being closed), then a table of that person's
   categories with budgeted / activity / 6-mo average / available.
   Clicking a category opens its transactions for the month, reusing the
   drill-down already in `PersonalSpending.tsx`.

Owners render in a fixed order — Steven, Allison, then Joint — rather
than whatever order the rows arrive in. Joint is only `Amazon -
Unassigned` and is omitted when it has no activity.

## Queries

Two queries, each written twice: `ynabhelper-ui/src-tauri/src/commands.rs`
(Tauri IPC) and `bot/webui_queries.py` (HTTP, for the mobile SPA). The
query layer is duplicated by design; both must stay in sync, and
`webui_queries.py` must be added to `REGISTRY` or the mobile page renders
empty.

**`q_personal_expenses_month(month)`** — widen the existing
`q_personal_spending_panel` from one group to both and keep its shape:
per category id, name, owner, budgeted_cents, activity_cents,
available_cents, avg6_cents.

**`q_personal_expenses_trend(months)`** — new. Sums outflows per
`(month, owner)` from `ledger_txn` over the trailing N months.

### Money rules

The trend query aggregates raw ledger rows, so it must apply the
household's counting rules explicitly:

- `amount_cents < 0` — outflows only; a transfer into Personal Savings is
  not spending.
- `payee NOT LIKE 'Transfer :%'` — credit cards are paid in full monthly,
  so transfer rows are movement, not spend.
- `is_split = 0` — splits are stored as parent + child rows; counting
  both double-counts.
- `account_id IN (SELECT id FROM account WHERE on_budget = 1)` — the
  off-budget investment accounts must not leak in.

The month query reads `month_category` and inherits its rules, so it
needs none of the above.

## Testing

`tests/test_webui_queries.py` gains cases for both Python queries,
including a `Transfer :` row and a split parent that must not be counted,
and a category in a group outside the scope that must not appear.

## Consequences

- The two **Personal Savings** envelopes dominate the panel — 162 and 104
  transactions and the largest dollars on both sides. They are being used
  as each person's day-to-day spending account rather than as savings.
  Correct to include for a spending comparison, but the panel is largely
  those two envelopes.
- The per-person subtotal on the **Budget page group header** stays
  available-only. It is a glance, not an analysis; the deep numbers live
  here.
- Owner inference remains name-based in a third place. If it ever moves
  to a real `category.owner` column, all three sites change together.
