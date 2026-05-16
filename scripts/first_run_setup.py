"""One-time setup on the deployment machine.

Captures:
  - YNAB Personal Access Token (write to .env)
  - YNAB budget_id (pick from list)
  - Telegram bot token + chat_id (start bot, ask user to message it, capture incoming chat_id)
  - Updates config.yaml with the captured values

Run once after `pip install -e ".[dev]"`:
    python scripts/first_run_setup.py
"""
from __future__ import annotations

import asyncio
import shutil
import sys
from pathlib import Path

import yaml
import ynab
from telegram import Update
from telegram.ext import Application, MessageHandler, filters

REPO = Path(__file__).resolve().parent.parent
CONFIG_EXAMPLE = REPO / "config.yaml.example"
CONFIG = REPO / "config.yaml"
ENV = REPO / ".env"


def ensure_config() -> None:
    if not CONFIG.exists():
        shutil.copy(CONFIG_EXAMPLE, CONFIG)
        print(f"Created {CONFIG} from example.")


def capture_ynab() -> tuple[str, str]:
    token = input("YNAB Personal Access Token (https://app.ynab.com/settings): ").strip()
    if not token:
        sys.exit("Token required.")

    config = ynab.Configuration(access_token=token)
    # NOTE: this `ynab` SDK exposes budgets via PlansApi.get_plans() — the API
    # internally renames "budget" to "plan". The user-facing concept is still
    # "budget" (matches YNAB UI + config.yaml field name).
    with ynab.ApiClient(config) as api_client:
        api = ynab.PlansApi(api_client)
        budgets = api.get_plans().data.plans

    if not budgets:
        sys.exit("No budgets found on this account.")

    print("\nYour budgets:")
    for i, b in enumerate(budgets):
        print(f"  [{i}] {b.name}  ({b.id})")

    while True:
        raw = input(f"Pick budget number [0-{len(budgets) - 1}]: ").strip()
        try:
            idx = int(raw)
            if 0 <= idx < len(budgets):
                break
        except ValueError:
            pass
        print("Invalid choice — try again.")

    return token, budgets[idx].id


async def capture_telegram_chat_id() -> tuple[str, int | None]:
    token = input("Telegram bot token (from @BotFather): ").strip()
    if not token:
        sys.exit("Token required.")

    print("Now message your bot from your phone (any text). Waiting...")

    chat_id_holder: dict[str, int] = {}
    stop_event = asyncio.Event()

    async def on_msg(update: Update, _ctx) -> None:
        chat_id = update.effective_chat.id
        chat_id_holder["id"] = chat_id
        await update.message.reply_text(f"Got it. Your chat id is {chat_id}.")
        stop_event.set()

    app = Application.builder().token(token).build()
    app.add_handler(MessageHandler(filters.ALL, on_msg))

    # PTB v22 manual lifecycle — Application.run_polling() is sync and manages
    # its own event loop, so we drive initialize/start/poll/stop ourselves
    # to remain inside our existing asyncio.run() context.
    await app.initialize()
    await app.start()
    await app.updater.start_polling(drop_pending_updates=True)
    try:
        await stop_event.wait()
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()

    return token, chat_id_holder.get("id")


def write_env(ynab_token: str, telegram_token: str) -> None:
    lines = [
        f"YNAB_TOKEN={ynab_token}\n",
        f"TELEGRAM_BOT_TOKEN={telegram_token}\n",
    ]
    ENV.write_text("".join(lines))
    print(f"Wrote {ENV}")


def update_config(budget_id: str, chat_id: int) -> None:
    data = yaml.safe_load(CONFIG.read_text())
    data["ynab"]["budget_id"] = budget_id
    data["gmail_accounts"][0]["chat_id"] = int(chat_id)
    CONFIG.write_text(yaml.safe_dump(data, sort_keys=False))
    print(f"Updated {CONFIG}")


def main() -> None:
    ensure_config()
    ynab_token, budget_id = capture_ynab()
    telegram_token, chat_id = asyncio.run(capture_telegram_chat_id())
    if not chat_id:
        sys.exit("Did not receive chat_id — exiting.")
    write_env(ynab_token, telegram_token)
    update_config(budget_id, chat_id)
    print(
        "\nDone. Next: run scripts/setup_scheduled_tasks.ps1 "
        "(admin PowerShell) to install schedulers."
    )


if __name__ == "__main__":
    main()
