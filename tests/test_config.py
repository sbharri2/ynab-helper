from pathlib import Path
import yaml
from bot.config import load_settings

def test_load_settings_from_example(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(yaml.safe_dump({
        "gmail_accounts": [{
            "email": "test@gmail.com",
            "user_id": "test",
            "token_path": "/tmp/tok.json",
            "chat_id": 123,
        }],
        "email_sources": [
            {"name": "amazon", "query": "from:x", "parser": "amazon"},
        ],
        "ynab": {"budget_id": "abc-123", "poll_interval_minutes": 30},
        "ollama": {"endpoint": "http://localhost:11434", "model": "qwen2.5:14b", "temperature": 0.3},
        "telegram": {"quiet_hours": "22:00-07:00", "daily_digest_time": "09:00"},
        "paths": {"database": "./test.db", "log_dir": "./logs"},
    }))
    monkeypatch.setenv("YNAB_TOKEN", "ynab-secret")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "tg-secret")

    s = load_settings(cfg)

    assert s.gmail_accounts[0].email == "test@gmail.com"
    assert s.gmail_accounts[0].chat_id == 123
    assert s.ynab.budget_id == "abc-123"
    assert s.ynab_token == "ynab-secret"
    assert s.telegram_bot_token == "tg-secret"
    assert s.ollama.model == "qwen2.5:14b"
