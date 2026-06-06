"""Typed configuration loaded from config.yaml + environment."""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel, Field


class GmailAccount(BaseModel):
    email: str
    user_id: str
    token_path: str
    chat_id: int


class EmailSource(BaseModel):
    name: str
    query: str
    parser: Literal["amazon", "amazon_shipment", "venmo", "retailer_order"]


class ObservedSource(BaseModel):
    """Phase 0: sender we want to capture raw samples from, but not yet parse.

    Sample collector loops these on a schedule, stuffs matching emails into
    `raw_email_sample`. When we've seen enough samples to write a parser, we
    move this entry to `email_sources` and the parser kicks in.
    """
    label: str
    query: str
    expected_volume: str = "low"  # informational only


class YnabConfig(BaseModel):
    budget_id: str
    poll_interval_minutes: int = 30
    enqueue_after: date = date.min


class OllamaConfig(BaseModel):
    endpoint: str = "http://localhost:11434"
    model: str = "qwen3-coder:30b"
    temperature: float = 0.3


class TelegramConfig(BaseModel):
    quiet_hours: str = "22:00-07:00"
    daily_digest_time: str = "09:00"
    # Phase 5 — proactive summary scheduling. Each is "HH:MM" 24-hour local.
    # Both default to outside the default quiet hours so they fire reliably.
    daily_summary_time: str = "07:30"
    weekly_summary_time: str = "18:00"
    # Python weekday: Monday=0, ..., Sunday=6
    weekly_summary_day: int = 6


class Paths(BaseModel):
    database: str = "./ynab_helper.db"
    log_dir: str = "./logs"


class Settings(BaseModel):
    gmail_accounts: list[GmailAccount]
    email_sources: list[EmailSource]
    observed_sources: list[ObservedSource] = []  # Phase 0: sampling sidecar
    ynab: YnabConfig
    ollama: OllamaConfig
    telegram: TelegramConfig
    paths: Paths
    ynab_token: str = Field(default="", repr=False)
    telegram_bot_token: str = Field(default="", repr=False)


def load_settings(config_path: Path | str = "config.yaml") -> Settings:
    load_dotenv()
    config_path = Path(config_path)
    if not config_path.exists():
        raise FileNotFoundError(
            f"Config file not found at {config_path}. "
            f"Copy config.yaml.example to config.yaml and edit."
        )
    data = yaml.safe_load(config_path.read_text())
    data["ynab_token"] = os.getenv("YNAB_TOKEN", "")
    data["telegram_bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", "")
    return Settings.model_validate(data)
