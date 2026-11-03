"""Export pinned public Linux/amd64 service images to docker-load archives, no daemon needed."""
import gzip
import hashlib
import io
import json
import shutil
import tarfile
from pathlib import Path

import requests
import truststore

truststore.inject_into_ssl()
ROOT = Path(__file__).resolve().parents[1]
ACCEPT = ", ".join(["application/vnd.oci.image.index.v1+json",
                   "application/vnd.docker.distribution.manifest.list.v2+json",
                   "application/vnd.oci.image.manifest.v1+json",
                   "application/vnd.docker.distribution.manifest.v2+json"])
IMAGES = [("library/redis", "8-alpine"), ("library/postgres", "18.6-alpine"),
          ("opensearchproject/opensearch", "3.9.0")]


def digest_file(path):
    h = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(4 * 1024 * 1024), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def export(repo, tag):
    image = repo.removeprefix("library/") + ":" + tag
    label = repo.replace("/", "-") + "-" + tag
    target = ROOT / "vendor/services" / (label + "-linux-amd64.tar")
    source_record = target.with_suffix(".source.json")
    if target.is_file() and source_record.is_file():
        print("Already exported:", image, flush=True)
        return
    auth = requests.get("https://auth.docker.io/token",
        params={"service": "registry.docker.io", "scope": f"repository:{repo}:pull"}, timeout=60)
    auth.raise_for_status()
    session = requests.Session()
    session.headers.update({"Authorization": "Bearer " + auth.json()["token"], "Accept": ACCEPT})
    base = "https://registry-1.docker.io/v2/" + repo

    def get_manifest(ref):
        response = session.get(base + "/manifests/" + ref, timeout=60)
        response.raise_for_status()
        return response, response.json()

    response, manifest = get_manifest(tag)
    if "manifests" in manifest:
        match = next((m for m in manifest["manifests"] if
            m.get("platform", {}).get("os") == "linux" and
            m.get("platform", {}).get("architecture") == "amd64"), None)
        if not match:
            raise RuntimeError("No Linux/amd64 manifest: " + image)
        response, manifest = get_manifest(match["digest"])
    pinned = response.headers.get("Docker-Content-Digest", "sha256:" + hashlib.sha256(response.content).hexdigest())
    if "sha256:" + hashlib.sha256(response.content).hexdigest() != pinned:
        raise RuntimeError("Manifest hash mismatch")
    work = target.parent / (label + "-blobs")
    work.mkdir(parents=True, exist_ok=True)

    def blob(digest):
        path = work / digest.split(":")[1]
        if path.exists() and digest_file(path) == digest:
            return path
        partial = path.with_suffix(".part")
        with session.get(base + "/blobs/" + digest, stream=True, timeout=(20, 90)) as response:
            response.raise_for_status()
            with partial.open("wb") as out:
                for block in response.iter_content(4 * 1024 * 1024):
                    out.write(block)
        if digest_file(partial) != digest:
            raise RuntimeError("Image blob hash mismatch")
        partial.replace(path)
        print(image, "blob verified", digest[:22], flush=True)
        return path

    config = blob(manifest["config"]["digest"])
    config_name = config.name + ".json"
    layers = []
    for item in manifest["layers"]:
        path = blob(item["digest"])
        layer = work / (path.name + "-layer.tar")
        if not layer.exists():
            with path.open("rb") as source:
                is_gzip = source.read(2) == b"\x1f\x8b"
            if is_gzip:
                with gzip.open(path, "rb") as source, layer.open("wb") as out:
                    shutil.copyfileobj(source, out, 4 * 1024 * 1024)
            else:
                shutil.copyfile(path, layer)
        layers.append(layer)
    partial = target.with_suffix(".tar.part")
    with tarfile.open(partial, "w") as archive:
        archive.add(config, arcname=config_name, recursive=False)
        layer_names = []
        for layer in layers:
            name = layer.stem.removesuffix("-layer") + "/layer.tar"
            layer_names.append(name)
            archive.add(layer, arcname=name, recursive=False)
        docker_manifest = [{"Config": config_name, "RepoTags": [image], "Layers": layer_names}]
        raw = json.dumps(docker_manifest).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size = len(raw)
        archive.addfile(info, io.BytesIO(raw))
    partial.replace(target)
    source_record.write_text(json.dumps({"image": image, "registry_manifest": pinned,
        "platform": "linux/amd64", "bytes": target.stat().st_size,
        "sha256": digest_file(target).split(":")[1]}, indent=2), encoding="utf8")
    session.close()
    print("DOCKER ARCHIVE READY:", image, target.stat().st_size, flush=True)


if __name__ == "__main__":
    for image in IMAGES:
        export(*image)
