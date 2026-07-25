"""Import the closed HSA's five-year transaction history + split medical categories.

Steven's HSA is closing and being replaced. The export holds 2021-11..2026-07 of
medical spending that exists nowhere else in the DB, and it is the only record of what
the family actually spends on care.

Two jobs, deliberately in one script so both sides use ONE classifier:

  1. Import the 420 HSA rows into a new on-budget, CLOSED account.
  2. Re-file existing credit-card rows for the same providers, so a provider lands in
     the same category regardless of which card paid it.

Design doc: docs/superpowers/specs/2026-07-25-hsa-history-import-design.md

The source file is a .xls by extension only -- it is an HTML <table>. pandas cannot
read it.

Idempotent: re-running inserts 0 rows and re-files 0 transactions.

    python scripts/import_hsa_history.py --db ynab_helper.db [--apply]

Without --apply it is a dry run and writes nothing.
"""
from __future__ import annotations

import argparse
import html
import re
import sqlite3
import sys
import unicodedata
import uuid
from pathlib import Path

DEFAULT_SRC = Path.home() / "Downloads" / "TransactionHistory (1).xls"

ACCOUNT_NAME = "HSA - HealthEquity (closed)"
EXPECTED_ROWS = 420
EXPECTED_FINAL_BALANCE_CENTS = 11217

# Categories that must exist before we can file anything. Pharmacy and Mental Health
# are new; the rest are looked up by name.
#
# "Mental Health", NOT "Therapy" -- the ledger contains B YOUNG PHYSICAL THERA, and a
# "Therapy" label would wrongly swallow physical therapy. PT stays in Medical.
NEW_CATEGORIES = [
    ("Pharmacy", "Day to Day Expenses"),
    ("Mental Health", "Day to Day Expenses"),
    ("Dental/Ortho", "Day to Day Expenses"),
]

CAT_MEDICAL = "Medical"
CAT_PHARMACY = "Pharmacy"
CAT_MENTAL = "Mental Health"
CAT_DENTAL = "Dental/Ortho"
CAT_WEIGHT_LOSS = "Weight Loss Meds"
CAT_HSA = "Health Equity HSA"


