# YNAB Helper

Personal Telegram assistant + local YNAB-replacement-in-progress. Ingests
CC alerts, bank balance emails, and merchant receipt emails from Gmail
in real time; categorizes via override map → strong-prior matcher →
local LLM (qwen3:32b on Ollama); pushes decisions to YNAB once daily.
The bot is the system of record; YNAB is a mirror being phased out.

## Architecture (Phase 7+, as of 2026-06-26)

Pipeline:
1. **gmail_watcher** (in-process, 60s cadence) — polls Gmail via IMAP,
   parses with per-sender modules in `bot/parsers/`, calls
   `bot.ingest.ingest_signal` to create ledger_txn + pending_txn rows.
2. **categorizer** — three-tier cascade inside `ingest_signal`:
   - `bot.payee_overrides.resolve_payee_override` — hard-coded
     biller→category map (AT&T, Hulu, Duke Energy, etc.)
   - `bot.storage.get_strongest_payee_category` — historical prior
     bypass for any payee with ≥3 hits at ≥70% to one category
   - `bot.categorizer.Categorizer.suggest` — Ollama LLM with priors
3. **Telegram UX** (`bot/telegram_bot.py`):
   - HOT lane: real-time DM as items arrive
   - `/batch` — checkbox-tile bulk processing of COLD-lane items
   - `/amazon` — verbose multi-line view for Amazon items only
   - AI agent (`bot/agent.py`) — free-text NL queries via qwen3:32b
4. **ynab_writer** (`bot/ynab_writer.py`) — runs once daily after the
   morning summary. Pushes every unsynced categorized pending_txn to
   YNAB. Bot wins on conflict; conflicts logged in the daily report.
   `/ynab` triggers manual run.
5. **ynab_full_sync** (in-process, 6h cadence) — pulls every YNAB
   transaction into local `ledger_txn` so the bot's mirror is current.

Reports DM'd to opted-in users:
- Daily summary at 7:30am (yesterday's activity, balances, queue counts)
- Daily YNAB writer report at 7:35am (pushed/conflicts/waiting)
- Awareness pings at 10am/2pm/7pm (/batch + /amazon counts)
- Weekly summary Sunday 6pm (top categories/payees/anomalies)
- Weekly YNAB sunset-progress report Sunday 6pm (coverage trend)

Removed in Phase 7:
- `bot/ynab_watcher.py` — used to enqueue pending_txns from
  YNAB-uncategorized list; now obsolete (writer pushes the other way)
- Synchronous `ynab.set_category` in `_apply_choice` — bot taps no
  longer talk to YNAB
- `bot/reporters/ynab_qa.py` — superseded by writer's daily report
- `/digest`, `/pending` commands — replaced by `/batch` + `/amazon`

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
