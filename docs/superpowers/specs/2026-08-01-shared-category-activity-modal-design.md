# Shared category-activity modal

**Date:** 2026-08-01
**Status:** approved, ready for implementation plan

## Problem

Steven, 2026-08-01:

> on personal expenses, when i click on an item and want to see the activity for
> that category, it pops up a window... i need the ability to reallocate those
> items to different categories if they are incorrect. [...] i want the budget
> panel to work like the expenses panel when you click on a category it pops up
> the window of activity that allows for moving items as just described.

Both halves already half-exist, in opposite ways:

| Panel | Drill-down | Recategorize | Opens from |
|---|---|---|---|
| `Budget.tsx` | yes (`ActivityModal`, `:915`) | **yes** | the Activity **number** only, and only when `activity_cents != 0` |
| `PersonalExpenses.tsx` | yes (`:342`) | **no** — read-only table | the category row |

So Budget has the capability with poor discoverability, and Personal Expenses
has the discoverability with no capability. This is one shared component, not
two features.

The other six budget sub-panels (Day-to-Day, Monthly Bills, Savings, Seasonal
Funds, Credit Cards, Reimbursables) have no drill-down at all. **Out of scope** —
noted only so the component is built to be reusable when they want one.

## Design

Extract `Budget.tsx:915 ActivityModal` into
`src/components/CategoryActivityModal.tsx`. Behaviour is Budget's current
behaviour, which already works:

1. Modal opens on a category, fetches that month's transactions via
   `getTransactions({ category_id, since, until })`.
2. Clicking a transaction swaps the list for a `CategoryPicker` (with the
   current category excluded) plus a "← Back to the list" affordance.
3. Picking fires `categorizeTransaction({ ledger_txn_id, category_id })` →
   `POST /categorize`.

### Props

```ts
interface Props {
  month: string;              // "YYYY-MM"
  categoryId: string;
  categoryName: string;
  activityCents: number;      // for the subtitle
  onClose: () => void;
}
```

Deliberately **not** passing the whole `MonthCategoryRow` — Budget and Personal
Expenses have different row types (`MonthCategoryRow` vs `PersonalRow`), and
the modal only needs these four fields. Each caller destructures at the call
site.

### Query invalidation

The two panels cache under different keys, and a move from either must refresh
both. The shared component invalidates the union:

```
["activity_txns"]     // Budget's drill list
["personal_txns"]     // Personal Expenses' drill list
["personal"]          // Personal Expenses' month rollup
["month_categories"]  // every budget panel's row data
["transactions"]      // the Transactions register
["ready_to_assign"]   // header cards everywhere
```

Per the standing rule that every mutation invalidates `ready_to_assign` and
`month_categories`.

### Transaction row rendering

Budget's list shows payee / date · account / amount. Personal Expenses' table
also shows **memo**, which is real information and must not regress. The shared
row renders memo as a muted second line when non-empty, and omits the line
entirely when it isn't — so Budget's list looks essentially unchanged and
Personal Expenses keeps everything it had.

Personal Expenses' current table columns (Date, Payee, Account, Memo, Amount)
collapse into the shared list layout. This is a deliberate visual change to
that panel: the list form leaves room for the picker to take over the modal
body, which a five-column table does not.

## Call-site changes

### `Budget.tsx`

- Delete the local `ActivityModal` (`:915–1020`); import the shared one.
- Keep the existing Activity-number click target (`:583`).
- **Add** the category name as a second trigger for the same modal.
- The name trigger is live even when `activity_cents == 0` — the current
  number-only trigger is suppressed at zero, and "no transactions this month"
  is a legitimate answer to a click, not a reason to make the click dead.

### `PersonalExpenses.tsx`

- Delete the read-only drill `<Modal>` (`:342–391`) and the `drillTxns` query
  (`:105`); the shared component owns both.
- The existing row click keeps setting `drill`; it now renders the shared modal.

## Testing

The UI has no component-test harness, so this is verified by running the app:

1. Budget → click a category **name** → modal opens with that month's
   transactions.
2. Budget → click a category **Activity number** → same modal (unchanged path).
3. Budget → click a category name with zero activity → modal opens, shows the
   empty state.
4. Personal Expenses → click a category → modal opens → click a transaction →
   pick a new category → row disappears from the list, the panel's totals move,
   and the header's Ready to Assign updates without a manual refresh.
5. Same move done from Budget → verify Personal Expenses reflects it on next
   visit (proves the shared invalidation set).

## Deployment

UI-only. No bot changes, no bot restart, no schema change.

- Desktop: `npm run tauri build`, then hot-swap the exe over the install dir at
  `AppData\Local` (no reinstall).
- Mobile web: `deploy_webui.ps1` — bundle update only, no bot restart.
