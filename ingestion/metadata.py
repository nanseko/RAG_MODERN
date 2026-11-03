import re

PART = re.compile(r"\b[A-Z]{2,5}-\d{3,6}\b")
ERR = re.compile(r"ERR_[A-Z]{2,10}_\d{2,4}")
# AN/TPS-43, KT-1 등 장비 모델. 숫자만 걸러내지 않도록 접두사를 요구한다.
EQUIPMENT = re.compile(r"\b(?:[A-Z]{1,4}(?:/[A-Z]{1,4})?[-/])?[A-Z]{1,4}-\d{1,4}[A-Z]?\b")


def extract_identifiers(text: str) -> dict:
    t = text or ""
    found = sorted({m for m in EQUIPMENT.findall(t) if not PART.fullmatch(m)})
    return {
        "part_numbers": sorted(set(PART.findall(t))),
        "error_codes": sorted(set(ERR.findall(t))),
        "equipment": found,
    }
