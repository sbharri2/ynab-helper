"""Did any /categorize requests reach the bot? Check audit_log."""
from bot import storage
from bot.config import load_settings

s = load_settings()
with storage.connect(s.paths.database) as con:
    cols = [r["name"] for r in con.execute("PRAGMA table_info(audit_log)").fetchall()]
    payload_col = "details" if "details" in cols else cols[-1]
    rows = con.execute(
        f"SELECT id, ts, event, {payload_col} as p FROM audit_log "
        "WHERE event = 'ui_categorize' ORDER BY id DESC LIMIT 5"
    ).fetchall()
    if not rows:
        print("No ui_categorize events EVER. The click never reached the API.")
    else:
        print(f"{len(rows)} recent ui_categorize events:")
        for r in rows:
            print(f"  #{r['id']}  {r['ts']}  {(r['payload'] or '')[:120]}")
