"""All tests use an isolated corpus and never load real project credentials."""
import os

os.environ["PYTHON_DOTENV_DISABLED"] = "1"

import pytest


@pytest.fixture(autouse=True)
def isolated_runtime(tmp_path, monkeypatch):
    monkeypatch.setenv("RAG_DB_PATH", str(tmp_path / "default.sqlite3"))
    monkeypatch.setenv("RAG_DOCS_DIR", str(tmp_path / "default_docs"))
    monkeypatch.setenv("RAG_MD_DIR", str(tmp_path / "default_md"))
    monkeypatch.setenv("RAG_ORIGINALS_DIR", str(tmp_path / "originals"))
    monkeypatch.setenv("RAG_DB_BACKEND", "sqlite")
    monkeypatch.setenv("RAG_SEARCH_BACKEND", "sqlite")
    monkeypatch.setenv("RAG_TASK_BACKEND", "inline")
    monkeypatch.setenv("RAG_OCR_MODE", "disabled")
    monkeypatch.setenv("RAG_AUTOSCAN", "0")
    monkeypatch.setenv("RAG_SCAN_INTERVAL_MIN", "0")
    for name in ("GEMMA_API_URL", "GEMMA_API_KEY", "GEMMA_MODEL", "GEMMA_TIMEOUT",
                 "GEMMA_MAX_TOKENS", "RAG_API_TOKEN", "RAG_USER_GROUPS"):
        # Register every key for cleanup even when dotenv adds an initially absent key.
        monkeypatch.setenv(name, "")
        monkeypatch.delenv(name)
