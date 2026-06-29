"""Find PayPal-aggregator rows that double-count YNAB-history breakouts.

The pattern:
  * One row with source_signal='paypal_payment' (the PayPal email parser)
  * One or more rows on the SAME account, within ±7 days, with
    source_signal='ynab_history', whose amount_cents sum to approximately
    the PayPal row's amount_cents
  * The merchant name (extracted from PayPal memo) likely matches.

Output: list of suspect PayPal aggregator rows and the matching YNAB
breakouts. The user reviews; we never auto-delete.
"""
from __future__ import annotations
import re
from bot import storage
from bot.config import load_settings

# Merchants pulled from PayPal memos look like:
#   "PayPal -$3496.35 payment to Southwest Airlines on 2026-03-02"
PAYPAL_MEMO_RE = re.compile(
    r"payment to ([\w &\.\'-]+?)(?:\s+on\s+\d|\s*$)", re.I
)


def main() -> int:
    s = load_settings()
    with storage.connect(s.paths.database) as con:
        paypal_rows = con.execute("""
            SELECT id, account_id, posted_date, amount_cents, payee, memo
            FROM ledger_txn
            WHERE source_signal = 'paypal_payment'
              AND amount_cents < 0
            ORDER BY posted_date DESC
        """).fetchall()
        if not paypal_rows:
            print("No paypal_payment rows in DB.")
            return 0

        suspects = []
        for pp in paypal_rows:
            # Find ynab_history rows on same account within ±7 days,
            # any amount (we'll sum and compare).
            kids = con.execute("""
                SELECT id, posted_date, amount_cents, payee, category_id
                FROM ledger_txn
                WHERE account_id = ?
                  AND source_signal = 'ynab_history'
                  AND amount_cents < 0
                  AND ABS(julianday(posted_date) - julianday(?)) <= 7
            """, (pp["account_id"], pp["posted_date"])).fetchall()
            if not kids:
                continue

            # Extract merchant from PayPal memo, filter kids to those
            # whose payee contains the merchant name token.
            m = PAYPAL_MEMO_RE.search(pp["memo"] or "")
            merchant_token = ""
            if m:
                merchant_token = m.group(1).split()[0].lower()
            matching = [
                k for k in kids
                if merchant_token
                and merchant_token in (k["payee"] or "").lower()
            ]
            if not matching:
                continue
            kid_sum = sum(-k["amount_cents"] for k in matching)
            pp_amt = -pp["amount_cents"]
            ratio = kid_sum / pp_amt if pp_amt > 0 else 0
            # Flag pairs where the kids' sum is within 10% of the parent.
            if 0.85 <= ratio <= 1.15:
                suspects.append({
                    "parent": pp,
                    "kids": matching,
                    "merchant": merchant_token or "?",
                    "parent_cents": pp_amt,
                    "kids_cents": kid_sum,
                    "ratio": ratio,
                })

    if not suspects:
        print("No suspect PayPal aggregator duplicates found.")
        return 0

    print(f"Found {len(suspects)} suspect PayPal aggregator(s):\n")
    for s in suspects:
        p = s["parent"]
        print(f"  PARENT #{p['id']}  {p['posted_date']}  ${s['parent_cents']/100:>9,.2f}  "
              f"merchant='{s['merchant']}'")
        print(f"    memo: {(p['memo'] or '')[:100]}")
        print(f"  {len(s['kids'])} matching ynab_history kids, "
              f"sum=${s['kids_cents']/100:,.2f} ({s['ratio']*100:.1f}% of parent):")
        for k in s["kids"]:
            print(f"    #{k['id']:>5}  {k['posted_date']}  "
                  f"${-k['amount_cents']/100:>9,.2f}  "
                  f"payee='{(k['payee'] or '')[:40]}'")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
