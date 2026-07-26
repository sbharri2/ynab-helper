# Personal category reorg + per-person subtotal

**Date:** 2026-07-25
**Status:** approved

## Problem

Grooming spend is filed as fixed bills. `Steven Haircut` and
`Allison Haircut and Perm and Color` sit in **Monthly Bills** next to the
mortgage and the electric bill, which is wrong — they are discretionary
per-person spending. Allison's nails have no envelope at all. `Exercise`
sits in **Day to Day Expenses** alongside groceries, when it belongs with
the other recreation envelopes.

Separately, there is no way to see Steven's personal spending totalled
against Allison's on the main Budget page.

## Decisions

### Category moves

| Category | From | To |
|---|---|---|
| `Steven Haircut` | Monthly Bills | Personal Spending |
| `Allison Haircut and Perm and Color` | Monthly Bills | Personal Spending |
| `Exercise` | Day to Day Expenses | Hobbies |
| `Allison Nails` *(new)* | — | Personal Spending |

"Personal Spending" (`689417f2-afcf-4a6c-a94a-265818711cac`) is the
existing group holding the Amazon per-person buckets and the two Personal
Savings envelopes. No new group is created — a standalone "Allison Nails"
group was considered and rejected, because the per-person subtotal reads
one group and a separate group would not roll into it.

### `is_spending` flips to 1

Both haircuts are currently `is_spending = 0`, inherited from their life
as bills. All three grooming categories get `is_spending = 1` so the
categorizer can suggest them for a salon charge. This also makes them
eligible for "Reconcile month" to cover from, which is correct for
discretionary envelopes. `Exercise` is already `1`; no change.

Note `is_spending` means "LLM-suggestable variable category", not "is a
real expense" — see `project_is_spending_overloaded`.

### Per-person subtotal on the main Budget page

`Budget.tsx` renders a subtotal line under the **Personal Spending**
group header only:

```
Personal Spending
  Steven  $312 available   Allison  $184 available   Joint  $40 available
```

Owner is inferred from the category name using the same rule the existing
Personal Spending panel uses (`commands.rs:1149` — name contains
"steven"/"allison", else "Joint"). This keeps the two views agreeing
without a second source of truth.

The change is entirely client-side. `q_month_categories` already returns
`category_name` and `group_name` per row, so there is no SQL to write —
which matters, because the query layer is duplicated across
`src-tauri/src/commands.rs` and `bot/webui_queries.py` and any new query
would have to be written twice. `webui/` is a built bundle of
`ynabhelper-ui/src`, so one edit ships to both the desktop app and the
mobile SPA.

## Consequences

- **The haircuts leave the Monthly Bills panel** (`commands.rs:1021`,
  `WHERE g.name = 'Monthly Bills'`). They are recurring fixed costs, so
  they stop appearing in the bills view. Accepted.
- **Exercise leaves the Day-to-Day panel** (`commands.rs:1750`). Hobbies
  has no dedicated panel, so Exercise appears only on the main Budget
  page and the Treemap. Accepted.
- Treemap colors by group name (`Treemap.tsx:32-50`); Personal Spending
  is mapped, Hobbies is not and falls through to the default.

## Safety

Moving a category between groups is an `UPDATE category SET group_id`.
`month_category` is keyed by `(month, category_id)`, so no budget history
is touched and nothing needs recomputing — this is important, because a
bulk `month_category` recompute injects phantom Ready-to-Assign (see
`project_never_backfill_month_category`).

The new category is created through the existing `POST /category/create`
(`bot/http_api.py:406`), which mints a `local-{uuid}` id, leaves
`ynab_category_id` NULL, and seeds **only the current month's** envelope.

`bot/ynab_full_sync.py` never writes to `category` or `category_group`,
so a sync will not revert any of this. The one destructive writer is
`scripts/import_ynab_history.py:145,173` (`INSERT OR REPLACE`, all
columns) — it is not scheduled, and re-running it would undo these moves.
