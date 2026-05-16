# YNAB Helper — Chrome Extension (Backfill Tool)

The original ynab-helper Chrome extension. **Not part of MVP-1**, but kept as a backfill tool for historical orders the bot wasn't around to see.

Use this when you need to sweep up Amazon order history that pre-dates running the Python bot — for example, when first onboarding.

For ongoing day-to-day categorization, use the Python bot at the repo root (see top-level `README.md`).

## Installation

See `INSTALLATION.md` in this directory.

## What it does

- Scrapes Amazon order history pages → exports CSV for manual YNAB import
- Scrapes Venmo transactions
- Scrapes Amazon payments page (charge dates / amounts)

## What it does NOT do

- Push notifications via Telegram (that's the bot)
- Auto-categorize via LLM (that's the bot)
- Auto-apply categories to YNAB (that's the bot)
- Match Amazon orders to bank charges (that's the bot's matcher)

If your goal is real-time categorization-while-fresh, use the bot. Use this only for one-off historical sweeps.
