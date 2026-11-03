"""Isolated native Windows worker/API/scan/retrieval/download smoke, no real corpus."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main():
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    with tempfile.TemporaryDirectory(prefix="rag-windows-native-smoke-") as folder:
        root = Path(folder)
        docs = root / "docs"
        docs.mkdir()
        env = dict(os.environ, RAG_DB_BACKEND="sqlite", RAG_SEARCH_BACKEND="sqlite",
            RAG_TASK_BACKEND="native", RAG_DB_PATH=str(root / "test.sqlite3"),
            RAG_DOCS_DIR=str(docs), RAG_MD_DIR=str(root / "md"), RAG_ORIGINALS_DIR=str(root / "originals"),
            RAG_AUTOSCAN="0", RAG_SCAN_INTERVAL_MIN="0", USE_QWEN=os.getenv("USE_QWEN", "0"), RAG_OCR_MODE="disabled",
            GEMMA_API_URL="", RAG_API_TOKEN="", RAG_USER_GROUPS="all", PYTHONIOENCODING="utf-8")
        os.environ.update(env)
        from scripts.e2e_check import prepare
        prepare(docs)
        stop = root / "stop"
        log = root / "worker.log"
        with log.open("wb") as output:
            worker = subprocess.Popen([sys.executable, "-m", "workers.native_worker",
                "--poll-interval", ".1", "--stop-file", str(stop)], cwd=ROOT, env=env,
                stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        try:
            from app.main import app
            from fastapi.testclient import TestClient
            from models.gemma_client import MockGemmaClient
            class LLM(MockGemmaClient):
                def judge(self, question, text):
                    return "RELEVANT" in text and "UNRELATED" not in text
                def rewrite(self, question, rejected):
                    return "heat test cooling verification"
            with TestClient(app) as client:
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    if client.get("/ready").status_code == 200:
                        break
                    if worker.poll() is not None:
                        raise RuntimeError("Native worker exited: " + log.read_text(errors="replace")[-1000:])
                    time.sleep(.1)
                else:
                    raise RuntimeError("Native worker heartbeat did not become ready")
                scan = client.post("/api/v1/preprocess")
                assert scan.status_code == 200 and scan.json()["status"] == "QUEUED", scan.text
                deadline = time.monotonic() + (180 if env["USE_QWEN"] == "1" else 60)
                while time.monotonic() < deadline:
                    jobs = client.get("/api/v1/jobs").json()["jobs"]
                    if len(jobs) == 6 and all(job["status"] == "DONE" for job in jobs):
                        break
                    if any(job["status"] == "FAILED" for job in jobs):
                        raise RuntimeError(json.dumps(jobs))
                    time.sleep(.1)
                else:
                    raise RuntimeError("Background preprocessing did not finish")
                with patch("rag.graph.get_client", return_value=LLM()):
                    response = client.post("/api/v1/query", json={"query": "heat test power drop cause"})
                assert response.status_code == 200, response.text
                answer = response.json()
                assert answer["retrieval_count"] == 5, answer
                assert len({s["document_id"] for s in answer["sources"]}) == 5
                for source in answer["sources"]:
                    assert client.get(source["download_url"]).status_code == 200
                    assert client.get(source["markdown_url"]).status_code == 200
                assert len(list((root / "md").glob("*.md"))) == 6
                from database.db import get_conn
                conn = get_conn()
                try:
                    vectors = conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
                    if env["USE_QWEN"] == "1":
                        assert vectors == 6
                finally:
                    conn.close()
                print(json.dumps({"status": "PASS", "backend": "native", "files": len(jobs),
                    "sources": len(answer["sources"]), "worker_subprocess": True,
                    "broker_required": False, "embedding": "qwen" if env["USE_QWEN"] == "1" else "keyword demo", "vectors": vectors,
                    "llm": "mock", "real_corpus_changed": False}), flush=True)
        finally:
            stop.write_text("stop\n")
            try:
                worker.wait(timeout=20)
            except subprocess.TimeoutExpired:
                worker.terminate()
                worker.wait(timeout=10)
        assert worker.returncode == 0, log.read_text(errors="replace")[-1000:]

if __name__ == "__main__":
    main()
