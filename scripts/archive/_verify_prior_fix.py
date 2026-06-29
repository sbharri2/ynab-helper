from bot import storage
from bot.payee_overrides import resolve_payee_override

CASES = [
    "HOLLYSPRINGS*UTILITIES HOLLY SPRINGS USA",
    "DUKEENERGY",
    "Holly Springs Utilities",
    "Duke Energy",
    "DOMINION ENERGY",
    "AT&T MOBILITY",
    "HARRIS TEETER #0118 HOLLY SPRINGS USA",
    "SQ *ATLANTIC BEACH COFFEE",
    "AMAZON MKTPLACE PMTS",
]
for p in CASES:
    ov = resolve_payee_override("ynab_helper.db", p)
    sp = storage.get_strongest_payee_category("ynab_helper.db", p)
    ov_s = ov['category_name'] if ov else "(no override)"
    sp_s = f"{sp['category_name']} ({sp['pct']*100:.0f}% via {sp.get('prefix_used','?')!r})" if sp else "(no prior)"
    print(f"  {p[:42]:<42}  override={ov_s:<28}  prior={sp_s}")
