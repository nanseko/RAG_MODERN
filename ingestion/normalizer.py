"""수집 텍스트 정규화: 파서 출력 → 청킹 입력 사이 1단계.

규칙: BOM/제로폭/제어문자 제거 → 탭·연속공백 1칸 → CRLF를 LF로 →
3개 이상 연속 개행 2개로. 표 구분자(|)와 의미 문자는 유지.
"""
import re

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_ZW = "\ufeff\u200b\u200c\u200d\u2060"


def normalize(text: str) -> str:
    t = text or ""
    for ch in _ZW:
        t = t.replace(ch, "")
    t = _CONTROL.sub("", t)
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+", " ", t)
    t = re.sub(r"\n{3,}", "\n\n", t)
    return "\n".join(line.strip() for line in t.split("\n")).strip()
