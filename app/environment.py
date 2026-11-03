"""Load only this project's .env; shell settings take precedence. Restart to apply edits."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_environment():
    from dotenv import load_dotenv
    path = Path(os.getenv("RAG_ENV_FILE", str(ROOT / ".env"))).expanduser()
    if not path.is_absolute():
        path = ROOT / path
    # Literal API credentials may contain dollar signs; never expand their contents.
    return load_dotenv(path, override=False, interpolate=False, encoding="utf-8-sig")
