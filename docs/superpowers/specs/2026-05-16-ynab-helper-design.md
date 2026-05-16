# YNAB Helper — Design Spec

**Date:** 2026-05-16
**Owner:** Steven Harris
**Status:** Approved for implementation planning

## Problem

Categorizing YNAB transactions falls behind every month. By the time review happens, context is gone — that random vendor at the music festival, the third-party Amazon order, the Venmo payment with the cryptic memo. The existing Chrome extension (Amazon order scraper + CSV export) helps with import but does nothing for the categorization bottleneck, which is the actual pain.

## Goal

A bot that pings via Telegram **while the purchase is still fresh in memory**, suggests a YNAB category using a local LLM, and applies the user's confirmation/correction to YNAB automatically. The bot should feel like a personal assistant, not a form.

## Non-goals

- Replacing YNAB's own UI
- Categorizing historical transactions in bulk (existing Chrome extension handles backfill)
- Multi-currency support
- Auto-linking refunds to original orders
- Subscribe-and-Save recurring detection (treated as normal transactions)
- Splitting an Amazon order across multiple YNAB categories — one order = one category

## Locked decisions

| Decision | Choice | Why |
|---|---|---|
| Channel | Telegram bot (long-polling) | Free, easy setup, no public URL needed, rich UX. SMS via Twilio is a future swap. |
| Hosting | Home desktop (Windows, always-on) | Reuses existing OAuth from email-triage project; 5090 enables local LLM |
| AI | Local Ollama on RTX 5090 | Zero per-transaction cost, privacy, no rate limits |
| Stack | Python 3.12+, SQLite, `google-api-python-client`, `python-telegram-bot`, Ollama HTTP API | Matches email-triage skill conventions |
| Interaction | AI-suggested with confirm-or-free-text reply | Best UX/complexity tradeoff; LLM handles fuzzy human descriptions |
| Storage | Single SQLite file (`ynab_helper.db`) | No server, single-file backup, queryable for debugging |
| Long-running process | NSSM service (preferred) or Windows Task Scheduler with restart-on-failure (fallback) | Auto-restart, runs without login |
| Existing Chrome extension | Moved to `chrome-extension/` subfolder, kept as backfill tool | Already works; useful for catching historical orders not seen by bot |

## Architecture

Four independent components + two pure helpers:

| Component | Type | When it runs | Job |
|---|---|---|---|
| `gmail_watcher` | Scheduled task | every 5–10 min | Poll Gmail for new Amazon + Venmo receipt emails, parse, store as `pending_order` |
| `ynab_watcher` | Scheduled task | every 30 min | Poll YNAB for new uncategorized transactions; non-Amazon/Venmo ones queue for daily digest; also runs the matcher |
| `telegram_bot` | Long-running service | always | Send messages from pending queue, parse replies, drive conversation |
| `weekly_digest` (MVP-3) | Scheduled task | Sundays 8am | Pull discretionary-category MTD spend, draft Telegram summary via Ollama |
| `parsers/amazon.py`, `parsers/venmo.py` | Pure functions | called by gmail_watcher | HTML email → structured dict |
| `matcher.py` | Pure function | called by ynab_watcher | `(pending_order, ynab_txn) → match_score` |

### Three data flow types

```
NON-AMAZON / NON-VENMO FLOW
  YNAB sees charge
    → ynab_watcher picks it up
    → categorizer suggests via Ollama
    → enqueued for daily digest at 9am
    → Telegram walk-through: one item at a time
    → user replies → category set directly in YNAB

AMAZON / VENMO FLOW (email-driven, real-time)
  Email arrives in Gmail (label: Receipts)
    → gmail_watcher parses items/note/total
    → categorizer suggests via Ollama
    → Telegram pushes immediately (subject to quiet hours)
    → user replies → category stored locally on pending_order
    → days later: YNAB charge appears
    → matcher links charge ↔ pending_order
    → category applied to YNAB transaction
    → pending_order marked status=matched

AMAZON FALLBACK (email missed or parse failed)
  AMAZON.COM charge appears in YNAB with no pending_order match
    → flows through normal non-Amazon path (daily digest)
    → still works, just without line-item detail
```

