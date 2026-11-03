"""One folder scan, with the same paths/configuration as the API."""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ingestion.pipeline import scan_once

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("docs_dir", nargs="?")
    parser.add_argument("--db")
    parser.add_argument("--md-dir")
    args = parser.parse_args()
    out = scan_once(args.docs_dir, args.db, args.md_dir)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    raise SystemExit(1 if out.get("failed") else 0)