def norm(s: str) -> str:
    """Fold a payee to a matchable form.

    Source payees are dirty: the same merchant appears under several spellings
    (CVS x4, Kelley Counseling x2, Shine Orthodontics x3, MD Psychiatry x3). Matching
    is substring-on-normalized, never exact.
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = s.encode("ascii", "ignore").decode("ascii").upper()
    return re.sub(r"[^A-Z0-9]+", " ", s).strip()


# Ordered rules -- FIRST match wins, so put the narrow ones first.
#
# Ordering that matters:
#   * MOCHI before any pharmacy rule. Mochi Health is GLP-1 telehealth and belongs in
#     the existing Weight Loss Meds category, not Pharmacy.
#   * The mental-health rules before the generic ones, so DUKE BEHAV HLTH does not get
#     caught by a broader Duke match.
RULES: list[tuple[str, tuple[str, ...]]] = [
    # OrderlyMeds is a GLP-1 supplier, not a general pharmacy -- its 5 card rows were
    # ALREADY filed under Weight Loss Meds at $349-$399/mo, matching the Mochi cadence.
    # It must beat the CVS/pharmacy rules below or it gets demoted to Pharmacy.
    (CAT_WEIGHT_LOSS, ("MOCHI", "ORDERLYMEDS", "ORDERLY MEDS")),
    (CAT_MENTAL, (
        "OAK CITY PSYCHOLOGY",
        "KELLEY COUNSELING",
        "KELLEY C",
        "MD PSYCHIATRY",
        "DUKE BEHAV",
    )),
    # Anchored on FULL provider names. A loose "SHINE" matches Sonshine Gymnastics,
    # Booneshine Brewing and Sunshine Beverage; a loose "STEET" matches HarrisTeeter.
    (CAT_DENTAL, (
        "SHINE ORTHODONTICS",
        "THOMAS C STEET",
        "ZIMA DENTAL",
    )),
    # Steven, 2026-07-25: "cvs is always medical... treat that way". This overrides the
    # recommendation to leave the 34 CVS rows sitting in Groceries (avg $31/txn) alone.
    # His call on his own spending.
    (CAT_PHARMACY, (
        "CVS",
        "WWW CVS COM",
        "GLENWOOD SOUTH PHARMAC",
        "MC CORMACKS PHARMACY",
        "MCCORMACKS PHARMACY",
        "WM CARY MED PARK PHARM",
        "FOOTHILLS PROFESSIONAL PH",
    )),
]

# Never re-file a row already sitting in a Reimbursables category. Those rows are
# deliberately paired (e.g. Mochi -$40 then +$40 across Steven/Allison Reimbursables);
# re-categorising one leg breaks the pairing and leaves a phantom expense.
PROTECTED_GROUPS = ("Reimbursables",)

# Human-medical providers stranded in the Emergency Savings category (a broad legacy
# catch-all also holding Treasury Direct transfers, IRS payments and interest). Only
# these move, and only out of Emergency Savings -- an explicit allow-list, because
# nothing about that category is mechanically safe to sweep.
#
# Deliberately absent: URGENT VET - CARY and SWIFT CREEK ANIMAL HOS. Pet medical is not
# family medical. B Young Physical Therapy IS here and goes to Medical, not Mental
# Health -- PT is not psychotherapy.
# One-off rows misfiled under Medical that are not medical at all. Matched on
# (payee substring, exact date) so they can never sweep up a similar payee.
#
# Venmo rows in Medical are deliberately absent -- Steven: those are reimbursing family
# members who fronted the cost of visits, so Medical is correct.
MISFILED: tuple[tuple[str, str, str], ...] = (
    # Professional licence. The other three identical $50/$55 NC Board charges and the
    # $974 AIA dues are all already in Steven Reimbursables.
    ("NC BOARD OF ARCHITECTURE", "2026-05-20", "Steven Reimbursables"),
    # Kids' arts programme. The other UNCG CVPA charge (-$635) is in Childcare.
    ("UNCG CVPA BOX OFFICE", "2025-07-09", "Childcare (YMCA or other)"),
    # A craft vendor at MerleFest, not a clinic. That whole week is the Wilkesboro
    # trip, and its neighbours (AdaArt Jewelry, Dalia Jade, Hometown Collaborative)
    # are all filed as Gifts.
    ("DANA MINETTE", "2026-04-25", "Gifts"),
)

RESCUE_FROM = "Emergency Savings"
RESCUE_PAYEES = (
    "DUKE HEALTH MYCHART",
    "BLUEWATER PEDIATRIC",
    "BLUE RIDGE DERMATOLOGY",
    "B YOUNG PHYSICAL THERA",
    "THOMAS C STEET",
    "MDLIVE MEDICAL GROUP",
    "CENTRAL DERMATOLOGY",
)


def classify(payee: str) -> str | None:
    """Return target category name for a payee, or None to leave it alone."""
    n = norm(payee)
    for cat, needles in RULES:
        for needle in needles:
            if needle in n:
                return cat
    return None


# ---------------------------------------------------------------- source parsing

CARD_ID_RE = re.compile(r"\s*\(Card Transaction ID:\s*([^)]*)\)")
TAG_RE = re.compile(r"<[^>]+>")


def cell_text(raw: str) -> str:
    return html.unescape(TAG_RE.sub("", raw)).replace("\xa0", " ").strip()


def to_cents(s: str) -> int:
    """'($150.00)' -> -15000, '$364.58' -> 36458."""
    s = s.strip()
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()").replace("$", "").replace(",", "").strip()
    val = round(float(s) * 100)
    return -val if neg else val


def parse_source(path: Path) -> list[dict]:
    raw = path.read_text(encoding="utf-8", errors="replace")
    out: list[dict] = []
    for tr in re.findall(r"<tr>(.*?)</tr>", raw, re.S):
        cells = [cell_text(c) for c in re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)]
        if len(cells) < 4:
            continue                      # title row
        if cells[0] == "Date":
            continue                      # header row
        date_s, txn, amount_s, balance_s = cells[0], cells[1], cells[2], cells[3]

        m = CARD_ID_RE.search(txn)
        card_id = m.group(1).strip() if m else None
        payee = CARD_ID_RE.sub("", txn).strip()

        mo, day, yr = date_s.split("/")
        out.append({
            "posted_date": f"{int(yr):04d}-{int(mo):02d}-{int(day):02d}",
            "payee": payee,
            "amount_cents": to_cents(amount_s),
            "balance_cents": to_cents(balance_s),
            "card_id": card_id,
        })
    return out


def is_mechanics(payee: str) -> bool:
    """Contributions, interest and fees are account mechanics, not medical spend."""
    n = norm(payee)
    return (
        "CONTRIBUTION" in n
        or n.startswith("INTEREST")
        or "FEE" in n
    )


def hsa_category(payee: str) -> str:
    if is_mechanics(payee):
        return CAT_HSA
    return classify(payee) or CAT_MEDICAL


# ---------------------------------------------------------------- db helpers

def cat_map(con: sqlite3.Connection) -> dict[str, str]:
    return {r["name"]: r["id"] for r in con.execute("SELECT id, name FROM category")}


def ensure_categories(con: sqlite3.Connection, apply: bool) -> None:
    cats = cat_map(con)
    for name, group_name in NEW_CATEGORIES:
        if name in cats:
            print(f"  category exists: {name}")
            continue
        grp = con.execute(
            "SELECT id FROM category_group WHERE name = ?", (group_name,)
        ).fetchone()
        if grp is None:
            sys.exit(f"FATAL: category group {group_name!r} not found")
        cid = f"botcat_{uuid.uuid4().hex[:12]}"
        print(f"  CREATE category: {name} (group {group_name})")
        if apply:
            con.execute(
                "INSERT INTO category (id, group_id, name, is_spending) "
                "VALUES (?, ?, ?, 1)",
                (cid, grp["id"], name),
            )


def ensure_account(con: sqlite3.Connection, apply: bool) -> str | None:
    row = con.execute(
        "SELECT id FROM account WHERE name = ?", (ACCOUNT_NAME,)
    ).fetchone()
    if row:
        print(f"  account exists: {ACCOUNT_NAME}")
        return row["id"]
    aid = str(uuid.uuid4())
    print(f"  CREATE account: {ACCOUNT_NAME} (on_budget=1, closed=1)")
    if apply:
        con.execute(
            "INSERT INTO account (id, name, type, on_budget, closed, balance_cents, "
            "cleared_balance_cents) VALUES (?, ?, 'checking', 1, 1, ?, ?)",
            (aid, ACCOUNT_NAME, EXPECTED_FINAL_BALANCE_CENTS,
             EXPECTED_FINAL_BALANCE_CENTS),
        )
        return aid
    return None


def dedupe_key_for(r: dict) -> str:
    """Stable per-row key so re-running the import is a no-op.

    Includes the card transaction id when present; several same-day same-amount
    charges to one provider are legitimately distinct visits.
    """
    base = f"hsa|{r['posted_date']}|{r['amount_cents']}|{norm(r['payee'])}"
    return f"{base}|{r['card_id']}" if r["card_id"] else base


# ---------------------------------------------------------------- main

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="ynab_helper.db")
    ap.add_argument("--src", default=str(DEFAULT_SRC))
    ap.add_argument("--apply", action="store_true", help="write (default: dry run)")
    args = ap.parse_args()

    src = Path(args.src)
    if not src.exists():
        sys.exit(f"FATAL: source not found: {src}")

    rows = parse_source(src)
    print(f"parsed {len(rows)} rows from {src.name}")

    # ---- validation gate -------------------------------------------------
    if len(rows) != EXPECTED_ROWS:
        sys.exit(f"FATAL: expected {EXPECTED_ROWS} rows, parsed {len(rows)}")

    total = sum(r["amount_cents"] for r in rows)
    if total != EXPECTED_FINAL_BALANCE_CENTS:
        sys.exit(
            f"FATAL: rows sum to {total} cents, expected "
            f"{EXPECTED_FINAL_BALANCE_CENTS}. Refusing to import."
        )
    # Rows are newest-first; the newest row's running balance is the final balance.
    if rows[0]["balance_cents"] != EXPECTED_FINAL_BALANCE_CENTS:
        sys.exit(
            f"FATAL: file's final balance {rows[0]['balance_cents']} != "
            f"{EXPECTED_FINAL_BALANCE_CENTS}"
        )
    print(f"  OK sum of rows == final balance == ${total / 100:,.2f}")

    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")

    mode = "APPLY" if args.apply else "DRY RUN"
    print(f"\n=== {mode} ===\n")

    print("[1] categories")
    ensure_categories(con, args.apply)
    cats = cat_map(con)
    for name in (CAT_MEDICAL, CAT_WEIGHT_LOSS, CAT_HSA):
        if name not in cats:
            sys.exit(f"FATAL: expected existing category {name!r} not found")

    print("\n[2] account")
    account_id = ensure_account(con, args.apply)
    if account_id is None and not args.apply:
        print("  (dry run: skipping row import, account does not exist yet)")

    print("\n[3] import HSA rows")
    existing = {
        r["dedupe_key"]
        for r in con.execute(
            "SELECT dedupe_key FROM ledger_txn WHERE dedupe_key LIKE 'hsa|%'"
        )
    }
    inserted = 0
    by_cat: dict[str, list[int]] = {}
    for r in rows:
        key = dedupe_key_for(r)
        cat_name = hsa_category(r["payee"])
        by_cat.setdefault(cat_name, []).append(r["amount_cents"])
        if key in existing:
            continue
        inserted += 1
        if args.apply and account_id:
            memo = f"HSA card txn {r['card_id']}" if r["card_id"] else "HSA"
            con.execute(
                "INSERT INTO ledger_txn (account_id, posted_date, amount_cents, "
                "payee, memo, category_id, cleared, source_signal, dedupe_key) "
                "VALUES (?, ?, ?, ?, ?, ?, 'reconciled', 'hsa_import', ?)",
                (account_id, r["posted_date"], r["amount_cents"], r["payee"],
                 memo, cats.get(cat_name), key),
            )
    print(f"  {inserted} to insert, {len(rows) - inserted} already present")
    for name, amts in sorted(by_cat.items(), key=lambda kv: sum(kv[1])):
        print(f"    {name:20} n={len(amts):>4}  ${sum(amts) / 100:>12,.2f}")

    print("\n[4] re-file transactions to match the classifier")
    # Deliberately NOT excluding the HSA account. Its rows were filed by this same
    # classifier at import, so re-running is a no-op -- but when a RULE changes (e.g.
    # adding Dental/Ortho), the HSA rows need to move too or the split applies to the
    # cards only. Keeps the script self-healing across rule edits.
    ledger = con.execute(
        """SELECT l.id, l.payee, l.amount_cents, l.category_id, l.posted_date,
                  c.name AS cat_name, g.name AS grp_name, a.name AS acct
           FROM ledger_txn l
           JOIN account a ON a.id = l.account_id
           LEFT JOIN category c ON c.id = l.category_id
           LEFT JOIN category_group g ON g.id = c.group_id
           WHERE l.is_split = 0"""
    ).fetchall()

    moves: dict[tuple[str, str], list[int]] = {}
    n_moved = 0
    n_protected = 0
    for r in ledger:
        target = classify(r["payee"] or "")
        if target is None or target == r["cat_name"]:
            continue
        if (r["grp_name"] or "") in PROTECTED_GROUPS:
            n_protected += 1
            continue
        n_moved += 1
        moves.setdefault((r["cat_name"] or "(none)", target), []).append(
            r["amount_cents"]
        )
        if args.apply:
            con.execute(
                "UPDATE ledger_txn SET category_id = ?, updated_at = CURRENT_TIMESTAMP "
                "WHERE id = ?",
                (cats[target], r["id"]),
            )
    print(f"  {n_moved} transactions to re-file "
          f"({n_protected} left alone in protected groups)")
    for (src_c, dst_c), amts in sorted(moves.items(), key=lambda kv: -len(kv[1])):
        print(f"    {src_c:22} -> {dst_c:16} n={len(amts):>3}  "
              f"${sum(amts) / 100:>10,.2f}")

    print(f"\n[5] rescue medical rows stranded in {RESCUE_FROM}")
    stranded = con.execute(
        """SELECT l.id, l.posted_date, l.payee, l.amount_cents
           FROM ledger_txn l
           JOIN category c ON c.id = l.category_id
           WHERE c.name = ? AND l.is_split = 0 AND l.amount_cents < 0
           ORDER BY l.posted_date""",
        (RESCUE_FROM,),
    ).fetchall()
    rescued, rescued_total = 0, 0
    for r in stranded:
        n = norm(r["payee"] or "")
        if not any(p in n for p in RESCUE_PAYEES):
            continue
        rescued += 1
        rescued_total += r["amount_cents"]
        print(f"    {r['posted_date']}  {r['amount_cents'] / 100:>10,.2f}  "
              f"{r['payee'][:34]}")
        if args.apply:
            con.execute(
                "UPDATE ledger_txn SET category_id = ?, "
                "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (cats[CAT_MEDICAL], r["id"]),
            )
    print(f"  {rescued} rows -> {CAT_MEDICAL}  "
          f"(${rescued_total / 100:,.2f}); "
          f"{len(stranded) - rescued} non-medical rows left in {RESCUE_FROM}")

    print("\n[6] one-off misfiled rows")
    fixed = 0
    for needle, when, target in MISFILED:
        if target not in cats:
            sys.exit(f"FATAL: category {target!r} not found")
        hits = [
            r for r in con.execute(
                """SELECT l.id, l.payee, l.amount_cents, c.name AS cat_name
                   FROM ledger_txn l
                   LEFT JOIN category c ON c.id = l.category_id
                   WHERE l.posted_date = ? AND l.is_split = 0""",
                (when,),
            ) if needle in norm(r["payee"] or "")
        ]
        for r in hits:
            if r["cat_name"] == target:
                continue
            fixed += 1
            print(f"    {when}  {r['amount_cents'] / 100:>9,.2f}  "
                  f"{r['payee'][:30]:32} {r['cat_name']} -> {target}")
            if args.apply:
                con.execute(
                    "UPDATE ledger_txn SET category_id = ?, "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (cats[target], r["id"]),
                )
    print(f"  {fixed} rows corrected")

    if args.apply:
        con.commit()
        print("\ncommitted.")
    else:
        con.rollback()
        print("\ndry run -- nothing written. Re-run with --apply.")
    con.close()


if __name__ == "__main__":
    main()
