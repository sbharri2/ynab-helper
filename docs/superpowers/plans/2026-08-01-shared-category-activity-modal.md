# Shared Category-Activity Modal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** One reusable modal that lists a category's transactions for a month and lets you recategorize any of them, used by both the Budget panel and the Personal Expenses panel.

**Architecture:** `Budget.tsx` already contains a working `ActivityModal` (`:915–1020`) that does exactly this. Extract it verbatim into `src/components/CategoryActivityModal.tsx`, widen its query-invalidation set to cover both panels' cache keys, add a memo line to the transaction row, then point both panels at it. Budget additionally gains the category **name** as a second way to open it.

**Tech Stack:** React 18, TypeScript, TanStack Query v5, Tailwind, Vite. Repo: `C:\Users\Steven\ynabhelper-ui` (separate from the bot repo).

## Global Constraints

- **No test harness exists in this repo.** `package.json` has no vitest/jest and there are no `*.test.*` files. The TDD cycle in this plan is therefore **typecheck-first**: write the call site that must compile, confirm `npm run build` fails, then make it pass. Behavioural verification is a scripted manual pass in Task 4. Do not add a test framework as part of this work.
- **Verification command:** `npm run build` (runs `tsc -b && vite build`). A clean exit is the gate for every task.
- **Every mutation must invalidate `month_categories` and `ready_to_assign`** — standing rule for this codebase, because the header cards read them on every panel.
- **The panel shows outcomes, not internals.** No scores, weights, or algorithm output in user-facing UI.
- **Do not touch the six budget sub-panels** (Day-to-Day, Monthly Bills, Savings, Seasonal Funds, Credit Cards, Reimbursables). They have no drill-down today and adding one is out of scope.
- **Bot repo is not modified.** No schema change, no bot restart, no Python.
- Spec: `docs/superpowers/specs/2026-08-01-shared-category-activity-modal-design.md` (in the **bot** repo, `C:\Users\Steven\ynabhelper`).

---

## File Structure

| File | Responsibility |
|---|---|
| `src/components/CategoryActivityModal.tsx` | **Create.** Owns the whole drill-down interaction: fetch a category-month's transactions, list them, swap to a picker on click, mutate, invalidate. |
| `src/pages/Budget.tsx` | **Modify.** Delete the local `ActivityModal`; import the shared one; make the category name a second trigger. |
| `src/pages/PersonalExpenses.tsx` | **Modify.** Delete the read-only drill `<Modal>` and its `drillTxns` query; render the shared one. |

---

### Task 1: Create the shared component

**Files:**
- Create: `src/components/CategoryActivityModal.tsx`

**Interfaces:**
- Consumes: `Modal` (`@/components/Modal`), `CategoryPicker` (`@/components/CategoryPicker`), `getTransactions` (`@/lib/db`), `categorizeTransaction` + `apiErrorText` (`@/lib/api`), `fmtMoney`/`fmtDate`/`moneyClass` (`@/lib/format`), `cn` (`@/lib/cn`), `Transaction` (`@/lib/types`).
- Produces: default export `CategoryActivityModal` with props
  `{ month: string; categoryId: string; categoryName: string; activityCents: number; onClose: () => void }`.
  It renders its own `<Modal open>` — callers conditionally render the component itself, they do not pass `open`.

- [ ] **Step 1: Write the component file**

This is Budget's `ActivityModal` with four changes, all called out in comments below: prop shape, month-bounds helper, the memo line, and the widened invalidation set.

