# Business Fund Panel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A `/budget/business` panel showing BUSINESS CHECKING's balance, runway, monthly income-vs-expense, and register — and business money removed from the family Ready-to-Assign identity.

**Architecture:** Desktop-first, matching the existing Budget sub-panels. The query is a Rust Tauri command in `commands.rs` (like `q_savings_panel` / `q_credit_cards_panel` / `q_reimbursables_panel`, none of which have Python ports). The RTA exclusion is the one change that must land in **both** Rust and Python, because `q_ready_to_assign` exists on both surfaces.

**Tech Stack:** Rust (rusqlite, Tauri 2), TypeScript/React, Python 3.12 (RTA only), SQLite.

**Spec:** `docs/superpowers/specs/2026-07-25-business-fund-panel-design.md`

## Spec corrections (read before Task 1)

The spec was written before the UI architecture was checked. Two claims in it are wrong; **this plan governs**:

1. Spec says "one `q_business_fund` in `bot/webui_queries.py`". That is the **mobile** surface. Desktop Budget sub-panels are Rust commands in `ynabhelper-ui/src-tauri/src/commands.rs`. The panel goes in Rust. A Python port is explicitly **out of scope** — Savings, Credit Cards and Reimbursables have no Python port either, so mobile parity is a known, accepted gap across this whole panel family.
2. Spec says the RTA exclusion is "the only budget-engine change". Correct in spirit, but it must be made **twice** — `commands.rs::q_ready_to_assign` and `bot/webui_queries.py::q_ready_to_assign` — or desktop and mobile will disagree about Ready-to-Assign.

Everything else in the spec stands.

## Global Constraints

- Business account is **`BUSINESS CHECKING - **********9649`**, id `24712bfa-ebb1-4379-b281-ae32898cc71e`, last4 `9649`. Category group is **`Business Fund`**, id `99b49b63-22a3-439b-83c3-030a3a977ffa`.
- **Never hardcode those ids in a query.** Resolve by name at query time so a re-created account or group still works, exactly as `_amazon_bucket_category` resolves buckets by name.
- **Balance comes from `account_balance_observed`** (latest `as_of_date`), not `account.balance_cents` and not a ledger sum — bank is the source of truth. Ledger sum is the documented fallback, and the response must say which was used.
- **Transfers are excluded from income and expense** and reported separately. Identify them by `transfer_account_id IS NOT NULL` — never by payee string matching.
- **Runway uses the median monthly net, not the mean.** The trailing-6 mean is −$2,157/mo because one $8,201.11 one-off dominates it; the median is −$779.91. If net is ≥ 0, report "funded" rather than a month count.
- **No schema migration.**
- Rust changes require `npm run tauri build` + exe hot-swap to reach the installed app. Do not attempt the rebuild inside a task; Task 7 hands it to Steven.
- Run Python via `.venv/Scripts/python.exe`.
- Never PID-kill or start the bot.

## Reference data (verified 2026-07-26, for test expectations)

| Fact | Value |
|---|---|
| Latest observed balance | **$2,988.53** (as_of 2026-07-25) |
| Ledger sum for the account | $2,988.53 (agrees exactly) |
| Jul 2026 | inflow $7,000.00 (a transfer), outflow −$8,981.02 |
| Jun 2026 and the 8 months before it | inflow **$0.00** every month |
| Recurring monthly outflow | −$779.91 (mortgage), −$843.35 before Mar 2026 |
| July RTA (before this change) | −$2,576.77 |

---

### Task 1: RTA excludes business money (Python)

**Files:**
- Modify: `bot/webui_queries.py` (`q_ready_to_assign`, the `cash_cents` / `available_cents` / `assigned_cents` queries)
- Test: `tests/test_webui_queries.py`

**Interfaces:**
- Produces: `q_ready_to_assign` no longer counts the business account's cash, nor the `Business Fund` group's `available`/`budgeted`.

Context: `q_ready_to_assign` computes `rta = cash_cents - available_cents`. The business account's $2,988.53 is currently inside `cash_cents`, so business money silently backs family envelopes. `assigned_cents` and `available_cents` already exclude `Internal Master Category` by group name — extend that same mechanism.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_webui_queries.py`:

