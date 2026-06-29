"""Smoke-test bot.batch_processor.parse_reply on a fake batch."""
from bot import batch_processor

FAKE = [
    {"n": 1, "pt_id": 101},
    {"n": 2, "pt_id": 102},
    {"n": 3, "pt_id": 103},
    {"n": 4, "pt_id": 104},
    {"n": 5, "pt_id": 105},
    {"n": 6, "pt_id": 106},
    {"n": 7, "pt_id": 107},
]

CASES = [
    "all",
    "all except 3=groceries 6 skip",
    "1 2 4",
    "3=dining out",
    "5 skip",
    "7 back",
    "all except 3=dining out 6 skip 9=gifts",
    "",
    "garbage text",
    "1, 2, 4",
    "3 = groceries, 5 skip",
]

for text in CASES:
    r = batch_processor.parse_reply(text, FAKE)
    print(f"\nINPUT: {text!r}")
    print(f"  applies_to_all: {r['applies_to_all']}")
    print(f"  decisions: {r['decisions']}")
    if r["unparsed"]:
        print(f"  unparsed: {r['unparsed']}")
    if r["warnings"]:
        print(f"  warnings: {r['warnings']}")

print("\n--- looks_like_batch_reply ---")
from bot.telegram_bot import _looks_like_batch_reply
for s in ["all", "3=groceries", "1 2 4", "1Password", "groceries",
          "skip", "all except 3", "hello bot"]:
    print(f"  {s!r:<30} -> {_looks_like_batch_reply(s)}")
