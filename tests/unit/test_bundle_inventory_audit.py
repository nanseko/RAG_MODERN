import json
from scripts import prepare_operating_bundle as bundle

def test_main_manifest_includes_final_ocr_wheels_and_excludes_image_build_cache(tmp_path, monkeypatch):
    gpu = tmp_path / "vendor/wheels-windows-ocr-cu128"
    gpu.mkdir(parents=True)
    (gpu / "runtime.whl").write_bytes(b"complete cuda wheel")
    services = tmp_path / "vendor/services"
    (services / "redis-blobs").mkdir(parents=True)
    (services / "opensearch.zip").write_bytes(b"native distribution")
    (services / "redis.tar").write_bytes(b"unused linux image")
    (services / "redis-blobs" / "internal.layer.tar").write_bytes(b"duplicate build cache")
    monkeypatch.setattr(bundle, "ROOT", tmp_path)
    bundle.manifest()
    paths = {r["path"] for r in json.loads((tmp_path / "vendor/offline-manifest.json").read_text())["files"]}
    assert "vendor/wheels-windows-ocr-cu128/runtime.whl" in paths
    assert "vendor/services/opensearch.zip" in paths
    assert "vendor/services/redis.tar" not in paths
    assert "vendor/services/redis-blobs/internal.layer.tar" not in paths
