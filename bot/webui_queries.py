"""Read-query registry for the mobile web UI's `POST /q/{name}` route.

Each entry is ``f(db_path, **args) -> JSON-serializable`` — a straight port
of the desktop Tauri commands in ynabhelper-ui/src-tauri/src/commands.rs, so
the mobile SPA can read the same data over HTTP instead of Tauri IPC. Field
names match the TypeScript interfaces in ynabhelper-ui/src/lib/types.ts
exactly (both surfaces share one shape).
"""
from __future__ import annotations

from typing import Any, Callable

from bot import storage


def q_categories(db_path: str, **_: Any) -> list[dict[str, Any]]:
    """Every visible category — port of commands.rs:109 (`Category[]`)."""
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT c.id, c.group_id, g.name AS group_name, c.name, "
            "  COALESCE(c.is_spending, 0) AS is_spending, "
            "  COALESCE(c.hidden, 0) AS hidden, "
            "  c.goal_kind, c.goal_target_cents "
            "FROM category c "
            "JOIN category_group g ON g.id = c.group_id "
            "WHERE c.hidden = 0 AND g.hidden = 0 "
            "  AND g.name != 'Internal Master Category' "
            "ORDER BY g.sort_order, c.name"
        ).fetchall()
    return [dict(r) for r in rows]


def q_category_groups(db_path: str, **_: Any) -> list[dict[str, Any]]:
    """Unhidden category groups — port of commands.rs:89 (`CategoryGroup[]`)."""
    with storage.connect(db_path) as con:
        rows = con.execute(
            "SELECT id, name, COALESCE(sort_order, 0) AS sort_order "
            "FROM category_group WHERE hidden = 0 ORDER BY sort_order, name"
        ).fetchall()
    return [dict(r) for r in rows]


REGISTRY: dict[str, Callable[..., Any]] = {
    "q_categories": q_categories,
    "q_category_groups": q_category_groups,
}