Email-driven is an *enhancement*. If Gmail polling breaks, Amazon charges still get categorized — they just lose item detail.

## Matching algorithm

Given the "one category per Amazon order" simplification, the matcher's job is *linking* YNAB charges to pending orders, not splitting categories across charges.

### Algorithm

1. **Exact match first.** New AMAZON charge with `amount == pending_order.total` within ±14 days → auto-match → apply order's category to YNAB.
2. **Amount drift tolerance.** No exact match? Try ±10% (capped at $10) for the same payee in same window. If a single candidate has match_score ≥ 0.85 AND no other candidate within 0.10 of it → auto-match.
3. **Ambiguous-or-no-match → ask user via Telegram:**
   > *"Saw a new $21.99 AMAZON charge. Could be part of your 5/12 $47.23 order (baby stuff) that already had a $25.24 charge clear? [Yes, same order] [No, new thing]"*
4. **Multi-shipment** is handled by mechanism 3 — the user decides. No subset-sum auto-matching.
5. **Cancelled orders** (email received, charge never appeared): after 30 days unmatched, transition `status=expired` and send a summary to the user.
6. **Refunds** are out of scope for matching — they flow through the normal daily digest as their own line item.

### Scoring function

Confidence score, range `[0.0, 1.10]` (memo_bonus is additive — exceeds 1.0 when YNAB memo contains the order_id, treated as strong corroborating evidence):

```
match_score(pending_order, ynab_txn) =
    amount_score  * 0.50   # exact=1.0, within±10%=linear falloff
  + date_score    * 0.30   # same day=1.0, 14 days out=0.0
  + payee_score   * 0.20   # AMAZON*/VENMO regex match
  + memo_bonus    * 0.10   # 0 or 1 — adds 0.10 if YNAB memo contains the order_id

# baseline range: 0.0 – 1.0 (the three weighted scores)
# with memo bonus:  0.0 – 1.10
```

Threshold: auto-match if `score ≥ 0.85` AND no other candidate within 0.10. Below that → user.

### Venmo matching

Trivial: exact amount, ±2 days, payee matches `VENMO*`. Venmo never fragments charges and amounts are always exact.

## Telegram conversation UX

### Message templates

**Amazon (real-time on email arrival):**
```
🛒 Amazon — $47.23 · 5/12
• Pampers Diapers Size 4
• Huggies Wipes 800ct
• Enfamil Formula 32oz

Best guess: Baby Supplies

[✅ Baby Supplies] [🍼 Baby Health] [🛍️ Shopping]
[📂 Other...] [⏭ Skip]
```

**Venmo (real-time on email arrival):**
```
💸 Venmo $42 → Sarah Chen · 5/14
Note: "concert tickets 🎸"

Best guess: Entertainment

[✅ Entertainment] [🍔 Dining Out] [👥 Gifts]
[📂 Other...] [⏭ Skip]
```

**Daily digest (9am, walk-through):**
```
📋 Good morning. 3 transactions to categorize from yesterday.

1/3 — $24.50 SQ*RIVERFEST FOOD V · 5/14
Best guess: Dining Out (festival vendor — Square processor)

[✅ Dining Out] [🎬 Entertainment] [🛒 Groceries]
[📂 Other...] [⏭ Skip]
```

After each reply, bot advances to 2/3, then 3/3, then *"All caught up. ✨"*.

### Reply grammar

| Input | Meaning |
|---|---|
| Tap button | Direct choice |
| `y`, `yes`, `✅`, `✓` | Confirm suggested category |
| `n`, `no` | Reject suggestion → show expanded category options |
| Exact category name | Use that category |
| Free text (`for Lily's bday`, `gas on the way home`) | LLM maps to most likely category, confirms |
| `skip` / `/skip` | Defer to tomorrow's digest |
| `undo` / `/undo` | Revert last categorization (5-min window) |

