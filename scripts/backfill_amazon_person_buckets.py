"""Retroactively categorize Amazon charges into the per-person buckets.

For each Amazon charge (payee ~ AMAZON/AMZN on an on-budget card):
  * match it to an order receipt (same amount within tol, date gap [-2,+21]);
  * the matched receipt's RECIPIENT (To: header) says who ordered it ->
    'Amazon - Steven' / 'Amazon - Allison';
  * no match -> 'Amazon - Unassigned'.
Sets ledger_txn.category_id locally (the app is the source of truth for the
budget; YNAB history isn't touched). Idempotent.

Usage: .venv/Scripts/python.exe scripts/backfill_amazon_person_buckets.py [--since 2026-06-01] [--dry-run]
"""
from __future__ import annotations
import argparse, os, sys
from datetime import date
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def person_from_to(to_hdr: str) -> str:
    t = (to_hdr or "").lower()
    if "allison" in t:
        return "allison"
    if "sbharri2" in t or "steven" in t:
        return "steven"
    return "unknown"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="2026-06-01")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    from dotenv import load_dotenv
    repo = Path(__file__).resolve().parents[1]
    load_dotenv(repo / ".env", override=True)
    from bot import storage
    from bot.config import load_settings
    from bot.gmail_imap import GmailIMAP, extract_headers
    from bot.gmail_watcher import _account_imap_password
    settings = load_settings(repo / "config.yaml")
    db = settings.paths.database

    with storage.connect(db) as con:
        cats = {r["name"]: r["id"] for r in con.execute(
            "SELECT id,name FROM category WHERE name IN "
            "('Amazon - Steven','Amazon - Allison','Amazon - Unassigned')")}
        BUCKET = {"steven": cats["Amazon - Steven"],
                  "allison": cats["Amazon - Allison"],
                  "unknown": cats["Amazon - Unassigned"]}
        orders = [dict(r) for r in con.execute(
            "SELECT id,external_id,email_id,order_date,total_cents FROM pending_order "
            "WHERE source='amazon' AND total_cents>0 AND order_date>=?", (args.since,))]
        charges = [dict(r) for r in con.execute(
            """SELECT lt.id,lt.posted_date,lt.amount_cents FROM ledger_txn lt
               JOIN account a ON a.id=lt.account_id
               WHERE (UPPER(lt.payee) LIKE '%AMAZON%' OR UPPER(lt.payee) LIKE '%AMZN%')
                 AND lt.amount_cents<0 AND lt.parent_txn_id IS NULL
                 AND (lt.payee IS NULL OR lt.payee NOT LIKE 'Transfer :%')
                 AND a.on_budget=1 AND lt.posted_date>=?""", (args.since,))]

    # Resolve each order's recipient by re-fetching its email (cache mailbox conns).
    mailboxes = [(a.email, _account_imap_password(a)) for a in settings.gmail_accounts
                 if _account_imap_password(a)]
    recip: dict[int, str] = {}
    for o in orders:
        who = "unknown"
        for email, pw in mailboxes:
            try:
                with GmailIMAP(email, pw) as imap:
                    uids = imap.search(f"rfc822msgid:{o['email_id']}")
                    if uids:
                        msg = imap.fetch_message(uids[0])
                        h = extract_headers(msg)
                        who = person_from_to(h.get("To", "") or h.get("Delivered-To", ""))
                        break
            except Exception:
                pass
        recip[o["id"]] = who

    # Match (greedy: smallest amount diff, then closest date).
    def d(s): return date.fromisoformat(str(s)[:10])
    pairs = []
    for oi, o in enumerate(orders):
        tol = min(int(o["total_cents"] * 0.10), 1000)
        for ci, ch in enumerate(charges):
            gap = (d(ch["posted_date"]) - d(o["order_date"])).days
            if -2 <= gap <= 21 and abs(abs(ch["amount_cents"]) - o["total_cents"]) <= tol:
                pairs.append((abs(abs(ch["amount_cents"]) - o["total_cents"]), abs(gap), oi, ci))
    pairs.sort()
    used_o, used_c, charge_person = set(), {}, {}
    for diff, g, oi, ci in pairs:
        if oi in used_o or ci in used_c: continue
        used_o.add(oi); used_c[ci] = oi
        charge_person[charges[ci]["id"]] = recip[orders[oi]["id"]]

    # Assign every charge a bucket.
    from collections import Counter
    tally = Counter(); assigns = []
    for ci, ch in enumerate(charges):
        who = charge_person.get(ch["id"], "unknown") if ci in used_c else "unknown"
        assigns.append((ch["id"], BUCKET[who], who, ch["posted_date"], ch["amount_cents"]))
        tally[who] += abs(ch["amount_cents"])

    print(f"Amazon charges since {args.since}: {len(charges)}  (matched to a receipt: {len(used_c)})")
    print("Per-person Amazon spend:")
    for who in ("steven", "allison", "unknown"):
        n = sum(1 for a in assigns if a[2] == who)
        print(f"  Amazon - {who.title():9}: {n:3} charges  ${tally[who]/100:,.2f}")

    if args.dry_run:
        print("\n(dry-run: nothing written)")
        return 0
    with storage.connect(db) as con:
        for cid, catid, who, _, _ in assigns:
            con.execute("UPDATE ledger_txn SET category_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (catid, cid))
    storage.audit(db, "amazon_person_backfill", {"since": args.since, "n": len(assigns)})
    print(f"\nCategorized {len(assigns)} Amazon charges into person buckets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
