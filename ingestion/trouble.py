"""고장기록 구조화: 원인-조치 쌍을 보존하는 역할 라벨 + 해결 결과 판정.

역할(role)은 heading_path 기준, heading이 없으면 본문 앞쪽 마크다운 제목줄로 판정한다.
청크가 나뉘어도 같은 문서의 원인/조치 청크는 document_id로 함께 조회되므로 쌍이 유지된다.
"""
import json
import re

ROLE_KEYS = [
    ("symptom", ("증상", "현상", "문제제기", "시험조건", "고장현상")),
    ("cause", ("원인", "분석")),
    ("action", ("조치", "대응", "대책", "수리", "교체", "정비", "보수")),
    ("verification", ("검증", "확인", "시험결과", "시험 결과", "결과", "시험", "측정")),
]

RESOLVED = ("해결", "완료", "정상", "종결", "복구")
TEMPORARY = ("임시", "우회", "관찰 중", "모니터링 중", "대기")
UNRESOLVED = re.compile(
    r"미\s*(?:해결|완료|복구)|비정상|"
    r"(?:해결|완료|정상|종결|복구)[^\n.!?]{0,16}(?:아님|아니|않|못|실패|불가|예정)")


def role_of(heading: str, title: str, text_head: str) -> str:
    # 누적 heading 경로("상위 / 하위")에서는 가장 깊은 제목이 이 구간의 역할을 정한다.
    leaf = re.split(r"\s*/\s*", (heading or "").strip())[-1] if heading else ""
    for scope in (leaf, f"{leaf} {title or ''}".strip(), f"{heading or ''} {title or ''}"):
        for role, keys in ROLE_KEYS:
            if scope and any(k in scope for k in keys):
                return role
    for line in (text_head or "").splitlines()[:6]:
        t = line.strip().lstrip("#*").strip()
        if t and len(t) < 40:
            for role, keys in ROLE_KEYS:
                if any(k in t for k in keys):
                    return role
    return "other"


def outcome_of(text: str) -> str:
    t = text or ""
    if any(k in t for k in TEMPORARY):
        return "temporary"
    # A negated or planned resolution is not evidence of a resolved case.
    if UNRESOLVED.search(t):
        return "unknown"
    if any(k in t for k in RESOLVED):
        return "resolved"
    return "unknown"


def extract_trouble(sections: list) -> dict:
    """sections=[{heading_path,title,text}] → {symptom,cause,action,verification,outcome}."""
    buckets = {"symptom": [], "cause": [], "action": [], "verification": []}
    for s in sections:
        role = s.get("role") or role_of(s.get("heading_path", ""), s.get("title", ""), s.get("text", ""))
        if role in buckets:
            body = (s.get("text") or "").strip()
            if body:
                buckets[role].append(body)
    out = {k: "\n".join(v) for k, v in buckets.items()}
    out["outcome"] = outcome_of("\n".join(buckets["action"] + buckets["verification"]))
    return out


def assign_roles(chunks: list) -> list:
    """청크 dict에 role을 붙여 같은 리스트에 돌려준다."""
    for c in chunks:
        head = "\n".join((c.get("text") or "").splitlines()[:6])
        c["role"] = role_of(c.get("heading_path", ""), c.get("title", ""), head)
    return chunks


def document_summary(chunks: list) -> tuple:
    """(trouble_json, outcome) — 문서 단위 5필드 요약.

    heading_path가 비면(마크다운/텍스트) 청크 자체를 섹션으로 쓴다. 인라인 제목은
    이미 청크 경계를 나눴으므로 heading_path로 다시 승격하지 않는다.
    """
    if any(c.get("heading_path") for c in chunks):
        order, seen = [], set()
        for c in chunks:
            h = c.get("heading_path") or ""
            if h and h not in seen:
                seen.add(h)
                order.append(h)
        sections = [{"heading_path": h, "text": "\n".join(
            c.get("text", "") for c in chunks if (c.get("heading_path") or "") == h)}
            for h in order]
    else:
        sections = [{"heading_path": "", "text": c.get("text", ""), "role": c.get("role")}
                    for c in chunks]
    t = extract_trouble(sections)
    return json.dumps(t, ensure_ascii=False), t["outcome"]