### Slash commands

| Command | Behavior |
|---|---|
| `/pending` | Show queue depth and oldest item date |
| `/skip` | Skip the item just shown |
| `/undo` | Revert last categorization (5-min window) |
| `/digest` | Force daily digest to run now |
| `/quiet on` / `/quiet off` | Manually pause/resume notifications |
| `/help` | Show command list |

### Quiet hours

Config: `quiet_hours: "22:00-07:00"`. Real-time items queue silently during quiet hours. At wake-up, bot sends:

```
☀️ Good morning. 3 items came in overnight + 5 from yesterday's digest. Starting with the overnight ones...
```

Daily digest fire-time should be set *after* quiet hours end.

### Error and degraded-mode UX

| Failure | Bot says |
|---|---|
| YNAB API down | *"YNAB's API isn't answering right now — I'll keep your reply and apply it when it's back. Should be a few minutes."* (exponential backoff) |
| Ollama down | Suggestion line omitted; category buttons still shown |
| Email parse failed | *"Got an Amazon email but couldn't read it. Subject: '...'. Categorize manually in YNAB when it lands."* |
| Ambiguous free-text reply | LLM picks best category + asks confirmation |
| Match ambiguity | Bot asks which pending order the charge belongs to |

## SQLite schema

```sql
CREATE TABLE pending_order (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  source TEXT NOT NULL CHECK (source IN ('amazon','venmo')),
  external_id TEXT,                          -- Amazon order ID, Venmo txn ID
  email_id TEXT NOT NULL,                    -- Gmail message ID (dedupe key)
  order_date DATE NOT NULL,
  total_cents INTEGER NOT NULL,
  raw_summary TEXT,                          -- "3 items: diapers, wipes, formula"
  raw_payload TEXT,                          -- JSON of full parsed data
  suggested_category TEXT,
  suggested_confidence REAL,
  chosen_category TEXT,
  chosen_at TIMESTAMP,
  status TEXT NOT NULL CHECK (status IN ('pending','categorized','matched','expired')),
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  UNIQUE (email_id)
);

CREATE TABLE pending_txn (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id TEXT NOT NULL,
  ynab_txn_id TEXT NOT NULL UNIQUE,
  ynab_account_id TEXT,
  payee TEXT,
  amount_cents INTEGER NOT NULL,
  txn_date DATE NOT NULL,
  memo TEXT,
  suggested_category TEXT,
  chosen_category TEXT,
  chosen_at TIMESTAMP,
  status TEXT NOT NULL CHECK (status IN ('pending','categorized','skipped')),
  digest_run_id INTEGER,
  created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE matched_charge (
  pending_order_id INTEGER REFERENCES pending_order(id),
  ynab_txn_id TEXT NOT NULL,
  matched_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (pending_order_id, ynab_txn_id)
);

CREATE TABLE bot_conversation (
  chat_id INTEGER PRIMARY KEY,
  user_id TEXT NOT NULL,
  last_asked_kind TEXT CHECK (last_asked_kind IN ('order','txn')),
  last_asked_id INTEGER,
  last_action_at TIMESTAMP,
  quiet_until TIMESTAMP
);

CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
  event TEXT NOT NULL,
  details TEXT
);

CREATE INDEX idx_pending_order_status ON pending_order(status, source, user_id);
CREATE INDEX idx_pending_txn_status   ON pending_txn(status, user_id);
CREATE INDEX idx_audit_ts             ON audit_log(ts);
```

`UNIQUE(email_id)` on `pending_order` provides idempotent inserts — gmail_watcher can re-process the same email window without duplicating.

## Module structure