```tsx
import { useState } from "react";
import { useQuery, useMutation, useQueryClient } from "@tanstack/react-query";
import Modal from "@/components/Modal";
import CategoryPicker from "@/components/CategoryPicker";
import { getTransactions } from "@/lib/db";
import { categorizeTransaction, apiErrorText } from "@/lib/api";
import { fmtMoney, fmtDate, moneyClass } from "@/lib/format";
import { cn } from "@/lib/cn";
import type { Transaction } from "@/lib/types";

interface Props {
  /** "YYYY-MM" */
  month: string;
  categoryId: string;
  categoryName: string;
  /** Signed activity for the subtitle; negative for outflows. */
  activityCents: number;
  onClose: () => void;
}

// Inclusive YYYY-MM-DD bounds for a "YYYY-MM" month. `-31` is a safe string
// upper bound — it sorts above any real day in the month and below the 1st
// of the next month, so q_transactions (posted_date <= until) is exact.
// Copied from PersonalExpenses rather than Budget's Date-based lastDay
// arithmetic: same result, no timezone surface.
function monthBounds(month: string): { since: string; until: string } {
  return { since: `${month}-01`, until: `${month}-31` };
}

function fmtMonth(month: string): string {
  const [y, m] = month.split("-").map(Number);
  return new Date(y, m - 1, 1).toLocaleDateString(undefined, {
    month: "long",
    year: "numeric",
  });
}

/**
 * A category's transactions for one month, with click-to-recategorize.
 *
 * Shared by Budget and Personal Expenses. Both panels cache drill lists
 * under different keys, so a move made here invalidates BOTH plus the
 * month rollups and Ready to Assign — otherwise a move made in one panel
 * leaves the other showing stale numbers.
 */
export default function CategoryActivityModal({
  month,
  categoryId,
  categoryName,
  activityCents,
  onClose,
}: Props) {
  const qc = useQueryClient();
  const [pickingTxn, setPickingTxn] = useState<Transaction | null>(null);

  const bounds = monthBounds(month);
  const { data: txns } = useQuery({
    queryKey: ["activity_txns", categoryId, month],
    queryFn: () =>
      getTransactions({
        category_id: categoryId,
        since: bounds.since,
        until: bounds.until,
        limit: 300,
      }),
  });

  const recat = useMutation({
    mutationFn: categorizeTransaction,
    onSuccess: () => {
      setPickingTxn(null);
      // Union of both panels' keys — see the class comment above.
      qc.invalidateQueries({ queryKey: ["activity_txns"] });
      qc.invalidateQueries({ queryKey: ["personal_txns"] });
      qc.invalidateQueries({ queryKey: ["personal"] });
      qc.invalidateQueries({ queryKey: ["month_categories"] });
      qc.invalidateQueries({ queryKey: ["transactions"] });
      qc.invalidateQueries({ queryKey: ["ready_to_assign"] });
    },
  });

  return (
    <Modal
      open
      onClose={onClose}
      title={
        <div className="space-y-0.5">
          <div>
            {categoryName} — {fmtMonth(month)} activity
          </div>
          <div className="text-xs font-normal text-ink-2 num">
            {fmtMoney(activityCents)} across {txns?.length ?? "…"} transaction
            {txns?.length === 1 ? "" : "s"} · click one to recategorize
          </div>
        </div>
      }
      widthClass="max-w-lg"
    >
      {pickingTxn ? (
        <div className="space-y-3">
          <div className="text-sm">
            <span className="font-medium">
              {pickingTxn.payee || "Transaction"}
            </span>
            <span className="text-ink-2 num">
              {"  ·  "}
              {fmtDate(pickingTxn.posted_date)}
              {"  ·  "}
              {fmtMoney(pickingTxn.amount_cents)}
            </span>
          </div>
          <CategoryPicker
            excludeId={categoryId}
            onPick={(category_id) =>
              recat.mutate({ ledger_txn_id: pickingTxn.id, category_id })
            }
          />
          <button
            onClick={() => setPickingTxn(null)}
            className="text-xs text-ink-2 hover:text-ink"
          >
            ← Back to the list
          </button>
          {recat.isError && (
            <div className="text-xs text-negative">
              {apiErrorText(recat.error)}
            </div>
          )}
        </div>
      ) : !txns ? (
        <div className="text-sm text-ink-2 py-3">Loading transactions…</div>
      ) : txns.length === 0 ? (
        <div className="text-sm text-ink-2 py-3">
          No transactions found for this month.
        </div>
      ) : (
        <ul className="max-h-80 overflow-y-auto -mx-1">
          {txns.map((t) => (
            <li key={t.id}>
              <button
                onClick={() => setPickingTxn(t)}
                className="w-full text-left px-3 py-2 rounded-lg hover:bg-surface-2 transition-colors flex items-center justify-between gap-3"
              >
                <div className="min-w-0">
                  <div className="text-sm font-medium truncate">
                    {t.payee || "—"}
                  </div>
                  <div className="text-xs text-ink-3 num">
                    {fmtDate(t.posted_date)} · {t.account_name}
                  </div>
                  {/* Personal Expenses' table showed memo; keep it rather
                      than regress that panel. Omitted entirely when empty
                      so Budget's list looks unchanged. */}
                  {t.memo && (
                    <div
                      className="text-xs text-ink-3 truncate"
                      title={t.memo}
                    >
                      {t.memo}
                    </div>
                  )}
                </div>
                <div
                  className={cn(
                    "num text-sm shrink-0",
                    moneyClass(t.amount_cents),
                  )}
                >
                  {fmtMoney(t.amount_cents)}
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}
```

