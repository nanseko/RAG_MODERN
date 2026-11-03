"""MSI extraction must confine target paths and never install system components."""
import pytest

def test_directory_map_anchors_product_and_preserves_long_names():
    from scripts.prepare_libreoffice import product_directory
    rows = {"TARGETDIR": ("", "SourceDir"), "INSTALLLOCATION": ("TARGETDIR", "LibreOffice"),
            "program": ("INSTALLLOCATION", "program"),
            "resources": ("program", "RESOUR~1|resource files"),
            "system": ("TARGETDIR", "System32")}
    assert product_directory("resources", rows).as_posix() == "program/resource files"
    assert product_directory("system", rows) is None

@pytest.mark.parametrize("name", ["../escape", "..", r"a\..\b", "a/b"])
def test_unsafe_msi_directory_names_are_rejected(name):
    from scripts.prepare_libreoffice import product_directory
    with pytest.raises(ValueError):
        product_directory("bad", {"INSTALLLOCATION": ("", "."), "bad": ("INSTALLLOCATION", name)})
