"""Temporary corpus -> parsing/Markdown/vector DB -> real HTTP-client contract -> citations."""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def prepare(docs):
    from docx import Document
    from pptx import Presentation
    from pptx.util import Inches
    from pypdf import PdfWriter
    from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject
    import zipfile

    (docs / "report.md").write_text("RELEVANT heat test power drop cause cooling", encoding="utf8")
    word = Document()
    word.add_paragraph("RELEVANT heat test conditions")
    word.add_table(1, 2).cell(0, 1).text = "power drop three dB"
    word.save(docs / "report.docx")
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    box = slide.shapes.add_textbox(Inches(1), Inches(1), Inches(5), Inches(2))
    box.text = "RELEVANT heat test cooling improvement"
    deck.save(docs / "report.pptx")
    writer = PdfWriter()
    page = writer.add_blank_page(width=612, height=792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"),
                             NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(b"BT /F1 12 Tf 72 700 Td (RELEVANT heat test verification report) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(stream)
    writer.write(docs / "report.pdf")
    xml = '<hs:sec xmlns:hs="urn:section" xmlns:hp="urn:paragraph"><hp:p><hp:run><hp:t>RELEVANT heat test Hancom record</hp:t></hp:run></hp:p></hs:sec>'
    with zipfile.ZipFile(docs / "report.hwpx", "w") as archive:
        archive.writestr("Contents/section0.xml", xml)
    (docs / "unrelated.txt").write_text("UNRELATED lunch menu only", encoding="utf8")


def main():
    os.environ["PYTHON_DOTENV_DISABLED"] = "1"
    import httpx
    from fastapi.testclient import TestClient
    from app.main import app
    from database.db import get_conn
    from ingestion.pipeline import scan_once
    from models.gemma_client import RealGemmaClient
    from rag.graph import run_rag

    with tempfile.TemporaryDirectory(prefix="rag-verified-e2e-") as temp:
        root = Path(temp)
        docs = root / "docs"
        docs.mkdir()
        os.environ["RAG_DB_PATH"] = str(root / "rag.sqlite3")
        os.environ["RAG_DOCS_DIR"] = str(docs)
        os.environ["RAG_MD_DIR"] = str(root / "md")
        os.environ["RAG_ORIGINALS_DIR"] = str(root / "originals")
        os.environ["RAG_AUTOSCAN"] = "0"
        os.environ["RAG_SCAN_INTERVAL_MIN"] = "0"
        os.environ.pop("GEMMA_API_URL", None)
        prepare(docs)
        scan = scan_once()
        assert scan["done"] == 6 and scan["failed"] == 0, scan
        assert len(list((root / "md").glob("*.md"))) == 6
        unchanged = scan_once()
        assert unchanged["jobs"] == 0 and unchanged["done"] == 0

        seen = {"judgments": 0, "generations": 0, "reviews": 0}
        def llm_endpoint(request):
            body = json.loads(request.content)
            system = body["messages"][0]["content"]
            user = body["messages"][-1]["content"]
            if system.startswith("Judge relevance"):
                seen["judgments"] += 1
                data = json.loads(user)
                answer = "no" if "UNRELATED" in data["document"] else "yes"
            elif system.startswith("Review answer"):
                seen["reviews"] += 1
                data = json.loads(user)
                assert "UNRELATED" not in json.dumps(data["sources"])
                answer = json.dumps({"grounded": True, "answers_question": True,
                                     "logically_consistent": True, "issues": []})
            elif system.startswith("Rewrite"):
                answer = "heat test cooling cause verification"
            else:
                seen["generations"] += 1
                assert "RELEVANT" in user and "UNRELATED" not in user
                answer = "Heat-test evidence reviewed [SOURCE_01]."
            return httpx.Response(200, json={"choices": [{"message": {"content": answer}}]})

        conn = get_conn()
        try:
            vectors = conn.execute("SELECT COUNT(*) FROM chunk_vectors").fetchone()[0]
            if os.getenv("USE_QWEN", "1") != "0":
                assert vectors >= 6, vectors
            llm = RealGemmaClient("http://internal-llm/v1",
                http=httpx.Client(transport=httpx.MockTransport(llm_endpoint)))
            result = run_rag("heat test power drop", ["all"], conn, client=llm,
                top_k=2, target_min=5, target_max=5, max_cycles=3)
            assert result["answer_status"] == "answered" and len(result["sources"]) == 5, result
            assert result["cycles"] == 3, result["cycle_trace"]
            assert result["rejected_count"] <= 1, result["cycle_trace"]
            assert len({s["document_id"] for s in result["sources"]}) == len(result["sources"])
            assert all("UNRELATED" not in s["content"] for s in result["sources"])
            assert seen["generations"] == 1 and seen["reviews"] == 1
            assert result["answer_review_status"] == "passed"
            assert "SOURCE_01" not in result["answer"]
            with TestClient(app) as client:
                for source in result["sources"]:
                    assert client.get(source["download_url"]).status_code == 200
                    assert client.get(source["markdown_url"]).status_code == 200
        finally:
            conn.close()
        print("E2E PASS:", json.dumps({"files": 6, "sources": len(result["sources"]),
              "cycles": result["cycles"], "rejected": result["rejected_count"],
              "vectors": vectors, "llm": "HTTP contract simulated"}, ensure_ascii=False))


if __name__ == "__main__":
    main()
