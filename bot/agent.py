"""AI-mediated Telegram chat layer.

Steven types free text. We route to a local Ollama instance (qwen3:32b) with
a set of tools (see bot/agent_tools.py). The model picks a tool, we execute
it, optionally feed the result back to the model for natural-language polish,
and DM the final string.

Why this exists: slash commands stop scaling once you have a real budget
workflow. "how much is in groceries", "move 50 from dining to groceries",
"show me amazon last 30 days" — these should all just work via chat.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

from bot import agent_tools, storage
from bot.config import Settings

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Tool schemas (OpenAI-compatible, Ollama accepts this format)
# ---------------------------------------------------------------------------

TOOLS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_category_available",
            "description": "Get how much money is available in a category for the current month. Use when the user asks 'how much is left in X?', 'what's in groceries?', 'available for dining?'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category_name": {
                        "type": "string",
                        "description": "Category name (e.g. 'Groceries', 'Dining Out'). Fuzzy match.",
                    },
                },
                "required": ["category_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_account_balance",
            "description": "Get the current balance of an account. Use when the user asks 'how much in checking?', 'amex balance?', 'what's in my savings?'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "account_name": {
                        "type": "string",
                        "description": "Account name (e.g. 'Joint Checking', 'Citi'). Fuzzy match.",
                    },
                },
                "required": ["account_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_categories_overview",
            "description": "List all (or filtered) categories with their current available balance. Use when the user asks 'what categories are there?', 'show me my budget', 'what's my monthly bills look like?'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "group_filter": {
                        "type": "string",
                        "description": "Optional category group name to filter (e.g. 'Monthly Bills', 'Day to Day'). Omit for all groups.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_drill_down",
            "description": "Show recent transactions for a category, payee, or account. Use when the user asks 'show me amazon last month', 'what did i spend on groceries?', 'transactions on checking?'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {
                        "type": "string",
                        "description": "What to drill into — category name, payee name, or account name.",
                    },
                    "kind": {
                        "type": "string",
                        "enum": ["category", "payee", "account"],
                        "description": "Which kind of target. Default 'category'.",
                    },
                    "days": {
                        "type": "integer",
                        "description": "Lookback window in days. Default 30.",
                    },
                },
                "required": ["target"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "show_summary",
            "description": "Show a snapshot of the household budget: overspent categories, tight categories, recent activity. Use when the user asks 'how are we doing?', 'where are we tight?', 'summary'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "period": {
                        "type": "string",
                        "enum": ["today", "yesterday", "this_week"],
                        "description": "Time window. Default 'today'.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "assign_to_category",
            "description": "Assign (add) money to a category's budget for the current month. Use when the user says 'assign 200 to groceries', 'put 50 in dining'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category_name": {"type": "string"},
                    "amount_dollars": {
                        "type": "number",
                        "description": "Dollar amount, positive to assign, negative to take back.",
                    },
                },
                "required": ["category_name", "amount_dollars"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_historical_budget",
            "description": "Set this month's budget for every spending category to the median outflow over the lookback window. Use this once at the start of a month to seed envelopes from history. Use when the user says 'set up my budget from history', 'apply my typical budget', 'budget like last month'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "months_lookback": {
                        "type": "integer",
                        "description": "How many months of history to average over. Default 12.",
                    },
                    "multiplier": {
                        "type": "number",
                        "description": "Scale factor applied to the median (e.g. 1.1 to budget 10% more than typical). Default 1.0.",
                    },
                    "dry_run": {
                        "type": "boolean",
                        "description": "When true, returns the proposed budget without applying it. Default false.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_money",
            "description": "Move money from one category's budget to another within the current month. Use when the user says 'move 50 from dining to groceries', 'shift 200 from vacation to gifts'.",
            "parameters": {
                "type": "object",
                "properties": {
                    "from_category": {"type": "string"},
                    "to_category": {"type": "string"},
                    "amount_dollars": {"type": "number"},
                },
                "required": ["from_category", "to_category", "amount_dollars"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "categorize_pending",
            "description": "If the bot has just asked the user to categorize a transaction (a pending item is in flight), use this to confirm the category by name. Use when the user names a category in response to a pending prompt.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category_name": {"type": "string"},
                },
                "required": ["category_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "skip_pending",
            "description": "Skip the currently in-flight pending categorization item. Use when the user says 'skip', 'I don't know', 'pass'.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "next_pending",
            "description": "Advance to the next pending categorization item — clear the current in-flight question and DM the next one. Use when the user says 'next', 'next one', 'move on', 'show me the next', 'what else?'.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_category",
            "description": "Create a new YNAB category (and mirror it locally). Use when the user says 'create a new category for X', 'add a category called Y', 'I need a category for my password manager'. Pass the human name. Optionally pass a group_name and a monthly_target_dollars so the category has a recurring need goal from day one (e.g. 'create Password Manager for $5/mo under Annual or Seasonal Costs' → category_name='Password Manager', group_name='Annual or Seasonal Costs', monthly_target_dollars=5).",
            "parameters": {
                "type": "object",
                "properties": {
                    "category_name": {"type": "string"},
                    "group_name": {
                        "type": "string",
                        "description": "Optional group name like 'Monthly Bills', 'Day to Day Expenses', 'Annual or Seasonal Costs'. Defaults to Monthly Bills.",
                    },
                    "monthly_target_dollars": {
                        "type": "number",
                        "description": "Optional. When set, YNAB attaches a monthly Need goal of this dollar amount. Use for recurring subscriptions / bills.",
                    },
                },
                "required": ["category_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "unskip_pending",
            "description": "Return previously-skipped transactions to the active queue. Use when the user says 'revisit skipped', 'unskip everything', 'let me look at what I skipped', 'go back through them'. Supports filtering by recency (since_hours) or payee substring (payee_filter). Defaults to all skipped.",
            "parameters": {
                "type": "object",
                "properties": {
                    "since_hours": {
                        "type": "integer",
                        "description": "Only unskip rows skipped within the last N hours. Omit for all.",
                    },
                    "payee_filter": {
                        "type": "string",
                        "description": "Only unskip rows whose payee matches this substring (case-insensitive). Omit for all.",
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_category",
            "description": "Rename an existing category. Use when the user says 'rename X to Y', 'change X to Z', 'X should be called Y'. Preserves the exact spelling the user provides (no auto title-casing).",
            "parameters": {
                "type": "object",
                "properties": {
                    "category_name": {"type": "string", "description": "Current name (fuzzy match)."},
                    "new_name": {"type": "string", "description": "Exact new name."},
                },
                "required": ["category_name", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_category_to_group",
            "description": "Move an existing category to a different category group. Use when the user says 'wrong group', 'move Password Manager to Annual', 'put X under Y instead'. YNAB allows category to group reassignment via PATCH.",
            "parameters": {
                "type": "object",
                "properties": {
                    "category_name": {"type": "string"},
                    "group_name": {
                        "type": "string",
                        "description": "Target group name (e.g. 'Annual or Seasonal Costs', 'Day to Day Expenses', 'Monthly Bills').",
                    },
                },
                "required": ["category_name", "group_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_quiet",
            "description": "Mute proactive notifications for N hours. Use when the user says 'pause for 2 hours', 'be quiet', 'quiet on'. Pass 0 to resume.",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer"},
                },
                "required": ["hours"],
            },
        },
    },
]


TOOL_NAME_TO_FUNCTION = {
    "get_category_available": agent_tools.get_category_available,
    "get_account_balance": agent_tools.get_account_balance,
    "list_categories_overview": agent_tools.list_categories_overview,
    "show_drill_down": agent_tools.show_drill_down,
    "show_summary": agent_tools.show_summary_tool,
    "assign_to_category": agent_tools.assign_to_category_tool,
    "apply_historical_budget": agent_tools.apply_historical_budget_tool,
    "move_money": agent_tools.move_money_tool,
    "categorize_pending": agent_tools.categorize_pending_tool,
    "skip_pending": agent_tools.skip_pending_tool,
    "next_pending": agent_tools.next_pending_tool,
    "unskip_pending": agent_tools.unskip_pending_tool,
    "create_category": agent_tools.create_category_tool,
    "move_category_to_group": agent_tools.move_category_to_group_tool,
    "rename_category": agent_tools.rename_category_tool,
    "set_quiet": agent_tools.set_quiet_tool,
}


SYSTEM_PROMPT = """You are Steven's personal finance assistant, integrated into a Telegram bot for the ynab-helper project.

