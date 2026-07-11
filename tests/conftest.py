"""
Shared fixtures. Every test runs against a throwaway SQLite file — never the
real data/news.db — and never touches the network or a browser.
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point config.DB_PATH at a fresh temp file and initialise the schema."""
    db_path = str(tmp_path / "test_news.db")
    monkeypatch.setattr(config, "DB_PATH", db_path)
    from database.models import init_db
    init_db()
    return db_path
