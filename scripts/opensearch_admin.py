"""Initialize, inspect, backfill or retry the configured OpenSearch index.

Run from the project root: python scripts/opensearch_admin.py --help
Backfill is explicit and retains existing corpus IDs, versions and SQL rows.
"""
import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.settings import get_settings
from database.db import get_conn
from retrieval.opensearch_store import OpenSearchError, OpenSearchStore, enqueue_backfill, outbox_status


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", help="SQLite metadata path; otherwise use configured database")
    parser.add_argument("--limit", type=int, default=100, help="documents per reconciliation batch")
    parser.add_argument("command", choices=("health", "init", "status", "reconcile", "backfill"))
    args = parser.parse_args(argv)
    conn = None
    try:
        get_settings()  # Load the project's .env before constructing the client.
        store = OpenSearchStore.from_env()
        if args.command == "health":
            result = store.health()
            code = 0 if result["healthy"] else 1
        elif args.command == "init":
            store.ensure_index()
            result, code = {"status": "READY", "index": store.index}, 0
        else:
            conn = get_conn(args.db)
            if args.command == "status":
                result = {**store.health(), "outbox": outbox_status(conn)}
                code = 0 if result["healthy"] else 1
            else:
                queued = enqueue_backfill(conn) if args.command == "backfill" else 0
                result = {"queued": queued, "synced": 0, "failed": 0, "pending": 0, "errors": []}
                # Reconcile one batch for routine retries; explicit backfill drains all
                # batches unless failures/another active consumer prevent progress.
                while True:
                    batch = store.reconcile(conn, args.limit)
                    for key in ("synced", "failed"):
                        result[key] += batch[key]
                    result["errors"].extend(batch["errors"])
                    result["pending"] = batch["pending"]
                    if args.command != "backfill" or batch["failed"] or not batch["pending"] or not batch["synced"]:
                        break
                result["status"] = "PENDING" if result["pending"] else "SYNCED"
                code = 1 if result["pending"] or result["failed"] else 0
        print(json.dumps(result, ensure_ascii=False))
        return code
    except (OpenSearchError, ValueError) as error:
        print(json.dumps({"status": "FAILED", "error": str(error)}, ensure_ascii=False))
        return 1
    finally:
        if conn:
            conn.close()


if __name__ == "__main__":
    raise SystemExit(main())