```python
def test_ready_to_assign_excludes_business_account_and_group(tmp_path):
    db = tmp_path / "t.db"
    storage.init_db(db)
    with storage.connect(db) as con:
        con.execute(
            "INSERT INTO account (id, name, type, on_budget, closed, balance_cents) "
            "VALUES ('a-fam', 'JOINT CHECKING', 'checking', 1, 0, 0)")
        con.execute(
            "INSERT INTO account (id, name, type, on_budget, closed, balance_cents) "
            "VALUES ('a-biz', 'BUSINESS CHECKING - 9649', 'checking', 1, 0, 0)")
        con.execute("INSERT INTO category_group (id, name) VALUES ('g-fam', 'Everyday')")
        con.execute("INSERT INTO category_group (id, name) VALUES ('g-biz', 'Business Fund')")
        con.execute("INSERT INTO category (id, group_id, name) VALUES ('c-fam', 'g-fam', 'Groceries')")
        con.execute("INSERT INTO category (id, group_id, name) VALUES ('c-biz', 'g-biz', 'Business Checking')")
        # $100 family cash, $50 business cash
        con.execute(
            "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, payee, is_split) "
            "VALUES ('a-fam', '2026-07-01', 10000, 'Paycheck', 0)")
        con.execute(
            "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, payee, is_split) "
            "VALUES ('a-biz', '2026-07-01', 5000, 'Rent received', 0)")
        # $30 assigned to a family envelope, $40 to the business one
        con.execute(
            "INSERT INTO month_category (month, category_id, budgeted_cents, "
            "activity_cents, available_cents) VALUES ('2026-07', 'c-fam', 3000, 0, 3000)")
        con.execute(
            "INSERT INTO month_category (month, category_id, budgeted_cents, "
            "activity_cents, available_cents) VALUES ('2026-07', 'c-biz', 4000, 0, 4000)")

    res = webui_queries.q_ready_to_assign(str(db), "2026-07")

    assert res["cash_cents"] == 10000, "business cash must not count as family cash"
    assert res["available_cents"] == 3000, "Business Fund group must leave the identity"
    assert res["assigned_cents"] == 3000
    assert res["ready_to_assign_cents"] == 10000 - 3000
```

- [ ] **Step 2: Run it and watch it fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_webui_queries.py::test_ready_to_assign_excludes_business_account_and_group -v`
Expected: FAIL — `cash_cents` is 15000 (business cash included) and `available_cents` is 7000.

- [ ] **Step 3: Add the exclusion constants**

Near the top of `bot/webui_queries.py`, after the imports:

```python
# The business checking account funds business expenses and is not family
# money. Its cash and its envelope group leave the Ready-to-Assign identity
# entirely, so family RTA reflects only family money (spec 2026-07-25).
# Matched by NAME, not id, so a re-created account/group still resolves.
BUSINESS_ACCOUNT_NAME_LIKE = "BUSINESS CHECKING%"
BUSINESS_GROUP_NAME = "Business Fund"
```

- [ ] **Step 4: Exclude business cash**

In `q_ready_to_assign`, change the `cash_row` query from:

```python
            "SELECT COALESCE(SUM(lt.amount_cents), 0) "
            "FROM ledger_txn lt "
            "JOIN account a ON a.id = lt.account_id "
            "WHERE a.on_budget = 1 AND a.closed = 0 AND lt.is_split = 0"
```

to:

```python
            "SELECT COALESCE(SUM(lt.amount_cents), 0) "
            "FROM ledger_txn lt "
            "JOIN account a ON a.id = lt.account_id "
            "WHERE a.on_budget = 1 AND a.closed = 0 AND lt.is_split = 0 "
            "  AND a.name NOT LIKE ?",
            (BUSINESS_ACCOUNT_NAME_LIKE,),
```

(the query currently takes no parameters — add the tuple argument to the `execute` call).

- [ ] **Step 5: Exclude the Business Fund group**

In the same function, both the `assigned_row` and `available_row` queries filter `g.name != 'Internal Master Category'`. Extend each to also exclude the business group, e.g.:

```python
            "WHERE mc.month = ? AND g.name NOT IN ('Internal Master Category', ?)",
            (month, BUSINESS_GROUP_NAME),
