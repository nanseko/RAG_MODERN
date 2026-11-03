"""Shared local settings. Environment variables override configs/app.yaml."""
import os
from dataclasses import dataclass
from pathlib import Path

import yaml

from app.environment import load_environment

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Settings:
    db: str
    docs: str
    md: str
    originals: str
    target_tokens: int = 650
    max_tokens: int = 1000
    candidates: int = 50
    top_k: int = 8
    target_min: int = 3
    target_max: int = 5
    max_cycles: int = 3
    context_tokens: int = 6000
    scan_interval_min: float = 30
    autoscan: bool = False


def project_path(value):
    p = Path(value).expanduser()
    return str(p if p.is_absolute() else ROOT / p)


def get_settings():
    load_environment()
    config_path = project_path(os.getenv("RAG_CONFIG_PATH", "configs/app.yaml"))
    with open(config_path, encoding="utf8") as f:
        config = yaml.safe_load(f) or {}
    paths = config.get("paths", {})
    chunk = config.get("chunking", {})
    retrieval = config.get("retrieval", {})
    ingest = config.get("ingestion", {})
    s = Settings(
        db=project_path(os.getenv("RAG_DB_PATH", paths.get("db", "data/rag.sqlite3"))),
        docs=project_path(os.getenv("RAG_DOCS_DIR", paths.get("docs", "data/docs"))),
        md=project_path(os.getenv("RAG_MD_DIR", paths.get("md", "data/md"))),
        originals=project_path(os.getenv("RAG_ORIGINALS_DIR", paths.get("originals", "data/originals"))),
        target_tokens=int(chunk.get("target_tokens", 650)),
        max_tokens=int(chunk.get("max_tokens", 1000)),
        candidates=int(retrieval.get("candidates", 50)),
        top_k=int(retrieval.get("rerank_top_n", 8)),
        target_min=int(retrieval.get("target_min", 3)),
        target_max=int(retrieval.get("target_max", 5)),
        max_cycles=int(retrieval.get("max_cycles", 3)),
        context_tokens=int(retrieval.get("context_tokens", 6000)),
        scan_interval_min=float(os.getenv("RAG_SCAN_INTERVAL_MIN", ingest.get("interval_min", 30))),
        autoscan=os.getenv("RAG_AUTOSCAN", str(int(ingest.get("autoscan", False)))) == "1",
    )
    if not (1 <= s.target_min <= s.target_max <= 5 and 1 <= s.max_cycles <= 3):
        raise ValueError("retrieval targets/cycles are invalid")
    if not (0 < s.target_tokens <= s.max_tokens and s.candidates > 0 and s.top_k > 0):
        raise ValueError("chunk/retrieval limits must be positive")
    if s.scan_interval_min < 0 or s.context_tokens < 1:
        raise ValueError("scan interval/context budget is invalid")
    return s
