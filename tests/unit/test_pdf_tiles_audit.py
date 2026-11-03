"""Tiled scans with a native page stamp must not index only that stamp."""
import fitz
from tests.unit.test_ocr_audit import image_bytes
from ingestion.parser import parse_file

def test_disjoint_scanned_tiles_trigger_ocr_with_native_page_stamp(tmp_path):
    path = tmp_path / "tiled.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=500, height=700)
        page.insert_text((10, 15), "p.1")
        for rect in (fitz.Rect(10, 30, 490, 330), fitz.Rect(10, 340, 490, 640)):
            page.insert_image(rect, stream=image_bytes(), keep_proportion=False)
        pdf.save(path)
    class OCR:
        def recognize(self, image):
            return "타일로 저장된 스캔 문서의 전체 시험 기록"
    sections = parse_file(path, ocr=OCR())
    assert sections[0]["extraction_method"] == "unlimited-ocr"
    assert "전체 시험 기록" in sections[0]["text"]

def test_overlapping_small_images_do_not_fake_full_scan_coverage(tmp_path):
    path = tmp_path / "overlapping.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=500, height=700)
        page.insert_text((10, 15), "p.1")
        rect = fitz.Rect(10, 30, 490, 250)
        for i in range(3):
            page.insert_image(rect, stream=image_bytes(), keep_proportion=False)
        pdf.save(path)
    class OCR:
        def recognize(self, image):
            raise AssertionError("Overlapping image area must be counted only once")
    sections = parse_file(path, ocr=OCR())
    assert sections[0]["extraction_method"] == "native"
