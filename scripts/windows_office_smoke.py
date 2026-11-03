"""Synthetic DOC/PPT conversion roundtrip through the bundled Windows LibreOffice."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

def main():
    from docx import Document
    from pptx import Presentation
    from pptx.util import Inches
    from ingestion.convert import _binary
    from ingestion.parser import parse_file
    binary = _binary("soffice")
    if not binary:
        raise RuntimeError("Prepare the bundled LibreOffice runtime first")
    with tempfile.TemporaryDirectory(prefix="rag-windows-office-") as folder:
        root = Path(folder)
        original, legacy = root / "modern", root / "legacy"
        original.mkdir()
        legacy.mkdir()
        doc = Document()
        doc.add_paragraph("WINDOWS LEGACY DOCUMENT PRESSURE ZERO TEST")
        doc.save(original / "word.docx")
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_textbox(Inches(1), Inches(1), Inches(7), Inches(2)).text = "WINDOWS LEGACY PRESENTATION COOLING TEST"
        deck.save(original / "slides.pptx")
        for filename, filter_name in (("word.docx", "doc:MS Word 97"), ("slides.pptx", "ppt:MS PowerPoint 97")):
            profile = root / (filename + ".profile")
            with (root / (filename + ".log")).open("wb") as output:
                result = subprocess.run([binary, "-env:UserInstallation=" + profile.as_uri(),
                    "--headless", "--convert-to", filter_name, "--outdir", str(legacy), str(original / filename)],
                    stdout=output, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                    timeout=300, creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            if result.returncode:
                raise RuntimeError("Legacy conversion failed: " + filename)
        word = "\n".join(section["text"] for section in parse_file(legacy / "word.doc"))
        ppt = "\n".join(section["text"] for section in parse_file(legacy / "slides.ppt"))
        assert "WINDOWS LEGACY DOCUMENT PRESSURE ZERO TEST" in word, word
        assert "WINDOWS LEGACY PRESENTATION COOLING TEST" in ppt, ppt
        print(json.dumps({"status": "PASS", "formats": ["DOC", "PPT"], "converter": binary,
                          "synthetic_documents_only": True, "system_install": False}))

if __name__ == "__main__":
    main()