- [ ] **Step 2: Verify it typechecks**

Run: `npm run build`
Expected: PASS. The component is not imported anywhere yet, so this only proves the file itself is sound.

If `getTransactions` rejects `limit`, or `Transaction` has no `memo`/`account_name`, stop and read `src/lib/db.ts` and `src/lib/types.ts` — do not cast to `any` to force it through.

- [ ] **Step 3: Commit**

```bash
git add src/components/CategoryActivityModal.tsx
git commit -m "feat(ui): extract a shared category-activity modal"
```

---

### Task 2: Point Budget at the shared component

**Files:**
- Modify: `src/pages/Budget.tsx` (delete `:915–1020`, edit the name cell at `:521–528`, edit the render site near `:682`)

**Interfaces:**
- Consumes: `CategoryActivityModal` from Task 1.
- Produces: nothing new. `activityFor` state keeps its existing `MonthCategoryRow | null` type.

- [ ] **Step 1: Delete the local ActivityModal and import the shared one**

Delete the entire `function ActivityModal({ month, row, onClose })` block — `Budget.tsx:915` through `:1020` inclusive.

Add to the import block at the top of the file:

```tsx
import CategoryActivityModal from "@/components/CategoryActivityModal";
```

- [ ] **Step 2: Run the build to confirm it now fails**

Run: `npm run build`
Expected: FAIL — `Cannot find name 'ActivityModal'` at the render site (near `:682`).

This failing build is the point of the step: it proves the old component really was the thing rendering, and pins the exact call site to fix.

- [ ] **Step 3: Update the render site**

Replace the `<ActivityModal ... />` render (near `:682`) with:

```tsx
{activityFor && (
  <CategoryActivityModal
    month={month}
    categoryId={activityFor.category_id}
    categoryName={activityFor.category_name}
    activityCents={activityFor.activity_cents}
    onClose={() => setActivityFor(null)}
  />
)}
```

If the existing site already guards with `{activityFor && ...}`, keep that guard and only swap the element and its props.

- [ ] **Step 4: Make the category name a second trigger**

`Budget.tsx:521–528` currently renders the name as a plain `<span>`. Replace that `<td>` body with:

```tsx
<td className="px-4 py-3 font-medium">
  <span className="inline-flex items-center gap-2">
    {signal && (
      <SignalDot signal={signal.signal} title={signal.reason} />
    )}
    <button
      onClick={() => setActivityFor(r)}
      title="See this month's transactions — click any to recategorize"
      className="text-left hover:text-accent transition-colors"
    >
      {r.category_name}
    </button>
  </span>
</td>
```

The `SignalDot` stays outside the button so clicking the dot does not open the modal — it carries its own tooltip.

Note this trigger is **not** gated on `activity_cents !== 0`, unlike the existing Activity-number trigger at `:583`. A category with no activity opens the modal and shows its empty state. Leave the `:583` trigger exactly as it is.

- [ ] **Step 5: Run the build to verify it passes**

