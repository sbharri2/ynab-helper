from bot.batch_processor import (
    build_awareness_body, build_batch, build_amazon_batch,
    render_batch_body, format_summary,
    count_cold_batch, count_amazon_ready, count_amazon_held,
)
from bot.reporters.daily import build_daily_summary

DB = "ynab_helper.db"

print("=" * 60)
print("AWARENESS PING")
print("=" * 60)
print(build_awareness_body(DB, user_id="steven"))

print("\n" + "=" * 60)
print("DAILY SUMMARY (first 20 lines)")
print("=" * 60)
out = build_daily_summary(DB)
for line in out.splitlines()[:20]:
    print(line)

print("\n" + "=" * 60)
print("SUBMIT REPLY (sample)")
print("=" * 60)
fake_result = {
    "confirmed": 12, "overridden": 1, "skipped": 1, "routed": 0,
    "failed": [], "no_guess": [],
}
print(format_summary(
    fake_result,
    remaining_cold=count_cold_batch(DB, user_id="steven"),
    amazon_ready=count_amazon_ready(DB, user_id="steven"),
))

print("\n" + "=" * 60)
print("/batch BODY (first 5 lines)")
print("=" * 60)
items = build_batch(DB, user_id="steven")
out = render_batch_body(items, count_cold_batch(DB, user_id="steven"),
                        [{"n": i+1, "pt_id": it["pt_id"], "checked": True}
                         for i, it in enumerate(items)])
for line in out.splitlines()[:8]:
    print(line)

print("\n" + "=" * 60)
print("/amazon BODY")
print("=" * 60)
items = build_amazon_batch(DB, user_id="steven")
if items:
    payload = [{"n": i+1, "pt_id": it["pt_id"], "checked": True}
                for i, it in enumerate(items)]
    print(render_batch_body(
        items, count_amazon_ready(DB, user_id="steven"), payload,
        verbose=True, header_emoji="📦", header_label="Amazon",
    ))
else:
    print("(no Amazon items ready)")
