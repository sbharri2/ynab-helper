"""Tests for the Tauri/web UI's localhost HTTP API.

``fixture_db`` copies the live database once per test module so tests read
real data without ever touching the live file (bot stays the single
writer — see feedback_bulk_db_ops_on_gdrive / project_db_on_gdrive memory).
"""
import json
import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

LIVE_DB = Path(r"C:\Users\Steven\ynabhelper\ynab_helper.db")


@pytest.fixture(scope="module")
def fixture_db(tmp_path_factory) -> str:
    tmp_dir = tmp_path_factory.mktemp("webui_db")
    dest = tmp_dir / "test.db"
    shutil.copy(LIVE_DB, dest)
    return str(dest)


def make_app(tmp_path, db_path):
    (tmp_path / "ui_api_token.txt").write_text("legacy-tok")
    (tmp_path / "ui_api_tokens.json").write_text(
        json.dumps({"allison-tok": "allison"}))
    from bot import http_api
    return http_api.build_app(db_path=db_path, token_dir=tmp_path)


def test_token_identity(tmp_path, fixture_db):
    app = make_app(tmp_path, fixture_db)
    c = TestClient(app)
    assert c.get("/categories",
                 headers={"x-api-token": "legacy-tok"}).status_code == 200
    assert c.get("/categories",
                 headers={"x-api-token": "allison-tok"}).status_code == 200
    assert c.get("/categories",
                 headers={"x-api-token": "wrong"}).status_code == 401


def test_registry_q_categories(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_categories"](fixture_db)
    assert rows and {"id", "group_id", "group_name", "name",
                     "is_spending", "hidden"} <= set(rows[0])


def test_q_route(tmp_path, fixture_db):
    app = make_app(tmp_path, fixture_db)
    c = TestClient(app)
    h = {"x-api-token": "legacy-tok"}
    assert c.post("/q/q_categories", json={}, headers=h).status_code == 200
    assert c.post("/q/nope", json={}, headers=h).status_code == 404
    assert c.post("/q/q_categories", json={}).status_code in (401, 422)


def test_q_transactions_shape(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_transactions"](fixture_db, since="2026-07-01",
                                       until="2026-07-31", limit=50)
    assert rows
    need = {"id", "account_name", "posted_date", "amount_cents", "payee",
            "category_name", "pending_id", "decision_state",
            "asked_in_chat", "account_on_budget", "is_split"}
    assert need <= set(rows[0])
    assert isinstance(rows[0]["asked_in_chat"], bool)
    assert isinstance(rows[0]["account_on_budget"], bool)
    assert isinstance(rows[0]["is_split"], bool)


def test_q_transactions_limit_offset(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_transactions"](fixture_db, limit=3, offset=0)
    assert len(rows) == 3


def test_q_month_categories_available_identity(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_month_categories"](fixture_db, month="2026-07")
    r = next(x for x in rows if x["category_name"] == "Groceries")
    assert isinstance(r["available_cents"], int)


def test_q_category_avg_activity_shape(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_category_avg_activity"](fixture_db, months=6)
    assert rows
    need = {"category_id", "months_observed", "avg_cents"}
    assert need <= set(rows[0])
    # avg_cents is a magnitude (outflows negated in the SQL), never negative.
    assert all(r["avg_cents"] >= 0 for r in rows)


def test_q_cash_trace_shape_and_walk(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_cash_trace"](fixture_db, months=6)
    assert len(rows) == 6
    need = {"month", "inflow_cents", "outflow_cents", "net_cents",
            "end_cash_cents"}
    assert need <= set(rows[0])
    for r in rows:
        assert r["net_cents"] == r["inflow_cents"] + r["outflow_cents"]
    # months clamp to 1..=24 (matches the Rust `.clamp(1, 24)`).
    assert len(REGISTRY["q_cash_trace"](fixture_db, months=999)) == 24
    assert len(REGISTRY["q_cash_trace"](fixture_db, months=0)) == 1


def test_q_cc_balance_summary_shape(fixture_db):
    from bot.webui_queries import REGISTRY
    row = REGISTRY["q_cc_balance_summary"](fixture_db)
    need = {"cc_total_owed_cents", "checking_total_cents",
            "after_payoff_cents", "cards"}
    assert need <= set(row)
    assert row["after_payoff_cents"] == (
        row["checking_total_cents"] - row["cc_total_owed_cents"])
    if row["cards"]:
        assert {"account_id", "name", "balance_cents"} <= set(row["cards"][0])


def test_q_inbox_shape(fixture_db):
    from bot.webui_queries import REGISTRY
    rows = REGISTRY["q_inbox"](fixture_db)
    if rows:
        need = {"id", "txn_date", "payee", "amount_cents", "memo",
                "raw_summary", "status", "queue_lane", "assigned_to_user_id",
                "suggested_category_id", "suggested_category_name"}
        assert need <= set(rows[0])
        assert all(r["status"] in ("pending", "skipped") for r in rows)


def test_income_sources_golden(fixture_db):
    from bot.webui_queries import REGISTRY
    srcs = {s["payee_key"]: s for s in REGISTRY["q_income_sources"](fixture_db)}
    assert "ACH O'BRIEN/ATKINS A" in srcs
    assert srcs["ACH O'BRIEN/ATKINS A"]["last_seen"] == "2026-07-14"
    assert "ACH ACTALENT, INC." in srcs          # manual weekly override
    assert srcs["ACH ACTALENT, INC."]["cadence"] == "weekly"


def test_rta_matches_identity(fixture_db):
    from bot.webui_queries import REGISTRY
    rta = REGISTRY["q_ready_to_assign"](fixture_db, month="2026-07")
    assert rta["ready_to_assign_cents"] == rta["cash_cents"] - rta["available_cents"]
    _ = [p["expected_date"] for p in rta["paychecks"]
         if p["source_payee"] == "ACH O'BRIEN/ATKINS A"]
    assert "2026-07-14" in " ".join(
        (p.get("actual_date") or p["expected_date"]) for p in rta["paychecks"])


def test_q_transactions_camelcase_body_filters(tmp_path, fixture_db):
    """DEBT from Task 2's review: prove POST /q/{name} converts a camelCase
    body key (accountId) to the snake_case kwarg (account_id) the query
    function expects, and that the filter actually took effect."""
    app = make_app(tmp_path, fixture_db)
    c = TestClient(app)
    h = {"x-api-token": "legacy-tok"}

    unfiltered = c.post("/q/q_transactions", json={"limit": 25},
                         headers=h)
    assert unfiltered.status_code == 200
    all_rows = unfiltered.json()
    assert all_rows
    target_account_id = all_rows[0]["account_id"]

    resp = c.post("/q/q_transactions",
                   json={"accountId": target_account_id, "limit": 25},
                   headers=h)
    assert resp.status_code == 200
    rows = resp.json()
    assert rows
    assert all(r["account_id"] == target_account_id for r in rows)
