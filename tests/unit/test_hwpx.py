import os
import tempfile
import zipfile

import pytest

from ingestion.parser import parse_file

SECTION_XML = """<?xml version="1.0" encoding="UTF-8"?>
<hs:sec xmlns:hs="http://www.hancom.co.kr/hwpml/2011/section"
        xmlns:hp="http://www.hancom.co.kr/hwpml/2011/paragraph">
  <hp:pPara><hp:p><hp:run><hp:t>고온시험 결과 출력이 감소했다</hp:t></hp:run></hp:p></hp:pPara>
  <hp:pPara><hp:p><hp:run><hp:t>원인분석 결과 방열 문제로 확인</hp:t></hp:run></hp:p></hp:pPara>
</hs:sec>
"""


def _make_hwpx(path: str):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("[Content_Types].xml", "<Types/>")
        z.writestr("Contents/section0.xml", SECTION_XML)


def test_parse_hwpx_extracts_text():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sample.hwpx")
        _make_hwpx(p)
        secs = parse_file(p)
        full = "\n".join(s["text"] for s in secs)
        assert "고온시험 결과 출력이 감소했다" in full
        assert "원인분석 결과 방열 문제로 확인" in full


def test_old_hwp_raises_clear_error():
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "sample.hwp")
        open(p, "wb").write(b"")
        with pytest.raises(ValueError):
            parse_file(p)
