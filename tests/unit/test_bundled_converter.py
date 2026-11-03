from pathlib import Path

def test_converter_discovers_project_runtime_without_global_install(tmp_path, monkeypatch):
    import ingestion.convert as convert
    monkeypatch.delenv("RAG_SOFFICE", raising=False)
    monkeypatch.setattr(convert, "ROOT", tmp_path, raising=False)
    monkeypatch.setattr(convert.shutil, "which", lambda *a, **kw: None)
    binary = tmp_path / "vendor/runtime/libreoffice/program/soffice.com"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"test executable")
    assert convert._binary("soffice") == str(binary)

def test_explicit_project_relative_converter_path_is_stable_across_cwd(tmp_path, monkeypatch):
    import ingestion.convert as convert
    monkeypatch.setattr(convert, "ROOT", tmp_path, raising=False)
    binary = tmp_path / "vendor/runtime/libreoffice/program/soffice.com"
    binary.parent.mkdir(parents=True)
    binary.write_bytes(b"test executable")
    monkeypatch.setenv("RAG_SOFFICE", "vendor/runtime/libreoffice/program/soffice.com")
    monkeypatch.setattr(convert.shutil, "which", lambda *a, **kw: None)
    assert convert._binary("soffice") == str(binary)
