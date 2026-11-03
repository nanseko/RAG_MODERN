"""Online preparation only: download the official pinned OCR snapshot using hf.

Runtime inference never calls this script. --verify-only needs no network or Hub library.
"""
import argparse
import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPO_ID = "baidu/Unlimited-OCR"
REVISION = "07dea832e22aefee32ad281d4b80551282e1c168"


def checksum(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def verify_manifest(destination):
    root = Path(destination).resolve()
    manifest = json.loads((root / "offline-manifest.json").read_text(encoding="utf8"))
    if manifest.get("repo_id") != REPO_ID or manifest.get("revision") != REVISION:
        raise ValueError("OCR model manifest revision mismatch")
    files = manifest.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("OCR model manifest has no files")
    for entry in files:
        path = (root / entry["path"]).resolve()
        if not path.is_relative_to(root) or path == root:
            raise ValueError("invalid manifest file path")
        if not path.is_file() or path.stat().st_size != entry["size"]:
            raise ValueError("missing model file or size mismatch: " + entry["path"])
        if checksum(path) != entry["sha256"]:
            raise ValueError("model checksum mismatch: " + entry["path"])
    return {"verified_files": len(files), "bytes": sum(file["size"] for file in files),
            "revision": REVISION}


def prepare_manifest(destination):
    import truststore
    truststore.inject_into_ssl()
    from huggingface_hub import HfApi
    root = Path(destination).resolve()
    info = HfApi(token=False).model_info(REPO_ID, revision=REVISION, files_metadata=True)
    files = []
    for sibling in info.siblings:
        path = root / sibling.rfilename
        if not path.is_file() or path.stat().st_size != sibling.size:
            raise ValueError("incomplete official snapshot: " + sibling.rfilename)
        actual = checksum(path)
        if sibling.lfs and actual != sibling.lfs.sha256:
            raise ValueError("official LFS checksum mismatch: " + sibling.rfilename)
        files.append({"path": sibling.rfilename, "size": path.stat().st_size,
                      "sha256": actual, "upstream_lfs_sha256": sibling.lfs.sha256 if sibling.lfs else None})
    manifest = {"repo_id": REPO_ID, "revision": REVISION,
        "source_url": "https://huggingface.co/" + REPO_ID + "/tree/" + REVISION,
        "prepared_utc": datetime.now(timezone.utc).isoformat(),
        "files": files, "total_bytes": sum(file["size"] for file in files)}
    (root / "offline-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf8")
    return verify_manifest(root)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", default=str(ROOT / "models/unlimited-ocr"))
    parser.add_argument("--verify-only", action="store_true")
    parser.add_argument("--manifest-only", action="store_true", help="Hash a completed pinned snapshot without downloading it again")
    args = parser.parse_args()
    if args.verify_only:
        result = verify_manifest(args.destination)
    else:
        # This online-only CLI prepares metadata in this process as well as downloading
        # in a subprocess. Updating a copied environment does not remove existing keys.
        env = os.environ.copy()
        env.pop("HF_HUB_OFFLINE", None)
        env.pop("TRANSFORMERS_OFFLINE", None)
        env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "1"
        env["HF_HOME"] = str(Path(args.destination).resolve() / ".cache/hf-home")
        env["HF_XET_CACHE"] = str(Path(args.destination).resolve() / ".cache/xet")
        os.environ.pop("HF_HUB_OFFLINE", None)
        os.environ.pop("TRANSFORMERS_OFFLINE", None)
        os.environ.update(env)
        if not args.manifest_only:
            wrapper = "import truststore; truststore.inject_into_ssl(); from huggingface_hub.cli.hf import main; main()"
            subprocess.run([sys.executable, "-c", wrapper, "download", REPO_ID,
                "--revision", REVISION, "--local-dir", args.destination, "--max-workers", "4"],
                check=True, env=env)
        result = prepare_manifest(args.destination)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
