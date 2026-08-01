from bot.ingest import _PROMPT_USER_KINDS


def test_ynab_sync_is_a_prompt_kind():
    """Regression: bot/ynab_watcher.py was deleted in the Phase 7+ cleanup,
    but ingest.py's comment still claimed it queued ynab_sync rows. Every
    YNAB-sourced transaction since then had no question path at all."""
    assert "ynab_sync" in _PROMPT_USER_KINDS


def test_auto_commit_block_is_gone():
    """Ingest must no longer set status='categorized' at ingest time.
    Dispatch owns that decision now."""
    src = open("bot/ingest.py", encoding="utf-8").read()
    assert "auto_commit" not in src
    assert "auto_prior" not in src
