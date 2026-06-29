# YNAB Helper — Transfer to Home Desktop

This folder contains everything needed to run the YNAB Helper bot on the home desktop (RTX 5090). Synced via Google Drive Desktop from the work laptop on 2026-05-17.

## What's here

```
ynab-helper/
├── bot/                      # Python source
├── chrome-extension/         # Original Chrome scraper (backfill tool only)
├── docs/                     # Design spec, plan, parsing-knowledge
├── scripts/                  # Setup + deployment scripts
├── tests/                    # Unit tests + fixtures (gitignored locally, included here)
├── _LOCAL_SECRETS_/          # Gmail OAuth token (not in git, also here)
├── pyproject.toml
├── .env.example              # Copy to .env; first_run_setup.py fills it in
├── config.yaml.example       # Copy to config.yaml; first_run_setup.py finishes it
├── .git/                     # Full git history — `git pull` works to get future updates
└── TRANSFER_INSTRUCTIONS.md  # This file
```

NOT included (regenerated on the home desktop):
- `.venv/` — recreated by `python -m venv .venv`
- `__pycache__/`, `*.egg-info/` — Python build artifacts
- `ynab_helper.db` — created on first run

## One-time setup on the home desktop

```powershell
# Open PowerShell. Decide where the bot lives — recommend OUTSIDE Google Drive:
$target = "C:\YnabHelper"
robocopy "G:\My Drive\AI_Projects\ynab-helper" $target /E /XD _LOCAL_SECRETS_
cd $target

# Place the gmail OAuth credentials in the expected location
$cred_src = "G:\My Drive\AI_Projects\ynab-helper\_LOCAL_SECRETS_\google_workspace_mcp\credentials\sbharri2@gmail.com.json"
$cred_dst = "$env:USERPROFILE\.google_workspace_mcp\credentials"
New-Item -ItemType Directory -Path $cred_dst -Force | Out-Null
Copy-Item $cred_src $cred_dst -Force

# Install Python 3.12+, then:
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# Verify the token still works:
python scripts\reauth_gmail.py    # Will re-auth if the token has been revoked

# Install Ollama (https://ollama.com/download/windows), then:
ollama pull qwen2.5:14b

# Capture YNAB token + Telegram chat_id, write .env + finish config.yaml:
python scripts\first_run_setup.py

# Run tests to verify everything works:
.\.venv\Scripts\python.exe -m pytest -q
# Expect: 59 passed

# Install Windows scheduled tasks (RUN AS ADMINISTRATOR):
.\scripts\setup_scheduled_tasks.ps1
```

## Why install OUTSIDE Google Drive

Google Drive Desktop syncs file changes in real time. Running a bot that writes `ynab_helper.db` and logs continuously inside the Drive folder would cause constant sync churn (and possible sync conflicts if the laptop is also running). The Drive folder is purely the transfer mechanism — copy it OUT to a non-Drive location for actual use.

## Source of truth

The repo is on GitHub: `git@github.com:sbharri2/ynab-helper.git`. After the initial transfer above, future updates flow via `git pull` rather than re-copying through Drive.

## Smoke test

Once setup is done:
1. Bot should auto-start at logon (via scheduled task `YNAB-Helper-Bot`).
2. Message the bot on Telegram: `/start` → reply with `/help`
3. Place a small Amazon order and watch the flow end-to-end:
   - Within ~10 min: bot DMs you the order with items + suggested category
   - Reply `y` to confirm
   - Days later when the YNAB charge clears: bot auto-applies the category
