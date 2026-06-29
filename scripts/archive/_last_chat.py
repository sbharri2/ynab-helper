"""Read the last conversation turns from bot_conversation."""
import json
from bot import storage

with storage.connect("ynab_helper.db") as con:
    r = con.execute(
        "SELECT last_asked_id, last_asked_kind, last_asked_message_id, "
        "last_action_at, last_turns_json FROM bot_conversation"
    ).fetchone()
    d = dict(r)
    print(f"last_asked_id      = {d['last_asked_id']}")
    print(f"last_asked_kind    = {d['last_asked_kind']}")
    print(f"last_asked_msg_id  = {d['last_asked_message_id']}")
    print(f"last_action_at     = {d['last_action_at']}")
    print("\n=== last_turns_json ===")
    try:
        turns = json.loads(d["last_turns_json"] or "[]")
        for i, t in enumerate(turns):
            print(f"\n[{i}] {t.get('role','?')}:")
            print(t.get("content", ""))
    except Exception as e:
        print(f"parse failed: {e}")
        print(d["last_turns_json"])

    print("\n=== recent categorize-related audit ===")
    rows = con.execute(
        "SELECT id, event, details, ts FROM audit_log "
        "WHERE ts >= '2026-06-15 12:00:00' "
        "  AND event IN ('categorized','skipped','ignored_dm_skipped') "
        "ORDER BY id DESC LIMIT 10"
    ).fetchall()
    for r in rows:
        print(f"  #{r['id']} {str(r['ts'])[:19]}  {r['event']}  {r['details']}")
