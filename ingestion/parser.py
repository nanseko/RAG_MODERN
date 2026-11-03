"""Native text/table extraction; legacy office uses an installed converter."""
import os
import re
from html.parser import HTMLParser
from pathlib import Path


def _section(path, text, **metadata):
    return {"title": Path(path).name, "heading_path": "", "page_number": None,
            "text": text, **metadata}


def _table(rows):
    rows = [[str("" if v is None else v).replace("|", "\\|").replace("\n", "<br>") for v in row] for row in rows]
    if not rows:
        return ""
    width = max(len(r) for r in rows)
    rows = [r + [""] * (width - len(r)) for r in rows]
    lines = ["| " + " | ".join(r) + " |" for r in rows]
    lines.insert(1, "| " + " | ".join(["---"] * width) + " |")
    return "\n".join(lines)


class _HTMLText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "div", "br", "tr", "h1", "h2", "h3"):
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _markdown_sections(path, text):
    """마크다운 제목으로 분할. 제목 계층은 누적 경로로 만들어 원인-조치 쌍이 헤딩 아래 묶인다."""
    sections, parts, stack = [], [], []

    def flush():
        body = "\n".join(parts).strip()
        if body:
            sections.append(_section(path, body, heading_path=" / ".join(stack)))
        parts.clear()

    for line in text.splitlines():
        m = re.match(r"^\s*(#{1,6})\s+(.*\S)\s*$", line)
        if m:
            flush()
            stack[:] = stack[:len(m.group(1)) - 1]
            stack.append(m.group(2).strip())
        else:
            parts.append(line)
    flush()
    return sections or [_section(path, text)]


def parse_file(path, *, ocr=None):
    ext = Path(path).suffix.lower()
    if ext in (".hwp", ".doc", ".ppt") and Path(path).stat().st_size == 0:
        raise ValueError("빈 문서 파일입니다")
    if ext in (".txt", ".md", ".markdown", ".html", ".htm"):
        raw = Path(path).read_bytes()
        for encoding in ("utf-8-sig", "cp949", "utf-16"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeError:
                continue
        else:
            raise ValueError("문자 인코딩을 읽을 수 없습니다. UTF-8로 저장하세요.")
        if ext in (".html", ".htm"):
            parser = _HTMLText()
            parser.feed(text)
            text = "".join(parser.parts)
        if ext in (".md", ".markdown") and re.search(r"^#{1,6}\s", text, re.M):
            return _markdown_sections(path, text)
        return [_section(path, text)]
    if ext == ".pdf":
        return _parse_pdf(path, ocr=ocr)
    if ext in (".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp"):
        return _parse_images(path, ocr=ocr)
    if ext == ".docx":
        from docx import Document
        from docx.oxml.ns import qn
        from docx.table import Table
        from docx.text.paragraph import Paragraph
        doc = Document(path)
        sections, parts, heading = [], [], ""
        def flush():
            if parts:
                sections.append(_section(path, "\n\n".join(parts), heading_path=heading))
                parts.clear()
        for element in doc.element.body:
            if element.tag == qn("w:p"):
                p = Paragraph(element, doc)
                if p.style and p.style.name.startswith("Heading"):
                    flush()
                    heading = p.text
                if p.text.strip():
                    parts.append(p.text)
            elif element.tag == qn("w:tbl"):
                table = Table(element, doc)
                parts.append(_table([[cell.text for cell in row.cells] for row in table.rows]))
        flush()
        return sections
    if ext == ".pptx":
        from pptx import Presentation
        def shape_text(shapes):
            parts = []
            for shape in shapes:
                if hasattr(shape, "shapes"):
                    parts.extend(shape_text(shape.shapes))
                if shape.has_text_frame:
                    parts.append(shape.text)
                if shape.has_table:
                    parts.append(_table([[cell.text for cell in row.cells] for row in shape.table.rows]))
            return parts
        prs = Presentation(path)
        return [_section(path, "\n\n".join(shape_text(slide.shapes)),
                         page_number=i, slide_number=i, heading_path=f"slide:{i}")
                for i, slide in enumerate(prs.slides, 1)]
    if ext == ".xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            return [_section(path, _table([[c.value if c.value is not None else "" for c in row]
                                         for row in ws.iter_rows()]),
                             heading_path=f"sheet:{ws.title}", sheet_name=ws.title)
                    for ws in wb.worksheets]
        finally:
            wb.close()
    if ext == ".hwpx":
        return _parse_hwpx(path)
    if ext == ".hwp":
        from ingestion.hwp import parse_hwp
        try:
            return parse_hwp(path)
        except ValueError as native_error:
            # HWP 3/distribution/encrypted files can require an approved converter.
            try:
                return _parse_legacy(path)
            except ValueError as converter_error:
                raise ValueError(f"{native_error}; {converter_error}") from converter_error
    if ext in (".doc", ".ppt"):
        return _parse_legacy(path)
    raise ValueError(f"unsupported: {ext}")



