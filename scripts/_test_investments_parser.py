"""Smoke-test bot/investments.py against the seed xlsx."""
import json
from bot import investments

snap = investments.find_latest_snapshot()
print(f"latest snapshot: {snap}")
parsed = investments.parse_snapshot(snap)
print()
print(f"as_of:    {parsed['as_of']}")
print(f"holdings: {len(parsed['holdings'])} rows")
print(f"insurance:{len(parsed['insurance'])} rows")
print(f"totals:   {len(parsed['totals_rows'])} rows")
print()
print("first 3 holdings:")
for h in parsed["holdings"][:3]:
    last = h["values"][-1] if h["values"] else None
    print(f"  {h['name'][:35]:35s}  owner={h['owner'][:18]:18s}  last={last['cents']/100 if last else 'n/a'}")
print()
print("first 3 insurance:")
for i in parsed["insurance"][:3]:
    prem = i["annual_premium_cents"]
    print(f"  {i['insurance_type'][:30]:30s}  {i['provider'][:18]:18s}  prem=${prem/100 if prem else 'n/a'}/yr")
print()
print("totals rows:")
for t in parsed["totals_rows"]:
    print(f"  {t['label'][:50]:50s}  {len(t['cells'])} cells")