Steven and (eventually) his wife Allison chat with you in natural language about their household budget. You have a set of tools that read from and write to a local SQLite ledger that mirrors YNAB.

GUIDELINES:
- When the user asks anything about money — balances, budgets, spending, categories, accounts — INVOKE A TOOL. Do not make up numbers from your head.
- When the user issues an instruction to modify the budget (assign, move, categorize, skip, mute) — INVOKE A TOOL. Do not ask "should I?"; just do it and confirm.
- If you're confused, ask a brief clarifying question rather than guessing.
- Keep replies tight. The interface is a phone notification, not a chat window.
- If a tool returns a long string, you can either pass it through directly OR add a one-sentence framing — do not duplicate the contents.
- Use the tools' built-in fuzzy matching — pass user-written names verbatim (e.g. "groceries", not "Groceries").
- Today's date and the current month are implicit; the tools handle that.
- Never invent category or account names. If the user's name doesn't resolve, surface the tool's refusal message.
"""


def _load_last_turns(db_path: str, chat_id: int, *, n: int = 4) -> list[dict]:
    """Pull last `n` user+assistant message pairs for context."""
    with storage.connect(db_path) as con:
        row = con.execute(
            "SELECT last_turns_json FROM bot_conversation WHERE chat_id = ?",
            (chat_id,),
        ).fetchone()
    if not row or not row["last_turns_json"]:
        return []
    try:
        data = json.loads(row["last_turns_json"])
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return data[-n * 2:]


def _save_turn(
    db_path: str, chat_id: int, user_id: str,
    user_text: str, assistant_text: str,
) -> None:
    """Append (user, assistant) to last_turns_json. Caps at last 5 pairs."""
    history = _load_last_turns(db_path, chat_id, n=4)
    history.append({"role": "user", "content": user_text})
    history.append({"role": "assistant", "content": assistant_text})
    # Cap at last 10 entries (5 pairs)
    history = history[-10:]
    with storage.connect(db_path) as con:
        con.execute(
            """INSERT INTO bot_conversation (chat_id, user_id, last_turns_json)
               VALUES (?, ?, ?)
               ON CONFLICT(chat_id) DO UPDATE SET last_turns_json = excluded.last_turns_json""",
            (chat_id, user_id, json.dumps(history)),
        )


def _call_ollama(
    endpoint: str, model: str, messages: list[dict],
    tools: list[dict] | None = None,
    temperature: float = 0.3,
) -> dict:
    """Single Ollama /api/chat call. Returns the raw response message dict."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "options": {"temperature": temperature},
    }
    if tools:
        payload["tools"] = tools
    # NOTE: Do NOT set "format": "json" when using tools — they conflict.
    with httpx.Client(timeout=120.0) as client:
        resp = client.post(f"{endpoint}/api/chat", json=payload)
        data = resp.json()
    return data.get("message", {})


