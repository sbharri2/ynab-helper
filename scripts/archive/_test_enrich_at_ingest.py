"""Verify _enrich_from_pending_order finds matching pending_orders."""
from datetime import date
from bot.ingest import _enrich_from_pending_order

DB = "ynab_helper.db"

# Simulate a Chase alert for $15 6/14 Amazon
parsed = {
    "summary": "Chase $15.00 at AMAZON MKTPLACE PMTS on 2026-06-14",
    "amount_cents": -1500,
    "posted_date": date(2026, 6, 14),
}
matched = _enrich_from_pending_order(DB, parsed, "AMAZON MKTPLACE PMTS")
print(f"Amazon $15 6/14:")
print(f"  matched_order_id = {matched.get('id') if matched else None}")
print(f"  enriched summary = {parsed.get('summary')!r}")
print()

# Simulate a Citi alert for $5.99 6/7 Apple — should match the Wordle receipt
parsed = {
    "summary": "Citi DC $5.99 at APPLE.COM/BILL CUPERTINO USA on 2026-06-07",
    "amount_cents": -599,
    "posted_date": date(2026, 6, 7),
}
matched = _enrich_from_pending_order(DB, parsed, "APPLE.COM/BILL CUPERTINO USA")
print(f"Apple $5.99 6/7:")
print(f"  matched_order_id = {matched.get('id') if matched else None}")
print(f"  enriched summary = {parsed.get('summary')!r}")
print()

# Non-generic — Harris Teeter should not match
parsed = {
    "summary": "Coastal $46.01 at HARRIS TEETER #118 HOLLY SPRINGS",
    "amount_cents": -4601,
    "posted_date": date(2026, 6, 7),
}
matched = _enrich_from_pending_order(DB, parsed, "HARRIS TEETER #118 HOLLY SPRINGS")
print(f"Harris Teeter (non-generic):")
print(f"  matched_order_id = {matched.get('id') if matched else None}")
print(f"  enriched summary = {parsed.get('summary')!r}  (should be UNCHANGED)")
