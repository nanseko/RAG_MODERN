"""Windows OCR bundle safety and integrity tests."""
import importlib.util
import json
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("windows_ocr_prep", ROOT / "scripts/prepare_windows_ocr.py")

def module():
    assert SPEC and SPEC.loader and Path(SPEC.origin).is_file(), "Windows OCR preparation helper must exist"
    result = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(result)
    return result

def test_reject_main_environment(tmp_path):
    helper = module()
    with pytest.raises(ValueError, match="main"):
        helper.validate_environment(tmp_path / ".venv", tmp_path)

def test_reject_environment_outside_project(tmp_path):
    helper = module()
    with pytest.raises(ValueError, match="project"):
        helper.validate_environment(tmp_path.parent / "outside", tmp_path)

def test_accept_isolated_environment(tmp_path):
    assert module().validate_environment(tmp_path / ".venv-ocr", tmp_path) == tmp_path / ".venv-ocr"

def test_tampered_wheel_is_rejected(tmp_path):
    helper = module()
    (tmp_path / "a.whl").write_bytes(b"modified")
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": [{"path": "a.whl", "size": 8, "sha256": "0" * 64}]}))
    with pytest.raises(ValueError, match="checksum"):
        helper.verify_manifest(manifest, tmp_path)

def test_manifest_traversal_is_rejected(tmp_path):
    helper = module()
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"files": [{"path": "../secret", "size": 0, "sha256": "0" * 64}]}))
    with pytest.raises(ValueError, match="path"):
        helper.verify_manifest(manifest, tmp_path)

def test_empty_manifest_is_rejected(tmp_path):
    helper = module()
    manifest = tmp_path / "manifest.json"
    manifest.write_text('{"files": []}')
    with pytest.raises(ValueError, match="empty"):
        helper.verify_manifest(manifest, tmp_path)

def test_binary_download_checks_range_response(tmp_path):
    helper = module()
    class Reply:
        status_code = 200
        headers = {}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
    class Session:
        def get(self, *args, **kwargs): return Reply()
    with pytest.raises(ValueError, match="range"):
        helper.download_binary(Session(), "https://official.example/wheel", tmp_path / "wheel")

def test_binary_download_reassembles_checked_ranges(tmp_path):
    helper = module()
    content = b"official binary content"
    class Reply:
        status_code = 206
        def __init__(self, start, end):
            self.start, self.end = start, end
            self.headers = {"Content-Range": f"bytes {start}-{end}/{len(content)}"}
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_content(self, block): yield content[self.start:self.end + 1]
    class Session:
        def get(self, url, *, headers, **kwargs):
            start, end = map(int, headers["Range"].removeprefix("bytes=").split("-"))
            return Reply(start, min(end, len(content) - 1))
    path = tmp_path / "wheel"
    helper.download_binary(Session(), "https://official.example/wheel", path, chunk_size=5)
    assert path.read_bytes() == content
