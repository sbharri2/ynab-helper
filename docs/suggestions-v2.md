# Suggestions v2 — the evidence engine

Status: approved 2026-07-10 (Steven). Supersedes the "LLM guesses, human
corrects" model. Sibling to `docs/redesign-v2.md` (chat/queue redesign);
this doc owns *how a category suggestion is produced*.

## Why (measured, not vibes)

Audit of all 242 human-categorized pending_txns (2026-07-10):

| system                                   | coverage | accuracy |
|------------------------------------------|----------|----------|
| current (override → prior → qwen)        | 81% had a suggestion | **64%** |
| YNAB-style "last category for payee"     | 51%      | **56%**  |
| naive "away city → Vacation" hybrid      | 75%      | 34%      |

- Only **71%** of repeat payees (≥3 txns) are ≥80% consistent to one
  category → *any* pure payee-memory system caps around 60%. Steven's
  categories are contextual; YNAB's model is structurally too weak here.
- Miss classes, ranked: **trip context** (16 — "Dining Out" vs
  "Vacation" for the same payee), **item ambiguity** (8 — Household vs
  Groceries vs Gifts at Target/Walmart/Amazon), **person attribution**
  (4+ — Allison's categories), LLM bad defaults (long tail).

Conclusion: beat YNAB by adding the context it can't see, not by
polishing the guess.

## Architecture — layers, deterministic-first, first confident layer wins

1. **Overrides** (exists) — hard payee → category map.
2. **Receipts / orders** — item-level truth. Amazon matching exists;
   EXTEND to Apple receipts and retailer_order emails already captured.
   Solves the item-ambiguity class.
3. **Trip windows** — flight/hotel/Airbnb/VRBO charge opens a *candidate
   trip*; bot asks ONCE in the group ("Looks like a trip Jul 20–26 —
   file away-from-home dining/transport as Vacation? y/n"); a "y" arms
   the window. While armed: charges whose payee city is outside the home
   metro (and in away-eligible groups: dining, transport, entertainment,
   lodging) suggest/file **Vacation**. Windows are rows in a `trip`
   table (state: candidate/confirmed/dismissed/expired; date range
   extendable by continued away activity). Solves the #1 miss class and
   is the flagship YNAB-can't-do-this feature.
   - Detection payees: airlines, hotels chains, AIRBNB, VRBO, TRIPMATE,
     cruise lines; also an explicit "we're traveling until the 26th"
     message in the group.
   - Home metro list lives in config (`suggest.home_cities`), seeded:
     Holly Springs, Apex, Cary, Raleigh, Durham, Fuquay-Varina,
     Morrisville, Garner, Wake Forest, Chapel Hill.
   - City parsing ONLY on raw CC-alert payees (they carry "MERCHANT CITY
     ST/USA"); never on cleaned YNAB-history payee names (the naive
     backtest failed exactly there).
4. **Person context** — which account/card the alert belongs to, Venmo
   counterparty from the payment email → bias toward that person's
   categories (Allison Personal Savings, Allison Reimbursables, ...).
5. **Payee memory v2** — normalized payee key (strip TST*/SQ*/SP
   prefixes, store numbers, trailing city+USA), **majority vote with
   recency weighting** (half-life ~180d) over ledger + pending history,
   minimum 2 observations, confidence = share of weighted vote. Amount
   bands for known-ambiguous payees (Venmo, ATM, checks).
6. **qwen, last** — only novel payees reach the LLM, and its prompt
   carries the evidence (person, away/home, priors, amount). Guardrails:
   never suggest Credit Card Payments / named-goal bill envelopes /
   Uncategorized (the known bad defaults).

Auto-file thresholds unchanged (redesign-v2 Phase 3): override/prior/
order commit; everything else is a suggestion for a human. Trip-window
Vacation filings auto-commit ONLY while the trip is human-confirmed —
provenance `filed_by = 'auto_trip'`.

## Honesty layer

- `scripts/eval_suggestions.py` — replayable backtest over all
  human-categorized rows; every layer change gets a before/after number
  IN THE COMMIT MESSAGE. No un-measured "improvements".
- Weekly report gains a scoreboard line: suggestion accuracy over the
  trailing 30d (suggested vs human-chosen), so drift is visible.

## Build order

1. eval harness (baseline: 64%)
2. payee memory v2 + normalization + LLM guardrails
3. person context
4. trip windows (schema + detection + group confirm + filing rule)
5. receipt expansion (Apple, retailer orders)
6. weekly scoreboard
