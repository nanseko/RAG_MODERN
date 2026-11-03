"""Prepare, verify, and install the isolated native Windows OCR wheel bundle.

Online: --download. Offline: --verify-only or --install. Never modifies .venv.
"""
import argparse
import concurrent.futures
import hashlib
import json
import os
import re
import subprocess
import sys
import venv
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urljoin, urldefrag

ROOT = Path(__file__).resolve().parents[1]
WHEELS = ROOT / "vendor/wheels-windows-ocr-cu128"
MANIFEST = ROOT / "vendor/windows-ocr-manifest.json"
LOCK = ROOT / "configs/requirements-ocr-windows.lock.txt"
REQUIREMENTS = ROOT / "configs/requirements-ocr-windows.txt"
INDEX = "https://download.pytorch.org/whl/cu128/"

def checksum(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

def validate_environment(destination, root=ROOT):
    root, destination = Path(root).resolve(), Path(destination).resolve()
    main = root / ".venv"
    if destination == main or destination.is_relative_to(main) or main.is_relative_to(destination):
        raise ValueError("OCR environment must not collide with the main .venv")
    if not destination.is_relative_to(root) or destination == root:
        raise ValueError("OCR environment must be inside the project")
    if destination.name != ".venv-ocr":
        raise ValueError("OCR environment must use the dedicated project .venv-ocr directory")
    return destination

def verify_manifest(manifest=MANIFEST, wheel_dir=WHEELS):
    root = Path(wheel_dir).resolve()
    data = json.loads(Path(manifest).read_text(encoding="utf8"))
    files = data.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("empty Windows OCR wheel manifest")
    for item in files:
        path = (root / item["path"]).resolve()
        if not path.is_relative_to(root) or path == root:
            raise ValueError("invalid manifest path")
        if not path.is_file() or path.stat().st_size != item["size"]:
            raise ValueError("missing wheel or size mismatch: " + item["path"])
        if checksum(path) != item["sha256"]:
            raise ValueError("wheel checksum mismatch: " + item["path"])
    return {"verified_files": len(files), "bytes": sum(item["size"] for item in files)}

def download_binary(session, url, path, chunk_size=32 * 1024 * 1024):
    # Bounded HTTP ranges avoid large-response gateway stalls and duplicate files.
    with session.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=(15, 40)) as response:
        response.raise_for_status()
        matched = re.fullmatch(r"bytes 0-0/(\d+)", response.headers.get("Content-Range", ""))
        if response.status_code != 206 or not matched:
            raise ValueError("official server did not honor byte range")
        size = int(matched.group(1))
    with open(path, "wb") as output:
        output.truncate(size)
    def part(start):
        end = min(start + chunk_size, size) - 1
        for attempt in range(3):
            try:
                with session.get(url, headers={"Range": f"bytes={start}-{end}"}, stream=True,
                                 timeout=(15, 60)) as response:
                    response.raise_for_status()
                    if response.status_code != 206 or response.headers.get("Content-Range") != f"bytes {start}-{end}/{size}":
                        raise ValueError("official server returned an invalid byte range")
                    received = 0
                    with open(path, "r+b") as output:
                        output.seek(start)
                        for block in response.iter_content(1024 * 1024):
                            output.write(block)
                            received += len(block)
                    if received != end - start + 1:
                        raise ValueError("official server returned an incomplete byte range")
                return
            except Exception:
                if attempt == 2:
                    raise
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(part, range(0, size, chunk_size)))