def _image_coverage(page, images):
    """Union area of clipped image rectangles, without double-counting overlaps."""
    import fitz
    rectangles = [page.rect & fitz.Rect(info["bbox"]) for info in images]
    rectangles = [rect for rect in rectangles if not rect.is_empty and rect.get_area() > 0]
    xs = sorted({coordinate for rect in rectangles for coordinate in (rect.x0, rect.x1)})
    area = 0.0
    for left, right in zip(xs, xs[1:]):
        spans = sorted((rect.y0, rect.y1) for rect in rectangles
                       if rect.x0 < right and rect.x1 > left)
        covered, end = 0.0, None
        for bottom, top in spans:
            if end is None or bottom > end:
                covered += top - bottom
                end = top
            elif top > end:
                covered += top - end
                end = top
        area += (right - left) * covered
    return min(1.0, area / max(1.0, page.rect.get_area()))


def _parse_pdf(path, ocr=None):
    import math
    import fitz
    from ingestion.ocr import OCRFailure, UnlimitedOCR

    dpi = int(os.getenv("RAG_OCR_DPI", "300"))
    max_pixels = int(os.getenv("RAG_OCR_MAX_PIXELS", "20000000"))
    if not 72 <= dpi <= 600 or max_pixels < 1000000:
        raise OCRFailure("OCR render DPI/pixel limits are invalid.")
    sections = []
    with fitz.open(path) as document:
        if document.needs_pass and not document.authenticate(""):
            raise ValueError("암호화된 PDF입니다. 암호를 해제한 사본을 제공하세요.")
        for number, page in enumerate(document, 1):
            native = page.get_text("text", sort=True).strip()
            significant = sum(char.isalnum() for char in native)
            invalid = sum(char == "\ufffd" or (not char.isprintable() and not char.isspace())
                          for char in native)
            images = page.get_image_info()
            coverage = _image_coverage(page, images)
            needs_ocr = (
                (not significant and bool(images or page.get_drawings()))
                or (bool(native) and invalid > max(2, len(native) * 0.05))
                or (significant < 40 and coverage >= 0.60)
            )
            method = "native"
            text = native
            if needs_ocr:
                ocr = ocr or UnlimitedOCR.from_environment()
                scale = dpi / 72
                pixels = page.rect.width * page.rect.height * scale * scale
                if pixels > max_pixels:
                    scale *= math.sqrt(max_pixels / pixels)
                pixmap = page.get_pixmap(matrix=fitz.Matrix(scale, scale), colorspace=fitz.csRGB,
                                        alpha=False)
                text = ocr.recognize(pixmap.tobytes("png"))
                if not isinstance(text, str) or not text.strip():
                    raise OCRFailure(f"Unlimited-OCR returned empty text for PDF page {number}.")
                method = "unlimited-ocr"
            sections.append(_section(path, text, page_number=number, extraction_method=method,
                heading_path=f"page:{number} / {'Unlimited-OCR' if method == 'unlimited-ocr' else 'native'}"))
    return sections


def _parse_images(path, ocr=None):
    import io
    from PIL import Image, ImageOps, ImageSequence
    from ingestion.ocr import OCRFailure, UnlimitedOCR

    ocr = ocr or UnlimitedOCR.from_environment()
    sections = []
    with Image.open(path) as image:
        for number, frame in enumerate(ImageSequence.Iterator(image), 1):
            frame = ImageOps.exif_transpose(frame.copy()).convert("RGB")
            output = io.BytesIO()
            frame.save(output, format="PNG")
            text = ocr.recognize(output.getvalue())
            if not isinstance(text, str) or not text.strip():
                raise OCRFailure(f"Unlimited-OCR returned empty text for image page {number}.")
            sections.append(_section(path, text, page_number=number,
                heading_path=f"page:{number} / Unlimited-OCR", extraction_method="unlimited-ocr"))
    return sections


def _parse_legacy(path):
    from ingestion.convert import convert_with_soffice
    import tempfile
    with tempfile.TemporaryDirectory(prefix="rag-convert-") as output:
        pdf = convert_with_soffice(path, out_dir=output)
        sections = parse_file(pdf)
    for section in sections:
        section["title"] = Path(path).name
    return sections


def _parse_hwpx(path):
    import re
    import zipfile
    import xml.etree.ElementTree as ET
    def tag(e):
        return e.tag.rsplit("}", 1)[-1]
    try:
        with zipfile.ZipFile(path) as z:
            names = sorted((n for n in z.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)),
                           key=lambda n: int(re.search(r"section(\d+)", n)[1]))
            if not names:
                raise ValueError("no sections in hwpx")
            sections = []
            for name in names:
                root = ET.fromstring(z.read(name))
                parts = []
                # Emit text once, including nested table paragraphs. HWPX uses hp:p;
                # some producers wrap them in pPara.
                for element in root.iter():
                    if tag(element) == "t" and element.text:
                        parts.append(element.text)
                    elif tag(element) in ("p", "pPara", "tr") and parts and parts[-1] != "\n":
                        parts.append("\n")
                sections.append(_section(path, "\n".join(t.strip() for t in parts if t.strip()),
                                         heading_path=name))
            return sections
    except (zipfile.BadZipFile, ET.ParseError) as e:
        raise ValueError(f"not a valid hwpx: {e}") from e
