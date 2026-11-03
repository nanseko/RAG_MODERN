import pytest

from ingestion.convert import convert_with_soffice
from ingestion.parser import parse_file


def test_convert_missing_binary_raises_guidance(tmp_path):
    p = tmp_path / "a.doc"
    p.write_bytes(b"fake")
    with pytest.raises(ValueError, match="LibreOffice"):
        convert_with_soffice(str(p), binary="nonexistent-soffice-xyz")


def test_legacy_format_without_soffice_has_clear_error(tmp_path, monkeypatch):
    import ingestion.convert as convert
    monkeypatch.setattr(convert, "_binary", lambda *a, **k: None)
    p = tmp_path / "a.hwp"
    p.write_bytes(b"fake")
    with pytest.raises(ValueError, match="LibreOffice"):
        parse_file(str(p))
