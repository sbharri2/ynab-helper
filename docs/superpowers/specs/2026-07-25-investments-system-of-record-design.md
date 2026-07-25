# Investments & Insurance as System of Record

**Date:** 2026-07-25
**Status:** Design approved
**Repos:** `ynabhelper` (bot, DB, API), `ynabhelper-ui` (Tauri desktop app)

## Problem

Retirement, investment, and insurance data lives in a Google Sheet
("Harris Savings Accounts and Insurance"). The app reads it through a
manual pipeline: export the Sheet to xlsx, drop the file in
`G:\My Drive\ynabclone\investments\`, and `bot/investments.py` parses the
newest file by mtime on every request.

Three failures follow from that design.

**No per-account dates.** The sheet has one date per column, so every
balance in a column claims the same as-of date. In the July 2026 round,
Marcus was 6 weeks stale relative to its column header. There is no place
to record when a number was actually observed.

**No write path.** `http_api.py` exposes only `GET /investments/files` and
`GET /investments/snapshot`. Updating anything means editing the Sheet by
hand, re-exporting, and copying the file — outside the app entirely.

**No joins.** Policies cannot be matched against ledger payees, so premium
drift is invisible. The July 2026 update found homeowner-adjacent premiums
had risen 42% ($3,626.10 → $5,137.68/yr) since February and nothing had
surfaced it.

The sheet also cannot express things the data actually has: a property's
market value and mortgage separately (only the equity difference), Bitcoin
as units × price, or vested vs. total for a profit-sharing plan.

## Decisions

Locked with the user before design:

1. **Manual entry in-app.** No Plaid, no aggregators, no third-party
   account access. Consistent with the existing rejection of live
   brokerage data and of any auth flow needing recurring human action.
2. **Observation log + rounds.** Every value carries its own `as_of_date`;
   a round is a label that groups them, not a claim they share a date.
3. **Insurance = registry + premium reconciliation.** No renewal reminders.
4. **Desktop Tauri app only, single operator.** The mobile web UI stays
   read-only and unchanged.
5. **DB as store; xlsx demoted to a one-time importer.**
   `GET /investments/snapshot` keeps its current JSON shape so the four
   existing Investments pages need no changes.

## Non-goals

- Live brokerage or bank connections of any kind.
- Renewal / expiry reminders and notifications.
- Mobile editing.
- Multi-user access or per-user permissions.
- Replacing the budget ledger. Holdings are observed balances, not
  transactions; they never enter envelope math.

---

## 1. Schema

Seven tables in `bot/storage.py` — added to the `SCHEMA` string as
`CREATE TABLE IF NOT EXISTS`, following existing conventions: TEXT primary
keys for domain entities, INTEGER AUTOINCREMENT for logs, all money in
`*_cents`.

### `holding`

One row per tracked account or asset.

| column | type | notes |
|---|---|---|
| `id` | TEXT PK | |
| `name` | TEXT NOT NULL | |
| `owner` | TEXT | `steven` \| `allison` \| `joint` \| `luke` \| `josie` |
| `kind` | TEXT | `retirement` \| `brokerage` \| `crypto` \| `cash` \| `education` \| `property` \| `other` |
| `institution` | TEXT | "American Funds", "Marcus" |
| `account_number` | TEXT | as shown in the sheet today |
| `tax_treatment` | TEXT | `pretax` \| `roth` \| `taxable` \| `hsa` \| `529` \| `none` |
| `ledger_account_id` | TEXT NULL | → `account(id)` |
| `closed` | INTEGER DEFAULT 0 | |
| `sort_order` | INTEGER DEFAULT 0 | |
| `notes` | TEXT | |
| `created_at` | TIMESTAMP | |

`holding` deliberately does **not** reuse `account`. An `account` is a
budget ledger object with transactions, reconciliation, and envelope
consequences. A holding is a periodically-observed number. Merging them
would drag OBA Stock and the 529s into envelope math, which is exactly the
mistake that produced the Marcus phantom reconciliation.

`ledger_account_id` is the escape hatch for the two that exist in both
worlds — Marcus Investment Flex Fund and Coastal — so a holding can show
its ledger balance beside its observed one.

### `snapshot_round`

| column | type | notes |
|---|---|---|
| `id` | TEXT PK | |
| `label` | TEXT NOT NULL | "Jul 2026" |
| `as_of_date` | DATE NOT NULL | the snapshot date shown in the UI |
| `created_at` | TIMESTAMP | |

This is the fix for `as_of` currently reporting the xlsx file's mtime.

### `holding_value`

| column | type | notes |
|---|---|---|
| `id` | INTEGER PK AUTOINCREMENT | |
| `holding_id` | TEXT NOT NULL | → `holding(id)` |
| `round_id` | TEXT NOT NULL | → `snapshot_round(id)` |
| `as_of_date` | DATE NOT NULL | per-value, may differ from the round |
| `value_cents` | INTEGER NOT NULL | the number that rolls into Total |
| `market_value_cents` | INTEGER NULL | property |
| `debt_cents` | INTEGER NULL | property |
| `vested_cents` | INTEGER NULL | Principal Hanbury |
| `units` | REAL NULL | crypto |
| `unit_price_cents` | INTEGER NULL | crypto |
| `source` | TEXT | `manual` \| `xlsx_import` \| `ledger` |
| `is_seeded` | INTEGER DEFAULT 0 | carried forward, not yet confirmed |
| `note` | TEXT | |
| | | `UNIQUE (holding_id, round_id)` |

The nullable columns are the cases the sheet could not express:

- **Property** — 117 Mayfield is $482,400 market against $232,877.08 debt.
  The sheet stored only the $249,522.92 difference, so a Zestimate change
  and a principal payment were indistinguishable.
- **Vested** — Principal Hanbury is $23,150.27 total, $17,372.27 vested.
- **Units × price** — Bitcoin at 0.947 units. Storing units means the next
  round is a price update, not arithmetic redone by hand. The current row
  was back-derived from a February total precisely because the count was
  never written down.

`value_cents` stays authoritative even where components exist; the
components explain it rather than replace it.

### `property_detail`

1:1 with `kind='property'` holdings: `holding_id` PK, `address`,
`valuation_source`, `purchase_date`, `is_primary_residence`,
`listed_price_cents`, `escrow_cents`.

`is_primary_residence` is what makes **Minus Home Equity** subtract only
117 Mayfield. Today that convention is an unwritten name match; the rental
at 105 7th Ave counts as an investment asset, verified against the 2021,
Nov-2024, and Feb-2026 columns.

### `insurance_policy`

`id` PK, `insurance_type`, `provider`, `policy_number`, `covers`,
`through_employer`, `coverage`, `deductible`, `premium_cents`,
`premium_frequency` (`annual` \| `semiannual` \| `quarterly` \| `monthly`),
`paid_via` (`escrow` \| `ledger` \| `payroll`), `ledger_payee_norm` NULL,
`sales_contact`, `renewal_date`, `comments`, `active`, `sort_order`.

`ledger_payee_norm` is a plain column, not a join table. Several policies
sharing one payee — Amica carries home, auto, and umbrella — is a repeated
string, not a many-to-many.

### `insurance_premium_observed`

`id` AUTOINCREMENT, `policy_id`, `as_of_date`, `amount_cents`,
`source` (`escrow` \| `ledger` \| `manual`), `note`.

This table exists because **escrow-paid premiums never appear as ledger
payees.** Amica home, Fortegra, and Neptune are all inside the mortgage
payment. Ledger matching alone would have reported zero drift while
premiums rose 42%.

### `savings_target`

`id` PK, `effective_year`, `age`, `combined_salary_cents`, `multiplier`
REAL, `note`.

Target is computed as salary × multiplier. Turning 45 — when the Fidelity
benchmark steps from 3× to 4× and the target jumps from $894,000 to
$1,192,000 — becomes a data edit, not a formula rewrite.

### Computed, never stored

Total, Minus Home Equity, Target Savings, Delta, and Annual Change are all
derived at read time. The sheet stored them and they drifted out of sync
with their own inputs — two columns in the current file carry wrong header
dates, and the Feb 2026 column was mislabeled 2025.

### Migration mechanics

New tables go in the `SCHEMA` string. `_migrate()` (≈line 368) stays for
PRAGMA-guarded additive ALTERs on existing tables; new tables need no entry
there since `CREATE TABLE IF NOT EXISTS` is already idempotent.

---

## 2. API

New module `bot/investments_store.py` holds the queries.
`bot/investments.py` survives as the one-time xlsx importer and is removed
from the read path.

Routes follow existing `http_api.py` conventions: Pydantic bodies at
**module scope** (never inside `build_app()` — a closure-scoped body model
makes every POST return 422), `dependencies=[Depends(_require_token)]` on
every route, `storage.audit(...)` on every mutation, `{"ok": True, ...}`
responses.

### Reads

**`GET /investments/snapshot`** — same route, same JSON shape, served from
the DB. Optional `?round_id=` to view an older round; defaults to newest.
The four existing UI pages are untouched.

Two improvements fall out. `as_of` becomes the round's real date instead of
a file mtime. And the DB emits a value per holding per round, where the
xlsx parser drops empty cells and compresses arrays — so every `values`
array is full-length. That removes the `buildSeries()` seed-row hunt in
`InvestmentsOverview.tsx`, which today works only because 14 rows happen to
be complete.

Two fields need explicit mapping to preserve the contract. The TS `Holding`
interface carries `is_real_estate`, emitted as `kind='property'`. The TS
`InsurancePolicy` carries `annual_premium_cents`, so the route normalizes
`premium_cents` by `premium_frequency` — a monthly $100 premium serializes
as 120000. Storing the raw premium with its frequency is what lets the
registry show the number the bill actually says.

**`GET /investments/rounds`** — `id`, `label`, `as_of_date`, holding count.
Feeds the snapshot-date selector.

**`GET /investments/holdings`** — the registry including closed rows, each
with its latest value. The editor's data source; `/snapshot` remains the
viewer's.

**`GET /investments/insurance`** — policies with observed premiums and
computed drift.

### Writes

**`POST /investments/round`** — `{label, as_of_date, seed_from_previous}`.
Seeding copies the prior round's values forward with `is_seeded=1`, so a
new round starts as "confirm or change each number" rather than 23 blank
fields, and the UI can mark untouched rows as stale.

**`POST /investments/values`** — plural and atomic:
`{round_id, values: [{holding_id, value_cents, as_of_date, market_value_cents?,
debt_cents?, vested_cents?, units?, unit_price_cents?, note?}]}`.
The editor saves a whole round in one transaction rather than 23 requests.
Upsert on `(holding_id, round_id)`.

**`POST /investments/holding`** — create or update; presence of `id` means
update. Closing is `closed: true`.

**`POST /investments/policy`**, **`POST /investments/policy/premium`** —
same create-or-update shape. The premium route is how an escrow
disbursement is recorded.

**`POST /investments/import-xlsx`** — one-time migration, idempotent on
round `as_of_date`. See §5.

**No DELETE routes.** Soft flags only (`holding.closed`,
`insurance_policy.active`). The nine closed rows in the current sheet are
worth keeping, and deleting a holding would silently rewrite past totals.

The bot remains the single writer; Tauri calls `127.0.0.1:8765` as the
other panels do.

---

## 3. Editor screens

Two new pages in `ynabhelper-ui`, registered in `App.tsx` under the
existing Investments sidebar group, alongside Overview / Holdings /
Allocation / Insurance.

### A. Update Values — `/investments/update`

The round editor, and the screen that gets used four times a year.

Header: round selector (existing rounds, plus **New round**), its
`as_of_date`, and a running Total that updates as values are typed.

Body: one row per active holding, grouped by owner, showing

- previous round's value (read-only),
- an input for the new value,
- delta in dollars and percent,
- a per-row date defaulting to the round date, editable inline.

Rows render by `kind`. Property takes market value and debt, showing equity
as the computed result. Crypto takes units and price, showing value as the
product. A holding with a `vested_cents` history takes both numbers.
Everything else is a single amount.

Seeded rows (`is_seeded=1`) render muted with a "carried forward" chip
until touched — the state that would otherwise reproduce exactly the Marcus
problem, now visible instead of silent.

Footer: **Save round** posts once to `/investments/values`, then
invalidates `["investments_snapshot"]`, `["investments_rounds"]`, and
`["investments_holdings"]`.

Adding, renaming, or closing a holding happens in a modal from this screen.
That is rare enough not to deserve its own page, and it keeps holding
management next to the only workflow that reveals a holding is missing.

### B. Insurance — `/investments/insurance/edit`

Policy registry with the drift column that motivated the whole feature.

Each policy shows expected premium, most recent observed premium, and the
difference — with the source of the observation, since escrow-paid
premiums arrive from mortgage statements rather than the ledger. A policy
with no observation in 12 months is flagged as unverified rather than
silently assumed current.

**Record premium** opens a small form (`amount`, `as_of_date`, `source`,
`note`) posting to `/investments/policy/premium`. Editing a policy is an
inline form posting to `/investments/policy`.

### Snapshot date across existing pages

All four read-only pages gain an "as of {date}" chip in the `PageShell`
subtitle, driven by the round's `as_of_date`. Overview additionally gets
the round selector, so history is browsable rather than implied by whichever
xlsx happens to be newest.

---

## 4. Testing

`tests/test_investments_store.py`, following the existing pytest layout:

- **Round + seeding** — seeding copies forward, marks `is_seeded`, and
  clears the flag on update.
- **Value upsert** — repeated posts to the same `(holding_id, round_id)`
  update rather than duplicate.
- **Totals math** — Minus Home Equity subtracts only holdings where
  `is_primary_residence=1`; the rental stays in. This is the arithmetic
  that was misread once already and is worth pinning.
- **Computed target** — salary × multiplier, and the 3×→4× step at 45.
- **Property and crypto components** — equity equals market − debt; value
  equals units × price.
- **Insurance drift** — an escrow-sourced observation above the expected
  premium surfaces as drift; a policy with no observation reads as
  unverified, not as zero drift.
- **Snapshot JSON shape** — asserts the response still satisfies the
  `InvestmentSnapshot` interface in `src/lib/types.ts:327-333`. This is the
  contract keeping the four existing pages working.

---

## 5. Migration

**Import.** `POST /investments/import-xlsx` reads a named file (default:
newest) and creates one `snapshot_round` per date column — 8 in the current
file — one `holding` per row, and one `holding_value` per non-empty cell.
Round dates come from `_extract_date_from_label`, which regexes the date out
of headers like `"2026 Value (02-15-26)"`.

An unparseable header **fails the import loudly** rather than guessing a
date. This is a one-time operation performed under supervision; a silently
invented date would be the exact class of error the design exists to
eliminate.

Historical property rows import with equity in `value_cents` and NULL
components, since that is all the sheet ever held. Market value and debt
are hand-entered for the current round only. The same applies to Bitcoin
units.

The 28 insurance rows import to `insurance_policy`, and the sheet's annual
premium column becomes an `insurance_premium_observed` row dated the
snapshot date — establishing the baseline the 42% increase is measured
against.

**Verification.** `scripts/verify_investments_import.py` parses the xlsx
with the existing `bot.investments.parse_snapshot` and diffs it against
`GET /investments/snapshot`: every holding name, every value cell, every
totals row. Zero diffs is the cutover gate. The two paths are independent
implementations of the same numbers, which makes the diff meaningful.

**Cutover.** Flip `/investments/snapshot` to the DB. The xlsx files stay in
place untouched and `bot/investments.py` stays importable, so rollback is a
git revert with the data still sitting where it was. No feature flag — the
rollback path is simpler than the flag would be.

**Bot restart required.** Schema changes and new routes go live only when
the `YNAB-Helper-Bot` scheduled task restarts. Do not PID-kill or start the
bot directly.

## Known gaps at cutover

Carried from the July 2026 round, to be entered once available:

- **Optum HSA** — row exists, value blank. Active 2026-07-15, ~$350/wk.
- **Escrow treatment** — 117 Mayfield holds $3,971.14, currently excluded
  from equity and not a row. Amerisbank escrow unknown. Either becomes a
  `cash` holding or is documented as deliberately omitted.
- **Bitcoin exact unit count** — 0.947 implied; a round 0.95 moves the row
  $186.
- **Target base** — assumes the ~$14k December bonus belongs in the
  $298,000 combined-salary base.

None block implementation. Each is a row edit after the editor exists,
which is the point of building it.
