"""Back up and restore the runtime assets that are removed on reboot."""
import argparse
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE = ROOT / "vendor/critical-assets.zip"
MANIFEST = "assets.json"
ASSETS = (
    "configs/requirements-ocr-cuda.lock.txt",
    "configs/requirements-ocr-windows.lock.txt",
    "configs/requirements-ocr-windows.txt",
    "rag/prompts/review.txt",
    "rag/prompts/system.txt",
    ".venv/Lib/site-packages/docx/templates/default.docx",
    ".venv/Lib/site-packages/pptx/templates/default.pptx",
)


def _sha256(data):
    return hashlib.sha256(data).hexdigest()


def _relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise ValueError(f"invalid asset path: {value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"invalid asset path: {value!r}")
    return path


def _target_for(root, relative):
    root = Path(root).resolve()
    target = root.joinpath(*relative.parts).resolve()
    if target == root or not target.is_relative_to(root):
        raise ValueError(f"asset escapes project root: {relative}")
    return target


def _atomic_write(target, data):
    target.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _load_manifest(archive_file):
    try:
        payload = json.loads(archive_file.read(MANIFEST))
    except KeyError as exc:
        raise ValueError("archive has no manifest") from exc
    records = payload.get("assets") if isinstance(payload, dict) else None
    if not isinstance(records, list) or not records:
        raise ValueError("archive manifest has no assets")
    result = {}
    for record in records:
        if not isinstance(record, dict):
            raise ValueError("archive manifest entry is invalid")
        relative = _relative_path(record.get("path"))
        digest = record.get("sha256")
        size = record.get("size")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"invalid checksum for {relative}")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise ValueError(f"invalid size for {relative}")
        key = relative.as_posix()
        if key in result:
            raise ValueError(f"duplicate asset in archive: {key}")
        result[key] = (relative, digest, size)
    return result


def backup_assets(root=ROOT, archive=ARCHIVE):
    root = Path(root).resolve()
    archive = Path(archive).resolve()
    records = []
    payloads = []
    for relative_text in ASSETS:
        relative = _relative_path(relative_text)
        source = _target_for(root, relative)
        if not source.is_file():
            raise FileNotFoundError(f"missing critical asset: {relative_text}")
        data = source.read_bytes()
        records.append({"path": relative.as_posix(), "sha256": _sha256(data), "size": len(data)})
        payloads.append((relative.as_posix(), data))
    archive.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = tempfile.mkstemp(prefix=archive.name + ".", dir=archive.parent)
    os.close(handle)
    try:
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED) as archive_file:
            archive_file.writestr(MANIFEST, json.dumps({"assets": records}, ensure_ascii=False))
            for name, data in payloads:
                archive_file.writestr(name, data)
        os.replace(temporary, archive)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return {"archive": str(archive), "assets": len(records)}


def restore_assets(root=ROOT, archive=ARCHIVE, quiet=False):
    root = Path(root).resolve()
    archive = Path(archive).resolve()
    restored = []
    present = []
    with zipfile.ZipFile(archive) as archive_file:
        for key, (relative, digest, size) in _load_manifest(archive_file).items():
            target = _target_for(root, relative)
            if target.exists():
                if not target.is_file():
                    raise ValueError(f"asset target is not a file: {key}")
                if target.stat().st_size > 0:
                    present.append(key)
                    continue
            try:
                data = archive_file.read(key)
            except KeyError as exc:
                raise ValueError(f"archive is missing asset: {key}") from exc
            if len(data) != size or _sha256(data) != digest:
                raise ValueError(f"archive asset is corrupt: {key}")
            _atomic_write(target, data)
            restored.append(key)
    if not quiet:
        for key in restored:
            print(f"restored {key}")
        print(f"critical assets: {len(restored)} restored, {len(present)} already present")
    return {"restored": restored, "present": present}


def check_assets(root=ROOT, archive=ARCHIVE):
    root = Path(root).resolve()
    archive = Path(archive).resolve()
    missing = []
    mismatch = []
    with zipfile.ZipFile(archive) as archive_file:
        records = _load_manifest(archive_file)
    for key, (relative, digest, size) in records.items():
        target = _target_for(root, relative)
        if not target.is_file():
            missing.append(key)
            continue
        data = target.read_bytes()
        if len(data) != size or _sha256(data) != digest:
            mismatch.append(key)
    return {
        "ok": not missing and not mismatch,
        "checked": len(records),
        "missing": missing,
        "mismatch": mismatch,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backup", action="store_true", help="rebuild the recovery archive")
    parser.add_argument("--check", action="store_true", help="verify archive and installed assets")
    parser.add_argument("--quiet", action="store_true", help="suppress normal restore output")
    args = parser.parse_args()
    try:
        if args.backup:
            print(json.dumps(backup_assets(), ensure_ascii=False))
            return 0
        if args.check:
            result = check_assets()
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["ok"] else 1
        restore_assets(quiet=args.quiet)
        return 0
    except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
        print(f"critical asset guard: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