```

- [ ] **Step 6: Run the test**

Run: `.venv/Scripts/python.exe -m pytest tests/test_webui_queries.py -v`
Expected: the new test passes; every pre-existing test in the file still passes.

- [ ] **Step 7: Sanity-check against the live DB (read-only)**

```bash
.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'.')
from bot.webui_queries import q_ready_to_assign
r = q_ready_to_assign('ynab_helper.db','2026-07')
print('cash', r['cash_cents']/100, 'available', r['available_cents']/100, 'rta', r['ready_to_assign_cents']/100)
"
```

Expected: `cash` drops by about 2988.53 versus the pre-change value. Record both numbers in your report. Read-only — do not write to the live DB.

- [ ] **Step 8: Commit**

```bash
git add bot/webui_queries.py tests/test_webui_queries.py
git commit -m "feat(budget): business cash and Business Fund leave Ready-to-Assign"
```

---

### Task 2: RTA excludes business money (Rust)

**Files:**
- Modify: `ynabhelper-ui/src-tauri/src/commands.rs` (`q_ready_to_assign`)

**Interfaces:**
- Consumes: the same two exclusion rules as Task 1.
- Produces: desktop RTA agrees with mobile RTA.

Context: this is the parallel implementation. If only Python changes, the desktop app (which is the surface Steven actually uses) keeps counting business money as family money, and the two surfaces silently disagree.

- [ ] **Step 1: Locate the three queries**

In `commands.rs`, inside `q_ready_to_assign`, find the SQL that computes the cash total, the assigned total, and the available total. They mirror the Python ones: a `SUM(amount_cents)` over `ledger_txn JOIN account` filtered on `on_budget = 1 AND closed = 0`, and two `SUM` queries over `month_category JOIN category JOIN category_group` filtering `g.name != 'Internal Master Category'`.

Report the line numbers you found before editing.

- [ ] **Step 2: Add the constants**

Near the top of `commands.rs` (module level, beside other consts):

```rust
/// Business checking funds business expenses and is not family money —
/// its cash and its envelope group leave the Ready-to-Assign identity
/// (spec 2026-07-25). Matched by NAME so a re-created row still resolves.
const BUSINESS_ACCOUNT_NAME_LIKE: &str = "BUSINESS CHECKING%";
const BUSINESS_GROUP_NAME: &str = "Business Fund";
```

- [ ] **Step 3: Apply the same three exclusions**

Add `AND a.name NOT LIKE ?` to the cash query, and extend the group filter on the assigned and available queries to exclude `BUSINESS_GROUP_NAME` as well. Bind the parameters positionally in rusqlite's `?N` style, matching the surrounding code's convention.

- [ ] **Step 4: Type-check**

Run: `cd ../ynabhelper-ui/src-tauri && cargo check`
Expected: compiles with no new warnings. If `cargo` is unavailable in your environment, say so plainly in your report rather than claiming success — Task 7 will catch it at build time.

- [ ] **Step 5: Commit**

```bash
git -C ../ynabhelper-ui add src-tauri/src/commands.rs
git -C ../ynabhelper-ui commit -m "feat(budget): business cash and Business Fund leave Ready-to-Assign"
```

Note: the UI is a **separate git repository** (`ynabhelper-ui`). Commit there, not in the bot repo.

---

### Task 3: The `q_business_fund` Rust command

**Files:**
- Modify: `ynabhelper-ui/src-tauri/src/commands.rs` (new struct + command)
- Modify: `ynabhelper-ui/src-tauri/src/main.rs` or `lib.rs` (register in the `invoke_handler!` list — find where `q_savings_panel` is registered and add alongside)

**Interfaces:**
- Produces: `q_business_fund(state, months: i64) -> Result<BusinessFund, String>` with this exact shape (the TypeScript interface in Task 4 must match field-for-field):

```rust
#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BusinessMonthRow {
    pub month: String,          // "YYYY-MM"
    pub inflow_cents: i64,      // transfers EXCLUDED
    pub outflow_cents: i64,     // negative; transfers EXCLUDED
    pub net_cents: i64,
    pub transfer_in_cents: i64,
    pub transfer_out_cents: i64,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BusinessTxnRow {
    pub id: i64,
    pub posted_date: String,
    pub payee: String,
    pub amount_cents: i64,
    pub category_name: Option<String>,
    pub is_transfer: bool,
}

