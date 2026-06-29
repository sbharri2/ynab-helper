from bot.config import load_settings
from bot.parsers.apple_receipt import parse
from bot.matcher import _PAYEE_PATTERNS
from bot.ynab_watcher import _enrichable_source

s = load_settings()
src = next((x for x in s.email_sources if x.name == "apple_receipt"), None)
print(f"config:      {src.name}  parser={src.parser}")
print(f"             query={src.query!r}")
print(f"matcher:     apple pattern = {_PAYEE_PATTERNS['apple'].pattern!r}")
for payee in [
    "APPLE.COM/BILL CUPERTINO USA",
    "Apple.com/bill Cupertino",
    "APL*ITUNES.COM/BILL",
    "AMAZON WEB SERVICES",
    "VENMO",
]:
    print(f"  enrichable({payee!r:35}) -> {_enrichable_source(payee)}")