def download():
    import truststore
    truststore.inject_into_ssl()
    import requests
    from packaging.utils import parse_wheel_filename
    WHEELS.mkdir(parents=True, exist_ok=True)
    source = {}
    for project, version in (("torch", "2.10.0"), ("torchvision", "0.25.0")):
        index_url = INDEX + project + "/"
        response = requests.get(index_url, timeout=60)
        response.raise_for_status()
        name = f"{project}-{version}+cu128-cp312-cp312-win_amd64.whl"
        links = [urljoin(index_url, link) for link in re.findall(r'href="([^"]+)"', response.text)
                 if unquote(urldefrag(link)[0]).endswith("/" + name)]
        if len(links) != 1:
            raise ValueError("official Windows wheel unavailable: " + name)
        url, fragment = urldefrag(links[0])
        expected = fragment.removeprefix("sha256=")
        if len(expected) != 64:
            raise ValueError("official wheel has no SHA256")
        source[name] = {"url": url, "upstream_sha256": expected}
        path = WHEELS / name
        if not path.is_file() or checksum(path) != expected:
            print("Downloading " + name, flush=True)
            temporary = path.with_suffix(".partial")
            download_binary(requests, url, temporary)
            if checksum(temporary) != expected:
                raise ValueError("official download checksum mismatch: " + name)
            temporary.replace(path)
    subprocess.run([sys.executable, "-m", "pip", "download", "--disable-pip-version-check", "--no-cache-dir",
        "--only-binary=:all:", "--platform", "win_amd64", "--python-version", "312", "--implementation", "cp",
        "--abi", "cp312", "--index-url", "https://pypi.org/simple", "--find-links", str(WHEELS),
        "--dest", str(WHEELS), "-r", str(REQUIREMENTS)], check=True)
    def metadata(path):
        name, version, _, tags = parse_wheel_filename(path.name)
        if any(tag.platform not in ("any", "win_amd64") for tag in tags):
            raise ValueError("non-Windows wheel found: " + path.name)
        if path.name in source:
            return path.name, source[path.name]
        response = requests.get(f"https://pypi.org/pypi/{name}/{version}/json", timeout=(15, 40))
        response.raise_for_status()
        entries = [entry for entry in response.json()["urls"] if entry["filename"] == path.name]
        if len(entries) != 1:
            raise ValueError("wheel not found on PyPI: " + path.name)
        return path.name, {"url": entries[0]["url"], "upstream_sha256": entries[0]["digests"]["sha256"]}
    paths = sorted(WHEELS.glob("*.whl"))
    with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
        source.update(dict(pool.map(metadata, paths)))
    files, locks = [], []
    for path in paths:
        name, version, _, tags = parse_wheel_filename(path.name)
        actual = checksum(path)
        if actual != source[path.name]["upstream_sha256"]:
            raise ValueError("upstream checksum mismatch: " + path.name)
        files.append({"path": path.name, "size": path.stat().st_size, "sha256": actual, **source[path.name]})
        locks.append(f"{name}=={version} --hash=sha256:{actual}")
    LOCK.write_text("# Windows CPython 3.12 x64; install only into .venv-ocr\n" + "\n".join(locks) + "\n", encoding="utf8")
    data = {"platform": "win_amd64", "python": "cp312", "cuda": "12.8", "torch": "2.10.0+cu128",
        "torchvision": "0.25.0+cu128", "transformers": "4.57.1", "model_revision": "07dea832e22aefee32ad281d4b80551282e1c168",
        "prepared_utc": datetime.now(timezone.utc).isoformat(), "files": files,
        "total_bytes": sum(item["size"] for item in files)}
    MANIFEST.write_text(json.dumps(data, indent=2) + "\n", encoding="utf8")
    return verify_manifest()

def install(destination):
    if os.name != "nt" or sys.version_info[:2] != (3, 12) or sys.maxsize <= 2**32:
        raise ValueError("Use native Windows CPython 3.12 x64")
    destination = validate_environment(destination)
    verify_manifest()
    venv.EnvBuilder(with_pip=True).create(destination)
    python = destination / "Scripts/python.exe"
    subprocess.run([str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-cache-dir",
        "--no-index", "--find-links", str(WHEELS), "--require-hashes", "-r", str(LOCK)], check=True)
    subprocess.run([str(python), "-m", "pip", "check"], check=True)
    return {"environment": str(destination), "offline_install": "passed"}

def check_runtime():
    import torch
    import torchvision
    import transformers
    import matplotlib, einops, addict, easydict, fitz, psutil, requests, httpx, fastapi, uvicorn, dotenv
    sys.path.insert(0, str(ROOT))
    from ingestion.ocr import validate_snapshot
    directory = validate_snapshot(ROOT / "models/unlimited-ocr")
    if torch.__version__ != "2.10.0+cu128" or torchvision.__version__ != "0.25.0+cu128" or transformers.__version__ != "4.57.1":
        raise ValueError("OCR runtime version mismatch")
    # get_arch_list() returns [] without a CUDA device, although the wheel contains CUDA binaries.
    binary_arches = torch._C._cuda_getArchFlags().split()
    if "sm_120" not in binary_arches:
        raise ValueError("CUDA wheel lacks RTX 5080 sm_120")
    return {"torch": torch.__version__, "torchvision": torchvision.__version__, "transformers": transformers.__version__,
        "cuda_runtime": torch.version.cuda, "binary_architectures": binary_arches,
        "cuda_available": torch.cuda.is_available(), "snapshot": str(directory), "gpu_inference_tested": False}

def serve():
    sys.path.insert(0, str(ROOT))
    from app.environment import load_environment
    load_environment()
    from scripts.run_ocr_server import main
    sys.argv = [sys.argv[0], "--model-dir", os.getenv("RAG_OCR_MODEL_DIR", str(ROOT / "models/unlimited-ocr")),
        "--host", "127.0.0.1", "--port", "10000"]
    main()

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--download", action="store_true")
    group.add_argument("--verify-only", action="store_true")
    group.add_argument("--install", action="store_true")
    group.add_argument("--check-runtime", action="store_true")
    group.add_argument("--serve", action="store_true")
    parser.add_argument("--venv", default=str(ROOT / ".venv-ocr"))
    args = parser.parse_args()
    if args.download:
        result = download()
    elif args.install:
        result = install(args.venv)
    elif args.check_runtime:
        result = check_runtime()
    elif args.serve:
        serve()
        return
    else:
        result = verify_manifest()
    print(json.dumps(result, indent=2))

if __name__ == "__main__":
    main()
