"""검색 부스트: 부품번호·오류코드 정확일치, 같은 장비, 해결 완료 선행.

RRF 점수는 순위 합이라 정규화되지 않는다. 부스트는 순위를 뒤집을 만큼만,
그리고 RRF 스케일(0~2/60)에 맞춰 곱한다. 부스트가 새로운 근거를 만들 수는 없다.
"""
import json
import re

from ingestion.metadata import extract_identifiers

CODE = re.compile(r"[A-Z]{2,5}-\d{3,6}|ERR_[A-Z]{2,10}_\d{2,4}")


class BoostWeights:
    """부스트 크기. base는 곱셈 계수, 나머지는 RRF 스케일 가산분."""

    def __init__(self, exact_code=0.6, same_equipment=0.3, resolved=0.4, base=1.0,
                 max_total=1.5):
        self.exact_code = exact_code
        self.same_equipment = same_equipment
        self.resolved = resolved
        self.base = base
        self.max_total = max_total

    def as_dict(self) -> dict:
        return {"base": self.base, "exact_code": self.exact_code,
                "same_equipment": self.same_equipment, "resolved": self.resolved,
                "max_total": self.max_total}


def _list(value) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return []
    return parsed if isinstance(parsed, list) else []


def boost_scores(query, docs, weights=None) -> list:
    """부스트를 적용해 재정렬한다. 원본 dict는 보존한다."""
    defaults = BoostWeights().as_dict()
    w = {**defaults, **(weights or {})} if isinstance(weights, dict) else defaults
    max_total = w["max_total"]
    q_codes = set(CODE.findall(query or ""))
    q_equipment = set(extract_identifiers(query or "")["equipment"])
    out = []
    for d in docs:
        boost = 0.0
        if q_codes and q_codes & (set(_list(d.get("error_codes")))
                                   | set(_list(d.get("part_numbers")))):
            boost += w["exact_code"]
        if q_equipment and q_equipment & set(_list(d.get("equipment"))):
            boost += w["same_equipment"]
        if d.get("outcome") == "resolved":
            boost += w["resolved"]
        boost = min(boost, max_total)
        out.append({**d, "boost": boost,
                    "retrieval_score": d.get("retrieval_score", 0.0) * w.get("base", 1.0)
                    + boost / 60.0})
    return sorted(out, key=lambda d: d["retrieval_score"], reverse=True)