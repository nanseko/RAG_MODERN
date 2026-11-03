import hashlib
import json
import sys

def test_default_verifier_checks_windows_distribution_not_archived_linux_bundle(tmp_path, monkeypatch, capsys):
    from scripts import verify_offline_bundle as verifier
    artifact = tmp_path / "runtime.whl"
    artifact.write_bytes(b"windows")
    vendor = tmp_path / "vendor"
    vendor.mkdir()
    (vendor / "offline-manifest.json").write_text(json.dumps({"files": [{
        "path": "runtime.whl", "bytes": 7, "sha256": hashlib.sha256(b"windows").hexdigest()}]}))
    (vendor / "ocr-offline-manifest.json").write_text('{"files":[]}')
    monkeypatch.setattr(verifier, "ROOT", tmp_path)
    monkeypatch.setattr(sys, "argv", ["verify_offline_bundle.py"])
    verifier.main()
    assert json.loads(capsys.readouterr().out)["verified_files"] == 1
