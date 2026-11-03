import pytest
import yaml
from app.settings import get_settings
from database.db import get_conn
from rag.graph import run_rag

def test_search_cycles_above_three_are_rejected_in_settings(tmp_path, monkeypatch):
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump({"retrieval": {"max_cycles": 4}}), encoding="utf8")
    monkeypatch.setenv("RAG_CONFIG_PATH", str(path))
    with pytest.raises(ValueError):
        get_settings()

def test_runtime_cannot_override_cycle_limit_to_four():
    conn = get_conn()
    try:
        with pytest.raises(ValueError):
            run_rag("고온 출력 저하", ["all"], conn, max_cycles=4)
    finally:
        conn.close()