```
ynab-helper/
├── bot/
│   ├── __init__.py
│   ├── config.py             # load config.yaml + .env
│   ├── storage.py            # SQLite schema, migrations, CRUD helpers
│   ├── gmail_watcher.py      # multi-source poller
│   ├── parsers/
│   │   ├── __init__.py
│   │   ├── amazon.py         # pure: HTML → order dict
│   │   └── venmo.py          # pure: HTML → txn dict
│   ├── categorizer.py        # Ollama prompt + call
│   ├── matcher.py            # pure scoring function
│   ├── ynab_client.py        # YNAB API wrapper
│   ├── ynab_watcher.py       # poll + enqueue + daily digest + matcher
│   ├── telegram_bot.py       # long-polling loop
│   ├── conversation.py       # walk-through state machine
│   └── reporters/
│       ├── __init__.py
│       └── weekly_digest.py  # MVP-3
├── scripts/
│   ├── install_service.ps1       # NSSM install of telegram_bot
│   ├── setup_scheduled_tasks.ps1 # provisions Task Scheduler entries
│   ├── first_run_setup.py        # YNAB token + Telegram chat_id + Gmail OAuth
│   └── add_gmail_account.py      # MVP-2: wife's OAuth flow
├── tests/
│   ├── fixtures/
│   │   ├── amazon_emails/        # committed sample HTML emails
│   │   ├── venmo_emails/
│   │   └── ynab_txns.json
│   ├── test_amazon_parser.py
│   ├── test_venmo_parser.py
│   ├── test_matcher.py
│   ├── test_categorizer.py
│   └── test_conversation.py
├── chrome-extension/             # existing extension moved here, kept as backfill
│   ├── manifest.json
│   ├── popup/
│   ├── content/
│   ├── background/
│   ├── icons/
│   └── README.md
├── docs/superpowers/specs/       # design specs (this file)
├── config.yaml.example
├── config.yaml                   # gitignored
├── .env.example
├── .env                          # gitignored
├── ynab_helper.db                # gitignored
├── pyproject.toml
├── .gitignore
└── README.md
```

## Configuration

`config.yaml` (committed) + `.env` (gitignored).

```yaml
gmail_accounts:
  - email: sbharri2@gmail.com
    user_id: steven
    token_path: ~/.google_workspace_mcp/credentials/sbharri2@gmail.com.json
    chat_id: <set during first_run_setup>
  # MVP-2:
  # - email: <wife's email>
  #   user_id: wife
  #   token_path: ~/.google_workspace_mcp/credentials/<wife>.json
  #   chat_id: <her telegram chat id>

email_sources:
  - { name: amazon, query: "from:auto-confirm@amazon.com newer_than:1d", parser: parsers.amazon }
  - { name: venmo,  query: "from:venmo@venmo.com newer_than:1d",         parser: parsers.venmo }

ynab:
  budget_id: <uuid>
  poll_interval_minutes: 30

ollama:
  endpoint: http://localhost:11434
  model: qwen2.5:14b
  temperature: 0.3

telegram:
  quiet_hours: "22:00-07:00"
  daily_digest_time: "09:00"

discretionary_categories: [Entertainment, Dining Out, Shopping]  # MVP-3
```

`.env` contains: `YNAB_TOKEN`, `TELEGRAM_BOT_TOKEN`.

## Deployment

Bot runs on the home desktop (RTX 5090). Development happens on Steven's work laptop, then code is pushed to git and pulled to the home desktop for deployment.

| Process | Runtime | Schedule |
|---|---|---|
| `telegram_bot.py` | NSSM Windows service (preferred) or Task Scheduler with restart-on-failure | Always |
| `gmail_watcher.py` | Task Scheduler | Every 5 min |
| `ynab_watcher.py` | Task Scheduler | Every 30 min (and at `daily_digest_time` internally) |
| `weekly_digest.py` | Task Scheduler | Sundays 8am (MVP-3) |
| `ollama serve` | Ollama Windows service | Already running |

`scripts/setup_scheduled_tasks.ps1` provisions all Task Scheduler entries — modeled on existing `Setup-EmailSorterSchedule.ps1` from email-triage.

### Migration from dev to home desktop

A separate milestone in build sequencing:

1. Push repo to git from work laptop
2. Pull on home desktop
3. Install Python 3.12+, dependencies via `pyproject.toml`
4. Install Ollama, `ollama pull qwen2.5:14b` (or chosen model)
5. Install NSSM (optional, falls back to Task Scheduler if skipped)
6. Run `scripts/first_run_setup.py` interactively to:
   - OAuth Gmail account (re-auth on this machine — do NOT copy tokens from laptop)
   - Enter YNAB Personal Access Token → writes to `.env`
   - Capture Telegram chat_id by prompting user to message the bot
   - Write resolved values into `config.yaml`
7. Run `scripts/setup_scheduled_tasks.ps1` (PowerShell as admin)
8. Smoke test: send yourself a test Amazon email, watch flow

## Logging & observability

- Rotating file logs in `./logs/` per component, INFO default
- `audit_log` SQLite table captures every state transition
- `/pending` Telegram command for real-time queue depth
- Debugging query example: `sqlite3 ynab_helper.db "SELECT * FROM audit_log WHERE details LIKE '%order_id_x%'"`

## Testing strategy

| Layer | Approach |
|---|---|
| Pure functions (`amazon.py`, `venmo.py`, `matcher.py`) | Unit tests with committed fixture files. 100% coverage target. |
| YNAB / Gmail clients | VCR-style recorded HTTP responses |
| Categorizer | Mock Ollama at prompt-input level; live tests against real model for sanity |
| Telegram bot | `conversation.py` state machine unit-tested in isolation; bot loop tested with fake Telegram client |
| E2E | Manual: send self a known Amazon email, watch full flow |

## Build sequencing

Full M4 scope, shipped in three sequenced phases. Each phase is independently shippable.

| Phase | Includes | Why this order |
|---|---|---|
| **MVP-1** | Steven's Gmail (Amazon + Venmo email flow) + ynab_watcher (daily digest for non-Amazon/Venmo) + telegram_bot + matcher + Ollama categorizer | Prove the core loop end-to-end with one user |
| **MVP-2** | Wife's Gmail wired in, multi-user routing tested | Needs her OAuth — coordinate separately |
| **MVP-3** | Weekly discretionary digest reporter | Needs ≥2 weeks of categorized data to be meaningful |

## Related prior art

Researched 2026-05-16. None match the email-trigger + conversational Telegram pattern, but worth referencing:

- [aelzeiny/YNAB_GPT](https://github.com/aelzeiny/YNAB_GPT) — GPT auto-categorization (cron only, no conversation)
- [AbdallahAHO/ynab-tui](https://github.com/AbdallahAHO/ynab-tui) — TUI + AI categorization with confidence threshold
- [esterhui/ynab-tui](https://github.com/esterhui/ynab-tui) — Amazon order matching (similar matcher logic — reference for edge cases)
- [sakowicz/actual-ai](https://github.com/sakowicz/actual-ai) — same idea for Actual Budget, supports Ollama (reference for local-LLM patterns)
- [cinnes/ynab-mcp](https://github.com/cinnes/ynab-mcp) — YNAB MCP server (reference for YNAB API patterns)
- [jeffsawatzky/ynab-cli](https://jeffsawatzky.github.io/ynab-cli/) — JSON rules engine (alternative to LLM, not chosen)
- [scottrobertson/awesome-ynab](https://github.com/scottrobertson/awesome-ynab) — full curated list
- [Toolkit for YNAB](https://github.com/toolkit-for-ynab/toolkit-for-ynab) — explicitly [declined to add LLM categorization](https://github.com/toolkit-for-ynab/toolkit-for-ynab/issues/3270); in maintenance mode

## Open implementation questions (resolve during planning, not now)

- Specific Ollama model choice (qwen2.5:14b is a guess — try a few during MVP-1)
- Python Telegram library: `python-telegram-bot` v21+ vs `aiogram` vs raw HTTP
- YNAB API client: official `ynab` package vs hand-roll
- HTML parsing approach for Amazon/Venmo emails: `beautifulsoup4` vs `selectolax` vs regex
- NSSM vs nssm-free Task Scheduler restart loop — pick during deployment

These are tactical choices for the writing-plans phase, not design decisions.
