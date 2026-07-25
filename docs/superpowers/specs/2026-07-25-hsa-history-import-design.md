# HSA History Import + Medical Category Split

**Date:** 2026-07-25
**Status:** Approved design, pending implementation
**Source file:** `C:\Users\Steven\Downloads\TransactionHistory (1).xls`

## Problem

Steven's HSA is closing and being replaced by a new HSA. The old account holds five
years of medical spending detail (Nov 2021 - Jul 2026) that exists nowhere in the
budget DB. That history is the only record of what the family actually spends on
medical care, so it needs to land in the ledger before the account goes away.

A second problem surfaced while scoping: the same medical providers already appear on
the credit cards under inconsistent categories. Importing the HSA rows under a new
category split without fixing the card rows would leave the same provider in two
different categories depending on which card paid.

## Source data

The file is a `.xls` by extension only; it is an HTML `<table>`. `pandas.read_excel`
cannot read it.

| Property | Value |
| --- | --- |
| Rows | 420 |
| Range | 2021-11-17 to 2026-07-22 |
| Medical spend | 219 charges, -$41,028.22, 53 raw payee strings |
| Employee contributions | 94, +$39,360.50 |
| Employer contributions | 46, +$1,812.00 |
| Interest | 57, +$10.34 |
| Fees | 4, -$42.45 |
| Ending balance | $112.17 |

Spend by year: 2022 -$8,920 | 2023 -$4,437 | 2024 -$12,910 | 2025 -$10,868 |
2026 -$3,895.

Payee strings are dirty: the same merchant appears under several spellings (CVS x4,
Kelley Counseling x2, Shine Orthodontics x3, MyEyeDr x2, MD Psychiatry x3, Tots N
Teens x3). Matching must be substring-based on a normalized string, not exact.

### Duplicate risk: none

Checked the one provider present in both sources. The ledger has Oak City Psychology
twice on Citi Double Cash (2025-12-16, 2026-02-16); the HSA file has 29 charges on
entirely different dates. These are separate payments from two different cards. No
HSA account or HSA-referencing ledger row exists in the DB today, and the
`Health Equity HSA` category has zero transactions.

## Design decisions

Five decisions, all made by Steven during brainstorming.

1. **On-budget, closed account** — not off-budget tracking. Chosen for spending-analytics
   visibility, accepting the historical-inflow trade-off.
2. **Split Pharmacy and Mental Health out of Medical** — rather than a flat dump into Medical.
3. **Guard envelopes with `closed = 0`** — rather than absorbing the drift.
4. **CVS is always medical** — Steven's explicit override of the recommendation to leave
   grocery-run CVS alone.
5. **Re-file the 11 medical rows stranded in Emergency Savings** — but only those rows.

## Architecture

### 1. Account

One new `account` row:

| Column | Value |
| --- | --- |
| `name` | `HSA - HealthEquity (closed)` |
| `type` | `checking` |
| `on_budget` | `1` |
| `closed` | `1` |
| `balance_cents` | `11217` |

`closed = 1` is load-bearing. `q_ready_to_assign` computes
`rta = cash_cents - available_cents`, and `cash_cents` (`webui_queries.py:587`) filters
`a.closed = 0`. A closed account therefore contributes nothing to Ready-to-Assign, which
is what keeps $39,360 of historical contributions from repeating the Marcus phantom-money
incident.

### 2. Envelope isolation

`_activity_for_month` (`bot/envelope.py:55`) filters `on_budget = 1` but **not** `closed`.
Add `AND a.closed = 0`.

Without this, the daily job — which refreshes only the current month
(`telegram_bot.py:1920`) — would pull July 2026's three HSA rows (-$303.95) into live
envelope activity. Lower `available_cents` raises `rta`, injecting $303.95 of phantom
money that has no matching on-budget cash.

The change is near-no-op for the other closed on-budget accounts (Allison Personal
Savings, Venmo, BASIC CHECKING, etc.) because none carry current-month transactions, and
historical months are never auto-recomputed. It prevents the whole class of drift going
forward.

### 3. Importer — `scripts/import_hsa_history.py`

Regex-parse the HTML table, then per row:

