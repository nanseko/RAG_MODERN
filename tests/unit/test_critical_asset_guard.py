from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from scripts.restore_critical_assets import (
    ASSETS,
    backup_assets,
    check_assets,
    restore_assets,
)


def make_project(tmp_path):
    root = tmp_path / "project"
    for relative in ASSETS:
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((relative + "\n").encode("utf8"))
    return root


def test_restore_only_missing_assets(tmp_path):
    root = make_project(tmp_path)
    archive = tmp_path / "critical-assets.zip"
    backup_assets(root=root, archive=archive)

    missing = root / ASSETS[0]
    expected = missing.read_bytes()
    missing.unlink()
    preserved = root / ASSETS[1]
    preserved.write_bytes(b"user edit")

    result = restore_assets(root=root, archive=archive, quiet=True)

    assert result["restored"] == [ASSETS[0]]
    assert missing.read_bytes() == expected
    assert preserved.read_bytes() == b"user edit"
    assert result["present"] == [ASSETS[1], *ASSETS[2:]]


def test_check_reports_missing_and_modified_assets(tmp_path):
    root = make_project(tmp_path)
    archive = tmp_path / "critical-assets.zip"
    backup_assets(root=root, archive=archive)

    (root / ASSETS[0]).unlink()
    (root / ASSETS[1]).write_bytes(b"changed")

    result = check_assets(root=root, archive=archive)

    assert result["ok"] is False
    assert result["missing"] == [ASSETS[0]]
    assert result["mismatch"] == [ASSETS[1]]


def test_backup_refuses_incomplete_source_tree(tmp_path):
    root = make_project(tmp_path)
    (root / ASSETS[0]).unlink()

    with pytest.raises(FileNotFoundError):
        backup_assets(root=root, archive=tmp_path / "critical-assets.zip")


def test_restore_rejects_paths_outside_project(tmp_path):
    root = make_project(tmp_path)
    root.mkdir(exist_ok=True)
    archive = tmp_path / "critical-assets.zip"
    manifest = '{"assets":[{"path":"../escape.txt","sha256":"' + "0" * 64 + '","size":1}]}'
    with ZipFile(archive, "w", ZIP_DEFLATED) as archive_file:
        archive_file.writestr("assets.json", manifest)
        archive_file.writestr("../escape.txt", b"x")

    with pytest.raises(ValueError):
        restore_assets(root=root, archive=archive, quiet=True)
    assert not (tmp_path / "escape.txt").exists()
