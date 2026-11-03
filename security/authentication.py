"""Placeholder auth: 사내 SSO 연동 전 로컬 스텁."""


def authenticate(token: str = "") -> dict:
    return {"user_id": "local", "groups": ["all"]}


def authorize(user_groups, allowed_groups) -> bool:
    ug, ag = set(user_groups or []), set(allowed_groups or [])
    return bool("all" in ag or (ug & ag))