- strip ` (Card Transaction ID: xxx)` from the transaction text into `memo`
- normalize `($150.00)` / `$364.58` to signed integer cents
- `cleared = 'reconciled'` (closed account, settled history)
- `dedupe_key` derived from date + amount + normalized payee, so re-running is idempotent

**Validation gate — abort the import if it fails:**

```
39,360.50 + 1,812.00 + 10.34 - 41,028.22 - 42.45 = 112.17
```

This must equal the file's final `HSA Cash Balance` and the account `balance_cents`.

### 4. New categories

Two, both under `Day to Day Expenses` with `is_spending = 1`, alongside `Medical`:

- `Pharmacy`
- `Mental Health`

Named `Mental Health`, deliberately **not** "Therapy" — the data contains
`B YOUNG PHYSICAL THERA` (3 txns, -$195), and a "Therapy" label would wrongly absorb
physical therapy. PT stays in `Medical`.

### 5. Category mapping — HSA rows

| Target | Txns | Amount | Match rule |
| --- | ---: | ---: | --- |
| `Medical` (exists) | 92 | -$27,989.25 | everything else: dental/ortho, vision, labs, imaging, pediatrics, dermatology, cardiology |
| `Mental Health` | 73 | -$8,253.00 | Oak City Psychology; Kelley Counseling (2 spellings); MD Psychiatry (3 spellings) |
| `Pharmacy` | 46 | -$2,653.95 | CVS (4 spellings); Wm Cary Med Park Pharm; Foothills Professional Ph |
| `Weight Loss Meds` (exists) | 8 | -$2,132.02 | Mochi + OrderlyMeds — both GLP-1, so they belong in the existing category rather than Pharmacy |
| `Health Equity HSA` (exists) | 201 | +$41,140.39 | 140 contributions + 57 interest + 4 fees (account mechanics, not medical spend) |

Rules are ordered, first match wins. Two orderings are load-bearing: the GLP-1 rule must
precede the pharmacy rules (or OrderlyMeds gets demoted to Pharmacy), and the
mental-health rules must precede the generic ones (or `DUKE BEHAV HLTH` gets caught by a
broader Duke match).

Categorizing **both** sides matters: the account's total categorized activity nets to
+$112.17 rather than a lopsided -$41k.

### 6. Retroactive re-categorization — existing ledger

Same rules applied to rows already in the DB, so a provider lands in one category
regardless of which card paid.

95 transactions moved, 4 left alone:

| From | To | Txns | Amount |
| --- | --- | ---: | ---: |
| `Groceries` | `Pharmacy` | 35 | -$1,092.87 |
| `Medical` | `Weight Loss Meds` | 23 | -$2,297.00 |
| `Medical` | `Pharmacy` | 20 | -$998.92 |
| `Medical` | `Mental Health` | 14 | -$675.00 |
| `Vacation` | `Pharmacy` | 3 | -$115.88 |

Two corrections the dry run caught before anything was written:

- **OrderlyMeds is a GLP-1 supplier, not a general pharmacy.** Its 5 card rows were
  *already* correctly filed under `Weight Loss Meds` at $349-$399/mo, matching the Mochi
  cadence. The first draft of the rules demoted them to `Pharmacy`. Fixed by moving
  OrderlyMeds into the GLP-1 rule, which also corrected the HSA row (-$739.02).
- **Reimbursables are protected.** Four Mochi rows in `Steven/Allison Reimbursables` are
  paired legs (-$40 then +$40, netting zero). Re-filing one leg would break the pairing
  and leave a phantom expense. `PROTECTED_GROUPS` now excludes the whole Reimbursables
  group from re-filing.

### Rescue from Emergency Savings

`Emergency Savings` is a broad legacy catch-all — 40 outflows including Treasury Direct
transfers, IRS payments, West Elm and Apple. **16 human-medical rows (-$2,266.14)** move
to `Medical` via an explicit payee allow-list; the other 24 stay put. Nothing about that
category is mechanically safe to sweep, so the list is enumerated rather than inferred.