#[derive(serde::Serialize)]
#[serde(rename_all = "camelCase")]
pub struct BusinessFund {
    pub account_name: String,
    pub balance_cents: i64,
    pub balance_as_of: Option<String>,   // as_of_date of the observation used
    pub balance_source: String,          // "observed" | "ledger"
    pub median_net_cents: i64,           // transfers excluded
    pub runway_months: Option<f64>,      // None when median_net_cents >= 0
    pub months: Vec<BusinessMonthRow>,   // oldest first
    pub transactions: Vec<BusinessTxnRow>, // newest first
}
```

- [ ] **Step 1: Resolve the account by name**

```sql
SELECT id, name FROM account WHERE name LIKE 'BUSINESS CHECKING%' AND closed = 0 LIMIT 1
```

If no row, return `Err("business checking account not found".into())` — do not silently return zeros, which would render a confident but empty panel.

- [ ] **Step 2: Balance, bank first**

```sql
SELECT balance_cents, CAST(as_of_date AS TEXT) AS as_of_date
FROM account_balance_observed
WHERE account_id = ?1
ORDER BY as_of_date DESC, observed_at DESC
LIMIT 1
```

If a row exists: `balance_cents` from it, `balance_source = "observed"`, `balance_as_of = Some(as_of_date)`.
If not: fall back to `SELECT COALESCE(SUM(amount_cents),0) FROM ledger_txn WHERE account_id = ?1 AND is_split = 0`, with `balance_source = "ledger"` and `balance_as_of = None`.

- [ ] **Step 3: Monthly rows, transfers separated**

```sql
SELECT substr(CAST(posted_date AS TEXT), 1, 7) AS month,
       COALESCE(SUM(CASE WHEN transfer_account_id IS NULL AND amount_cents > 0
                         THEN amount_cents ELSE 0 END), 0) AS inflow_cents,
       COALESCE(SUM(CASE WHEN transfer_account_id IS NULL AND amount_cents < 0
                         THEN amount_cents ELSE 0 END), 0) AS outflow_cents,
       COALESCE(SUM(CASE WHEN transfer_account_id IS NOT NULL AND amount_cents > 0
                         THEN amount_cents ELSE 0 END), 0) AS transfer_in_cents,
       COALESCE(SUM(CASE WHEN transfer_account_id IS NOT NULL AND amount_cents < 0
                         THEN amount_cents ELSE 0 END), 0) AS transfer_out_cents
FROM ledger_txn
WHERE account_id = ?1 AND is_split = 0
GROUP BY month
ORDER BY month DESC
LIMIT ?2
```

Then reverse to oldest-first before returning. `net_cents = inflow_cents + outflow_cents` (outflow is already negative).

- [ ] **Step 4: Median net and runway**

Collect `net_cents` from the returned months, sort ascending, take the middle value (for an even count, the lower of the two middles — pick one and document it; do not average, which reintroduces outlier sensitivity).

```
if median_net_cents >= 0 { runway_months = None }
else { runway_months = Some(balance_cents as f64 / (-median_net_cents) as f64) }
```

- [ ] **Step 5: Transactions**

```sql
SELECT lt.id, CAST(lt.posted_date AS TEXT) AS posted_date,
       COALESCE(lt.payee, '') AS payee, lt.amount_cents,
       c.name AS category_name,
       lt.transfer_account_id IS NOT NULL AS is_transfer
