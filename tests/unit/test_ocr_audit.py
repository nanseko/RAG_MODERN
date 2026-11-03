"""OCR boundary audit with synthetic pages and no external document transfer."""
import io
import json

import fitz
import httpx
import pytest
from PIL import Image, ImageDraw

from ingestion.ocr import OCRFailure, UnlimitedOCR
from ingestion.parser import parse_file

def image_bytes():
    image = Image.new("RGB", (160, 100), "white")
    ImageDraw.Draw(image).text((10, 10), "pressure 0 bar", fill="black")
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()

def test_image_uses_internal_ocr_protocol_and_preserves_page(tmp_path):
    path = tmp_path / "scan.png"
    path.write_bytes(image_bytes())
    def server(request):
        payload = json.loads(request.content)
        assert request.url == "http://127.0.0.1:10000/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer local"
        assert payload["messages"][0]["content"][0]["text"] == "<image>document parsing."
        assert payload["messages"][0]["content"][1]["image_url"]["url"].startswith("data:image/png;base64,")
        return httpx.Response(200, json={"choices": [{"message": {"content": "압력 0 bar 기록"},
                                                    "finish_reason": "stop"}]})
    ocr = UnlimitedOCR(mode="api", api_url="http://127.0.0.1:10000/v1", api_key="local",
                       transport=httpx.MockTransport(server))
    sections = parse_file(path, ocr=ocr)
    assert len(sections) == 1
    assert sections[0]["text"] == "압력 0 bar 기록"
    assert sections[0]["page_number"] == 1
    assert sections[0]["extraction_method"] == "unlimited-ocr"

@pytest.mark.parametrize("kind", ["empty", "truncated", "malformed", "redirect"])
def test_partial_or_bad_ocr_is_rejected(kind):
    def server(request):
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "https://example.com"})
        if kind == "malformed":
            return httpx.Response(200, json={"choices": []})
        return httpx.Response(200, json={"choices": [{"message": {"content": "" if kind == "empty" else "partial"},
                                                    "finish_reason": "length" if kind == "truncated" else "stop"}]})
    ocr = UnlimitedOCR(mode="api", api_url="http://127.0.0.1:10000/v1",
                       transport=httpx.MockTransport(server))
    with pytest.raises(OCRFailure):
        ocr.recognize(image_bytes())

def test_mixed_pdf_keeps_native_text_and_ocr_page_provenance(tmp_path):
    path = tmp_path / "mixed.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page()
        page.insert_text((50, 50), "Native pressure test report and cooling verification.")
        scanned = pdf.new_page()
        scanned.insert_image(scanned.rect, stream=image_bytes())
        pdf.save(path)
    class OCR:
        def recognize(self, png):
            assert png.startswith(b"\x89PNG")
            return "스캔 페이지 압력 시험 결과"
    sections = parse_file(path, ocr=OCR())
    assert sections[0]["text"] == "Native pressure test report and cooling verification."
    assert sections[0]["extraction_method"] == "native"
    assert sections[1]["text"] == "스캔 페이지 압력 시험 결과"
    assert sections[1]["page_number"] == 2
    assert sections[1]["extraction_method"] == "unlimited-ocr"

def test_scanned_image_without_config_fails_instead_of_empty_success(tmp_path):
    path = tmp_path / "scan.png"
    path.write_bytes(image_bytes())
    with pytest.raises(OCRFailure, match="disabled"):
        parse_file(path)
