from datetime import date, timedelta
from bot.config import load_settings
from bot.reporters.ynab_qa import build_ynab_qa_report

settings = load_settings()
# Test for the last 3 days so we see at least one with activity
for delta in (1, 2, 3):
    as_of = date.today() - timedelta(days=delta)
    print(f"\n{'='*60}")
    print(f"Testing for {as_of}")
    print("="*60)
    print(build_ynab_qa_report("ynab_helper.db", settings, as_of=as_of))