Run: `npm run build`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pages/Budget.tsx
git commit -m "refactor(budget): use the shared activity modal; open it from the category name"
```

---

### Task 3: Give Personal Expenses the recategorize capability

**Files:**
- Modify: `src/pages/PersonalExpenses.tsx` (delete `:105–115` and `:342–391`, edit imports)

**Interfaces:**
- Consumes: `CategoryActivityModal` from Task 1.
- Produces: nothing new. `drill` state keeps its existing `PersonalRow | null` type and the row click at `:289` is unchanged.

- [ ] **Step 1: Delete the read-only modal and its query**

Delete the `drillTxns` query — `PersonalExpenses.tsx:105–115`, the whole
`const { data: drillTxns, isLoading: drillLoading } = useQuery({...})` block. The shared component fetches its own.

Delete the read-only `<Modal open={!!drill} ...>` block — `:342–391` inclusive.

- [ ] **Step 2: Fix the imports**

`Modal` and `getTransactions` are now unused by this file, and `bounds` is only still used if something else references it.

- Remove `Modal` from the import at `:10`.
- Remove `getTransactions` from the `@/lib/db` import at `:12–14`, keeping `getPersonalExpensesMonth` and `getPersonalExpensesTrend`.
- Add: `import CategoryActivityModal from "@/components/CategoryActivityModal";`
- Leave `fmtDate`, `moneyClass`, `Transaction`, and `monthBounds` alone for now — Step 3 resolves whatever is genuinely unused.

- [ ] **Step 3: Run the build to see exactly what is now unused**

Run: `npm run build`
Expected: FAIL, with `TS6133` "declared but its value is never read" for some subset of `fmtDate`, `moneyClass`, `Transaction`, `bounds`, `monthBounds`, `drillLoading`.

Delete exactly what the compiler names — nothing more. Do not guess ahead of this step; `monthBounds` and `moneyClass` may still be used elsewhere in the file.

- [ ] **Step 4: Render the shared modal**

At the position the deleted `<Modal>` occupied — just before the closing `</PageShell>` — add:

```tsx
{drill && (
  <CategoryActivityModal
    month={month}
    categoryId={drill.category_id}
    categoryName={drill.category_name}
    activityCents={drill.activity_cents}
    onClose={() => setDrill(null)}
  />
)}
```

- [ ] **Step 5: Run the build to verify it passes**

Run: `npm run build`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/pages/PersonalExpenses.tsx
git commit -m "feat(personal-expenses): recategorize transactions from the drill-down"
```

---

### Task 4: Verify behaviour in the running app

**Files:** none — this task changes no code. It exists because the repo has no automated UI tests, so the behavioural gate has to be explicit and scripted rather than assumed.

**Interfaces:**
- Consumes: Tasks 1–3, all committed.
- Produces: a pass/fail report. If any check fails, fix it and re-run the whole script from check 1.

- [ ] **Step 1: Start the app against the live bot**

Run: `npm run tauri:dev`

The bot's localhost API must be up (`127.0.0.1:8765`) or the picker will not load categories. Do **not** start the bot yourself — it is the `YNAB-Helper-Bot` scheduled task and owns its own lifecycle. If it is down, use the Bot Control panel.

- [ ] **Step 2: Run the checks**

Budget panel:

1. Click a category **name** → modal opens, titled `<name> — <Month Year> activity`, listing that month's transactions.
2. Click a category **Activity number** → the same modal opens (the pre-existing path still works).
3. Find a category with `$0.00` activity → click its **name** → modal opens and reads "No transactions found for this month."

Personal Expenses panel:

4. Click a category → modal opens, and each transaction with a memo shows it as a third line.
5. Click a transaction → the list is replaced by the category picker.
6. Click "← Back to the list" → the list returns, nothing was changed.
7. Click a transaction again, pick a different category → the modal's list drops that row, the panel's Activity and Available for that category move, and the header's Ready to Assign updates — **all without a manual refresh**.

Cross-panel (this is what the widened invalidation set buys):

8. With both panels visited this session, make a move from **Budget**, then navigate to **Personal Expenses** → its numbers already reflect the move.
9. Make a move from **Personal Expenses**, then navigate to **Budget** → same.

- [ ] **Step 3: Report**

State which checks passed and which failed, with the actual observed behaviour for any failure. Do not report this task complete on the basis of a clean `npm run build` — a clean typecheck says nothing about check 7 or 9.

---

## Deployment

Not part of task execution — run only after Task 4 passes and Steven approves.

- Desktop: `npm run tauri build`, then hot-swap the built exe over the install dir under `AppData\Local` ("Harris Budget"). No reinstall.
- Mobile web: `deploy_webui.ps1`. Bundle-only update, **no bot restart**.
- `Set-ExecutionPolicy -Scope Process Bypass -Force` before any `.ps1` on this machine.