Scoping note: the initial pattern scan found 12 such rows. The final list is 16 — the
scan's pattern set missed `B YOUNG PHYSICAL THERA` (3 rows) and `MDLIVE MEDICAL GROUP`.

Deliberately excluded: `URGENT VET - CARY` (-$889.61) and `SWIFT CREEK ANIMAL HOS`
(-$839.45). Pet medical is not family medical. `B Young Physical Therapy` is included
but routes to `Medical`, not `Mental Health` — PT is not psychotherapy.

The CVS move is on Steven's explicit instruction. The recommendation had been to leave
the 34 Groceries rows alone (avg $31/txn, consistent with shopping runs rather than
prescriptions); Steven overrode this, stating CVS is always medical.

`Emergency Savings` is a broad legacy catch-all — 130+ rows including Treasury Direct
transfers, APA Treas payments, IRS payments, and interest. Only the 11 human-medical
rows move. Vet bills stay put.

## Explicitly not doing

- **Vet bills stay out of Medical** — `URGENT VET - CARY` (-$889.61),
  `SWIFT CREEK ANIMAL HOS` (-$839.45). Pet medical is not family medical.
- **Dental/ortho stays in Medical** — Shine Orthodontics (5, -$4,976) + Thomas C Steet
  DDS (8, -$1,846) = **-$6,822**, a larger cluster than Pharmacy. Steven asked for a
  Pharmacy/Mental Health split specifically. Worth revisiting as its own category later.
- **No historical month recompute.** 2021-2025 `month_category` rows stay frozen. The
  budget UI's historical Medical figures will not include HSA spend; medical analysis
  reads `ledger_txn`, where the categories and payee detail live. Recomputing 57 months
  would touch the rollover chain implicated in the 2026-07-25 RTA divergence incident.

## Flagged for Steven, not changed

Rows currently in `Medical` that are not medical:

- `NC Board of Architecture` -$55.00 — professional license
- `UNCG CVPA BOX OFFICE` -$19.98 — box office
- `Dana Minette Studi` -$139.10 — unclear
- `Venmo` 5 txns, -$855.39 — ambiguous, needs per-row review

Notable single charge: `Hospiten Dominic Bavar, Higuey` -$3,849.64 — a hospital in the
Dominican Republic, and the largest single medical transaction in the file. Presumably
a trip emergency.

## Verification — results

| Check | Result |
| --- | --- |
| Balance gate: rows sum == final balance | PASS, $112.17 |
| Row count on new account | PASS, 420 |
| `SUM(amount_cents)` == `balance_cents` | PASS, 11217 |
| **`q_ready_to_assign` 2026-07 unchanged** | **PASS — all 6 fields delta 0.00** |
| Idempotence (re-run `--apply`) | PASS — 0 inserts, 0 re-files, 0 rescues |
| Test suite | 97 passed, 5 pre-existing failures |
| New guard test fails without the guard | PASS (verified by reverting) |

### Pre-existing drift found, not caused by this work

Running `refresh_month('2026-07')` moves RTA by **+$122.18**. Isolated against an
untouched pre-import DB copy: the identical $122.18 appears there too. Cause is two
stale `month_category` rows — `Water and Trash (16th)` (-$118.97) and
`Allison Personal Savings` (-$3.21) — from transactions that posted since the last
refresh. The nightly job would have applied this anyway. **This import contributes
exactly $0 of RTA movement.**

### Resulting medical picture

364 transactions, **-$57,509.59**, 2021-06 to 2026-07.

| Category | Txns | Amount |
| --- | ---: | ---: |
| `Medical` | 137 | -$37,381.86 |
| `Mental Health` | 87 | -$8,928.00 |
| `Weight Loss Meds` | 36 | -$6,338.11 |
| `Pharmacy` | 104 | -$4,861.62 |

By year: 2021 -$2,533 | 2022 -$9,322 | 2023 -$4,812 | 2024 -$15,425 |
2025 -$17,842 | 2026 -$7,576 (partial).

## OPEN ISSUE: YNAB full-sync reverts 118 of the re-filed rows

