from ingestion.normalizer import normalize


def test_collapses_whitespace_and_blank_lines():
    out = normalize("제목\r\n\r\n\r\n본문   내용\t\t추가")
    assert out == "제목\n\n본문 내용 추가"


def test_removes_control_chars_but_keeps_table_pipes():
    out = normalize("조건 | 결과\x00\x07\n값 | 출력저하")
    assert "\x00" not in out and "조건 | 결과" in out


def test_strips_zero_width_and_bom():
    out = normalize("\ufeff\u200b시작 본문\u200b 끝")
    assert out == "시작 본문 끝"