FROM ledger_txn lt
LEFT JOIN category c ON c.id = lt.category_id
WHERE lt.account_id = ?1 AND lt.is_split = 0
ORDER BY lt.posted_date DESC, lt.id DESC
LIMIT 200
```

- [ ] **Step 6: Register the command**

Add `q_business_fund` to the `tauri::generate_handler![...]` list beside `q_savings_panel`.

- [ ] **Step 7: Type-check**

Run: `cd ../ynabhelper-ui/src-tauri && cargo check`
Expected: compiles clean. If `cargo` is unavailable, say so plainly rather than claiming success.

- [ ] **Step 8: Commit**

```bash
git -C ../ynabhelper-ui add src-tauri/src/
git -C ../ynabhelper-ui commit -m "feat(budget): q_business_fund command"
```

---

### Task 4: TypeScript types, dispatch, and mock data

**Files:**
- Modify: `ynabhelper-ui/src/lib/types.ts`
- Modify: `ynabhelper-ui/src/lib/db.ts`
- Modify: `ynabhelper-ui/src/lib/mockData.ts`

**Interfaces:**
- Consumes: the Rust struct from Task 3 — field names must match its camelCase serialization exactly.
- Produces: `getBusinessFund(months: number): Promise<BusinessFund>`

- [ ] **Step 1: Add the interfaces to `types.ts`**

```ts
export interface BusinessMonthRow {
  month: string;
  inflowCents: number;
  outflowCents: number;
  netCents: number;
  transferInCents: number;
  transferOutCents: number;
}

export interface BusinessTxnRow {
  id: number;
  postedDate: string;
  payee: string;
  amountCents: number;
  categoryName: string | null;
  isTransfer: boolean;
}

