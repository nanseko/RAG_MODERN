BANNED = ["ignore previous instructions", "show confidential"]


def sanitize(text: str) -> str:
    t = text or ""
    low = t.lower()
    for b in BANNED:
        if b in low:
            t = t.replace(b, "[blocked]").replace(b.upper(), "[blocked]")
    return t