def run_agent_turn(
    db_path: str,
    *,
    user_text: str,
    chat_id: int,
    user_id: str,
    settings: Settings,
) -> str:
    """Process one Telegram message from the user and return what to reply.

    Flow:
      1. Build messages = [system, ...prior_turns, user_text]
      2. Call qwen3:32b with TOOLS
      3. If model returned tool_calls: execute each, build a follow-up
         conversation that includes the tool results, ask the model to
         produce a final natural-language reply.
      4. Persist the turn pair for next time.
    """
    history = _load_last_turns(db_path, chat_id, n=4)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}, *history,
                {"role": "user", "content": user_text}]

    endpoint = settings.ollama.endpoint
    model = settings.ollama.model
    temp = settings.ollama.temperature

    try:
        first = _call_ollama(endpoint, model, messages, tools=TOOLS, temperature=temp)
    except Exception as e:  # noqa: BLE001
        log.exception("ollama call failed: %s", e)
        reply = "I had trouble reaching the local model. Try again in a moment."
        _save_turn(db_path, chat_id, user_id, user_text, reply)
        return reply

    tool_calls = first.get("tool_calls") or []
    if not tool_calls:
        # Plain text response — pass through
        reply = (first.get("content") or "").strip() or "(no response)"
        _save_turn(db_path, chat_id, user_id, user_text, reply)
        return reply

    # Execute every tool call. Collect results.
    tool_messages: list[dict] = []
    user_visible: list[str] = []
    for tc in tool_calls:
        fn_block = tc.get("function") or {}
        name = fn_block.get("name", "")
        args = fn_block.get("arguments") or {}
        if isinstance(args, str):
            try:
                args = json.loads(args)
            except (ValueError, TypeError):
                args = {}
        func = TOOL_NAME_TO_FUNCTION.get(name)
        if func is None:
            result = f"(unknown tool: {name})"
        else:
            try:
                # Inject context the model doesn't see (chat_id, db_path)
                injected = {**args}
                if name in {"categorize_pending", "skip_pending",
                            "next_pending", "set_quiet"}:
                    injected["chat_id"] = chat_id
                if name in {"create_category", "move_category_to_group",
                            "rename_category"}:
                    injected["settings"] = settings
                result = func(db_path, **injected)
            except Exception as e:  # noqa: BLE001
                log.exception("tool %s failed: %s", name, e)
                result = f"(error invoking {name}: {e})"
        user_visible.append(result)
        tool_messages.append({
            "role": "tool",
            "content": result,
            "tool_call_name": name,  # informational; Ollama doesn't strictly need this
        })

    # For most tool calls, the tool's own output is already the user-facing
    # reply. Skip the follow-up Ollama call to save 10+ seconds. We only
    # ask the model to summarize when there are multiple tool calls and the
    # combined output would otherwise be confusing.
    if len(tool_calls) == 1:
        reply = user_visible[0]
    else:
        # Multi-tool case: ask the model to compose a final reply
        followup_messages = [
            *messages,
            {"role": "assistant", "tool_calls": tool_calls, "content": ""},
            *tool_messages,
        ]
        try:
            second = _call_ollama(
                endpoint, model, followup_messages, tools=None, temperature=temp,
            )
            reply = (second.get("content") or "").strip() or "\n\n".join(user_visible)
        except Exception as e:  # noqa: BLE001
            log.warning("follow-up summarization failed; concatenating tool outputs: %s", e)
            reply = "\n\n".join(user_visible)

    _save_turn(db_path, chat_id, user_id, user_text, reply)
    return reply
