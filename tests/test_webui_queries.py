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