export interface BusinessFund {
  accountName: string;
  balanceCents: number;
  balanceAsOf: string | null;
  balanceSource: "observed" | "ledger";
  medianNetCents: number;
  runwayMonths: number | null;
  months: BusinessMonthRow[];
  transactions: BusinessTxnRow[];
}
```

- [ ] **Step 2: Add the dispatch to `db.ts`**

Import `BusinessFund` in the existing type-import block, then add beside `getSavingsPanel`:

```ts
export async function getBusinessFund(months: number): Promise<BusinessFund> {
  return query("q_business_fund", { months }, "businessFund" as never);
}
```

- [ ] **Step 3: Add mock data**

In `mockData.ts`, add a `businessFund` key matching the shape, using believable-but-obviously-fake numbers, so `npm run dev` renders the page. Follow the existing mock entries' style. Include at least one transfer row and one month with zero income, so the "no income" and "transfer excluded" states are visible in mock mode.

- [ ] **Step 4: Type-check**

Run: `cd ../ynabhelper-ui && npx tsc --noEmit`
Expected: no errors.

- [ ] **Step 5: Commit**

```bash
git -C ../ynabhelper-ui add src/lib/
git -C ../ynabhelper-ui commit -m "feat(budget): BusinessFund types, dispatch, mock"
```

---

### Task 5: The panel page

**Files:**
- Create: `ynabhelper-ui/src/pages/BusinessFund.tsx`
- Modify: `ynabhelper-ui/src/App.tsx` (route + sidebar entry)

**Interfaces:**
- Consumes: `getBusinessFund` from Task 4.

Read an existing sub-panel first — `ynabhelper-ui/src/pages/Savings.tsx` or `Reimbursables.tsx` — and follow its structure: `PageShell`, loading via `Skeleton`/`LoadingSpinner`, the shared formatting helpers in `lib/format.ts`. Do not invent new layout primitives.

- [ ] **Step 1: Build the page**

Three sections, in this order:

1. **Header** — account name, balance with its "as of {balanceAsOf}" caption, and runway. Runway reads "{n.n} months at the current burn" when `runwayMonths` is non-null, or "Funded — income covers expenses" when null. If `balanceSource === "ledger"`, show a quiet caption that the figure is a ledger sum because no bank observation was found.
2. **Income vs expense by month** — a row per month: month, inflow, outflow, net. Transfers get their own muted column or caption so it is visible that they are excluded from the first three. Zero-income months must be legible at a glance — that is the signal the panel exists to surface.
3. **Transactions** — the register, newest first, with category and a transfer marker.

Per the standing UI rule, show outcomes and transactions — no algorithm internals, no scores, no "median vs mean" explanation in the UI.

- [ ] **Step 2: Wire the route**

In `App.tsx`, add beside the other `/budget/*` routes:

```tsx
<Route path="/budget/business" element={<BusinessFundPage />} />
```

and add the sidebar entry in the Budget group alongside Savings / Reimbursables.

- [ ] **Step 3: Verify in mock mode**

Run: `cd ../ynabhelper-ui && npm run dev`, open `/budget/business`, confirm all three sections render from mock data with no console errors. Screenshot or describe what you saw in your report. Stop the dev server when done.

- [ ] **Step 4: Type-check and commit**

```bash
cd ../ynabhelper-ui && npx tsc --noEmit
git -C ../ynabhelper-ui add src/pages/BusinessFund.tsx src/App.tsx
git -C ../ynabhelper-ui commit -m "feat(budget): Business Fund panel"
```

---

### Task 6: Hide the Business Fund group from the main Budget page

**Files:**
- Modify: `ynabhelper-ui/src-tauri/src/commands.rs` (`q_month_categories`, or whichever command feeds the main Budget page's group list — find it and report which)
- Modify: `bot/webui_queries.py` (`q_month_categories`, same change)

Context: the group now has its own panel, and its envelope numbers are meaningless (it carries a stale −$1,559.82 into future months). Leaving it on the main Budget page invites someone to budget into it.

- [ ] **Step 1: Confirm which query feeds the Budget page groups**

Read `ynabhelper-ui/src/pages/Budget.tsx` to see which `db.ts` function it calls, trace that to the command name, and report it before editing.

- [ ] **Step 2: Exclude the group in both implementations**

Add `AND g.name != 'Business Fund'` (Rust: use the `BUSINESS_GROUP_NAME` const from Task 2; Python: the constant from Task 1) to the group/category listing used by the Budget page only. **Do not** exclude it from `q_categories` — the category must remain selectable in the category picker so transactions can still be filed to it.

- [ ] **Step 3: Verify the picker still offers it**

Run: `.venv/Scripts/python.exe -c "
import sys; sys.path.insert(0,'.')
from bot.webui_queries import q_categories
print([c['name'] for c in q_categories('ynab_helper.db') if 'Business' in c['name']])
"`
Expected: `['Business Checking']` still present. Read-only.

- [ ] **Step 4: Commit**

```bash
git add bot/webui_queries.py
git commit -m "feat(budget): hide Business Fund group from the main Budget page"
git -C ../ynabhelper-ui add src-tauri/src/commands.rs
git -C ../ynabhelper-ui commit -m "feat(budget): hide Business Fund group from the main Budget page"
```

---

### Task 7: Build and hand off

**Files:** none.

- [ ] **Step 1: Full checks**

```bash
.venv/Scripts/python.exe -m pytest tests/ -q
cd ../ynabhelper-ui && npx tsc --noEmit && cd src-tauri && cargo check
```

Expected: Python has only the known pre-existing failures (`test_conversation.py` x3, `test_ynab_client.py`, `test_amazon_parser.py::test_parse_handles_garbage_input`); TypeScript and Rust clean.

- [ ] **Step 2: Do NOT build or install**

`npm run tauri build` plus the exe hot-swap is Steven's step — a build swaps the installed app while he may be using it, and a parallel agent may be mid-deploy. Report that the work is committed and awaiting a desktop rebuild, and note the bot restart is separately needed for the Python RTA change to reach the mobile surface.

- [ ] **Step 3: Report what changed on each surface**

State plainly in your report: which changes are desktop-only, which are mobile-only, and which landed on both. Steven needs to know the panel itself is desktop-only (like Savings, Credit Cards and Reimbursables) and that mobile parity is a deliberate, deferred gap.

---

## Notes for the implementer

- **The UI is a separate git repository** at `../ynabhelper-ui`. Bot-repo files commit in `C:\Users\Steven\ynabhelper`; UI files commit in `C:\Users\Steven\ynabhelper-ui`. Never `git add -A` in either.
- **A concurrent session commits to the bot repo.** Stage only your own paths. Never touch `bot/parsers/amazon.py`, `bot/gmail_watcher.py`, `bot/group_chat.py`, `bot/trips.py`, or any `*.db*` file.
- **Do not port the panel query to Python.** It is deliberately Rust-only, matching Savings / Credit Cards / Reimbursables. Only the RTA and Budget-page-hiding changes are dual-surface.
- **The stale month_category rows are not yours to fix.** The Business Fund group carries −$1,559.82 into Aug 2026 → Jan 2027. The panel never reads `month_category`, so it cannot be affected. Bulk historical recompute is barred by standing rule.
