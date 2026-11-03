from ingestion.chunker import hybrid_chunk
from ingestion.metadata import extract_identifiers


def test_chunk_does_not_exceed_limit():
    secs = [{"title": "t", "heading_path": "4. 고온시험", "page_number": 1,
             "text": "출력 저하. " * 800}]
    chunks = hybrid_chunk(secs, target_tokens=650, max_tokens=1000)
    assert len(chunks) >= 2
    assert all(len(c["text"]) // 4 <= 1000 for c in chunks)


def test_table_header_is_preserved():
    secs = [{"title": "t", "heading_path": "h", "page_number": 1,
             "text": "조건 | 결과\n값 | 출력저하"}]
    chunks = hybrid_chunk(secs)
    assert "조건" in chunks[0]["text"]


def test_identifiers_extracted():
    ids = extract_identifiers("부품 ABC-1234 에서 ERR_TX_041 발생")
    assert "ABC-1234" in ids["part_numbers"]
    assert "ERR_TX_041" in ids["error_codes"]
