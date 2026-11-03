"""Verify each bundled artifact without contacting the internet."""
import hashlib
import json
import argparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def verify_manifest(manifest_path, root=ROOT):
    root = Path(root).resolve()
    entries = json.loads(Path(manifest_path).read_text(encoding="utf8")).get("files")
    if not isinstance(entries, list) or not entries:
        raise ValueError("offline manifest has no files")
    failures = []
    for record in entries:
        path = (root / record["path"]).resolve()
        if not path.is_relative_to(root) or path == root:
            failures.append(record["path"] + ": invalid path")
            continue
        if not path.is_file() or path.stat().st_size != record["bytes"]:
            failures.append(record["path"] + ": missing/size")
            continue
        h = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
                h.update(block)
        if h.hexdigest() != record["sha256"]:
            failures.append(record["path"] + ": hash")
    if failures:
        raise ValueError("\n".join(failures))
    return {"verified_files": len(entries), "bytes": sum(entry["bytes"] for entry in entries)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(ROOT / "vendor/offline-manifest.json"))
    args = parser.parse_args()
    try:
        result = verify_manifest(args.manifest, ROOT)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise SystemExit(str(exc)) from exc
    print(json.dumps(result))


if __name__ == "__main__":
    main()
