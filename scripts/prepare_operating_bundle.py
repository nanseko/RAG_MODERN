"""Download official native service archives and hash internal-network artifacts."""
import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path

import requests
import truststore

truststore.inject_into_ssl()
ROOT = Path(__file__).resolve().parents[1]
ARCHIVES = [
    ("opensearch-3.9.0-windows-x64.zip",
     "https://artifacts.opensearch.org/releases/bundle/opensearch/3.9.0/opensearch-3.9.0-windows-x64.zip",
     "https://artifacts.opensearch.org/releases/bundle/opensearch/3.9.0/opensearch-3.9.0-windows-x64.zip.sha512"),
    ("postgresql-18.6-1-windows-x64-binaries.zip",
     "https://get.enterprisedb.com/postgresql/postgresql-18.6-1-windows-x64-binaries.zip", None),
]


def file_hash(path, algorithm="sha256"):
    h = hashlib.new(algorithm)
    with path.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def download(name, url, checksum_url):
    target = ROOT / "vendor/services" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    expected = None
    if checksum_url:
        r = requests.get(checksum_url, timeout=60)
        r.raise_for_status()
        expected = re.search(r"\b[0-9a-fA-F]{128}\b", r.text)
        if not expected:
            raise RuntimeError("Official checksum missing: " + name)
        expected = expected[0].lower()
    if target.exists() and (expected is None or file_hash(target, "sha512") == expected):
        print("Already downloaded:", name, flush=True)
    else:
        partial = target.with_suffix(target.suffix + ".part")
        for attempt in range(3):
            offset = partial.stat().st_size if partial.exists() else 0
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            try:
                with requests.get(url, stream=True, headers=headers, timeout=(20, 60)) as r:
                    r.raise_for_status()
                    append = offset > 0 and r.status_code == 206
                    if not append:
                        offset = 0
                    total = int(r.headers.get("Content-Length", 0)) + offset
                    print(f"Downloading {name}: {total:,} bytes, resume {offset:,}", flush=True)
                    next_log = time.monotonic() + 25
                    with partial.open("ab" if append else "wb") as out:
                        for block in r.iter_content(4 * 1024 * 1024):
                            out.write(block)
                            if time.monotonic() >= next_log:
                                print(f"{name}: {partial.stat().st_size:,}/{total:,}", flush=True)
                                next_log = time.monotonic() + 25
                if total and partial.stat().st_size != total:
                    raise RuntimeError("Download size mismatch: " + name)
                if expected and file_hash(partial, "sha512") != expected:
                    raise RuntimeError("Official SHA512 mismatch: " + name)
                partial.replace(target)
                break
            except requests.RequestException:
                if attempt == 2:
                    raise
        print("Downloaded:", name, flush=True)
    return {"path": str(target.relative_to(ROOT)).replace("\\", "/"), "source": url,
            "bytes": target.stat().st_size, "sha256": file_hash(target),
            "official_sha512": expected}


def manifest():
    records = []
    for directory in ("vendor/wheels", "vendor/wheels-windows-ocr-cu128", "vendor/services", "vendor/runtime/libreoffice", "models"):
        base = ROOT / directory
        if not base.exists():
            continue
        for path in sorted(base.rglob("*")):
            if (not path.is_file() or ".cache" in path.parts or "__pycache__" in path.parts
                or any(part.endswith("-blobs") for part in path.parts)
                or (directory == "vendor/services" and (path.parent != base or not path.name.endswith((".zip", ".msi", ".source.json"))))
                or path.name.endswith((".part", ".pyc"))
                or (path.parent == ROOT / "models" and path.suffix == ".py")):
                continue
            records.append({"path": str(path.relative_to(ROOT)).replace("\\", "/"),
                            "bytes": path.stat().st_size, "sha256": file_hash(path)})
    out = ROOT / "vendor/offline-manifest.json"
    out.write_text(json.dumps({"format": 1, "files": records}, indent=2), encoding="utf8")
    print(f"Manifest: {len(records)} files, {sum(x['bytes'] for x in records):,} bytes", flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--archives", action="store_true")
    p.add_argument("--manifest", action="store_true")
    args = p.parse_args()
    if args.archives:
        entries = [download(*record) for record in ARCHIVES]
        (ROOT / "vendor/services/sources.json").write_text(json.dumps(entries, indent=2), encoding="utf8")
    if args.manifest:
        manifest()
