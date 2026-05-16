# YNAB Helper

Personal Telegram assistant that categorizes YNAB transactions while purchases are still fresh in memory. Pulls Amazon and Venmo receipts from Gmail in real time, asks for daily review on everything else, suggests categories using a local LLM (Ollama), and writes the chosen category back to YNAB via API.

See `docs/superpowers/specs/2026-05-16-ynab-helper-design.md` for the full design.

## Status

- MVP-1 (Steven's flow): in development per `docs/superpowers/plans/2026-05-16-ynab-helper-mvp1.md`
- MVP-2 (wife's Gmail): planned
- MVP-3 (weekly digest): planned

## Architecture

- **gmail_watcher** (scheduled, every 5 min) — polls Gmail for new Amazon/Venmo emails, parses, queues
- **ynab_watcher** (scheduled, every 30 min) — matches pending categorized orders to new YNAB charges, queues non-Amazon/Venmo for daily digest
- **telegram_bot** (long-running) — pushes items to user, parses replies, applies categories
- **categorizer** (Ollama HTTP) — local LLM suggests YNAB category from items + history
- **matcher** — pure scoring function linking emails to YNAB charges

## Requirements (deployment machine)

- Windows 10+, Python 3.12+
- Ollama installed and a model pulled (e.g. `ollama pull qwen2.5:14b`)
- Telegram bot created via BotFather (free)
- YNAB Personal Access Token
- Gmail account with OAuth approved for the bot

## Setup

```powershell
git clone <repo>
cd ynab-helper
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Auth Gmail (browser popup):
python scripts\reauth_gmail.py

# Validate parsing assumptions against your actual inbox:
python scripts\inspect_receipts.py

# Capture YNAB + Telegram tokens, pick budget, get chat_id:
python scripts\first_run_setup.py

# Install Windows scheduled tasks (admin PowerShell):
.\scripts\setup_scheduled_tasks.ps1

# Verify Ollama running:
ollama list
```

## Running

After `setup_scheduled_tasks.ps1`:
- `YNAB-Helper-GmailWatcher` runs every 5 min
- `YNAB-Helper-YnabWatcher` runs every 30 min
- `YNAB-Helper-Bot` runs at logon and restarts on failure

Check via `taskschd.msc`.

## Testing

```powershell
pytest -v
```

## Chrome extension (backfill tool)

The original Chrome scraper lives in `chrome-extension/` — useful for catching up on historical Amazon orders that the bot wasn't around to see. See `chrome-extension/README.md`.

## Files

- `bot/` — Python source
- `tests/` — unit tests + fixtures
- `scripts/` — setup, deployment, validation
- `chrome-extension/` — legacy Chrome backfill tool
- `docs/superpowers/specs/` — design + parsing-knowledge docs
- `docs/superpowers/plans/` — implementation plans

## Privacy

All processing happens on the local machine. The local Ollama instance is the only AI involved — no transaction data leaves your network. The Gmail OAuth token is stored locally. The YNAB API token is in a gitignored `.env`.