Found while adding Dental/Ortho. A Mochi row re-filed at 18:18 was back in `Medical` by
18:35. Cause: `bot/ynab_full_sync.py:242` writes YNAB's category onto local rows —
**YNAB is authoritative for transaction categories** on any row carrying a
`ynab_txn_id`. (`ynab_full_sync_run` last fired 2026-07-25 18:35:51, exactly matching the
revert; it has run 241 times.)

Note this does not contradict "the bot's budget is its own truth" — that concerns budget
*assignments* in `month_category`, which the bot owns. Per-transaction categories are
still mirrored from YNAB.

**Measured exposure is far smaller than first estimated.** The initial read was that all
118 rows carrying a `ynab_txn_id` would revert. They do not: `full_sync` pulls only
`since_date = last_sync - 7 days` (`ynab_full_sync.py:443-446`), so exposure is a rolling
~7-day window, not all history.

Observed over a real sync (2026-07-25 19:08:19, the first after the bot restart):
**2 rows reverted**, both recent — a Mochi charge (-$79.00, 2026-07-19) and a dental
charge (-$118.00). Both were re-asserted by re-running the script.

| | Rows | Durable? |
| --- | ---: | --- |
| Imported HSA rows | 140 | Yes — no `ynab_txn_id` |
| Card rows outside the 7-day window | ~116 | Yes in practice — sync never pulls them |
| Card rows inside the rolling 7-day window | a few | No — revert until YNAB agrees |

So the five years of history are safe. The live exposure is only newly-posted charges,
which drift back to whatever category YNAB holds. Options, needing Steven's call:

1. **Make it durable in YNAB.** Create `Pharmacy`, `Mental Health` and `Dental/Ortho` in
   YNAB by hand (the YNAB API has no create-category endpoint), map their
   `ynab_category_id`, then push the 118 assignments via the existing
   `ynab_client.set_category`.
2. **Let local win in sync.** Guard `ynab_full_sync` so a locally-set category is not
   overwritten. Aligns with the redesign direction of the bot replacing YNAB, but it is
   a real change to sync semantics and needs its own testing.
3. **Accept the drift.** Keep the 140 durable HSA rows; the card rows revert to Medical
   on the next sync. Re-running this script re-applies the split at any time.

## Analytics coverage, and why history is NOT backfilled

Two data sources, two outcomes.

**Correct, full 5-year history (these read `ledger_txn` directly):**
`q_category_time_series`, `q_treemap_categories`, `q_daily_spend_heatmap`,
`q_payee_summary`, `q_category_avg_activity`. Trailing-12-month averages come out as
Mental Health $430.28/mo, Medical $394.54, Dental/Ortho $321.70, Weight Loss $207.50,
Pharmacy $47.60 — **$1,401.62/mo combined**.

**Stale (reads `month_category`):** `q_month_categories`, the Budget month grid. For
historical months the new categories are **absent, not zero** — the query is
`FROM month_category mc JOIN category c`, so a missing row does not render as $0, it
vanishes. And `Medical`'s historical figures stay inflated because they still contain the
spend moved out of them (2025-06 shows -$1,012.92 against a true -$134.50).

**The fix is worse than the problem — measured, not assumed.** Recomputing the 60
affected months (2021-06..2026-07) for just the 5 medical categories moves Ready-to-Assign
by **+$10,491.96**. Categories never budgeted historically accumulate years of negative
activity with no budgeted offset; their `available` collapses, total `available_cents`
drops, and `rta = cash_cents - available_cents` inflates by the whole amount. The
intuition that "moving spend between categories nets to zero" holds within a month but
NOT across a rebuilt rollover chain.

Decision: accept the stale history. Refresh the CURRENT month only — safe, and what the
nightly job already does (verified: +$121.29, entirely the stale Water and Trash bill,
nothing to do with the HSA).

## Deployment

- `bot/envelope.py` goes live only on the next restart of the `YNAB-Helper-Bot`
  scheduled task. Until then the bot runs the unguarded query.
- No Rust change and no desktop app rebuild. `month_category.activity_cents` is written
  only by the Python bot; `commands.rs` contains no `INSERT`/`UPDATE month_category`.
  The desktop app and mobile web UI read the new account, categories and rows straight
  from the shared DB.
