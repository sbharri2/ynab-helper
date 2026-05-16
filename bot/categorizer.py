"""Ollama-backed category suggester.

Sends a structured prompt to a local Ollama instance and parses the
JSON-mode reply into ``{category_id, confidence, reasoning}``. All errors
(connection refused, timeout, malformed JSON, missing keys) are caught
and converted into a None-category fallback so the bot keeps working
even when Ollama is offline.
"""
from __future__ import annotations

import json
import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)


SYSTEM_PROMPT = """You are a personal-finance assistant that assigns transactions to YNAB categories.

You will be given:
- A transaction summary (payee, items, memo)
- The amount in cents
- The transaction date
- The source (e.g. amazon, venmo)
- A list of available categories, each with id, name, and group

Pick the SINGLE best category from the provided list.

Respond ONLY with a JSON object matching this exact shape:
{
  "category_id": "<one of the provided ids, or null if none fit>",
  "confidence": <float between 0.0 and 1.0>,
  "reasoning": "<one short sentence>"
}

Do not include any text outside the JSON object. Do not invent category ids
that were not in the provided list.
"""


class Categorizer:
    def __init__(
        self,
        endpoint: str,
        model: str,
        temperature: float = 0.3,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.model = model
        self.temperature = temperature

    def _build_user_message(
        self,
        *,
        summary: str,
        amount_cents: int,
        date_str: str,
        source: str,
        categories: list[dict],
    ) -> str:
        amount_dollars = amount_cents / 100.0
        lines = [
            f"Source: {source}",
            f"Date: {date_str}",
            f"Amount: ${amount_dollars:.2f}",
            f"Summary: {summary}",
            "",
            "Available categories:",
        ]
        for c in categories:
            lines.append(f"- {c['id']} | {c['group']} > {c['name']}")
        return "\n".join(lines)

    def suggest(
        self,
        *,
        summary: str,
        amount_cents: int,
        date_str: str,
        source: str,
        categories: list[dict],
    ) -> dict[str, Any]:
        """Ask Ollama to pick a category. Always returns a dict; never raises."""
        try:
            user_msg = self._build_user_message(
                summary=summary,
                amount_cents=amount_cents,
                date_str=date_str,
                source=source,
                categories=categories,
            )
            payload = {
                "model": self.model,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": user_msg},
                ],
                "stream": False,
                "format": "json",
                "options": {"temperature": self.temperature},
            }
            with httpx.Client(timeout=60.0) as client:
                resp = client.post(f"{self.endpoint}/api/chat", json=payload)
                data = resp.json()
            content = data["message"]["content"]
            parsed = json.loads(content)
            return {
                "category_id": parsed.get("category_id"),
                "confidence": float(parsed.get("confidence", 0.0)),
                "reasoning": parsed.get("reasoning", ""),
            }
        except Exception as e:  # noqa: BLE001 - graceful fallback by design
            log.warning("Categorizer.suggest failed: %s", e)
            return {
                "category_id": None,
                "confidence": 0.0,
                "reasoning": "",
                "error": str(e),
            }
