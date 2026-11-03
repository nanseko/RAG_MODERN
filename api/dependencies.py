"""Single local identity; enterprise SSO should replace this dependency."""
import hmac
import os

from fastapi import Header, HTTPException


def user_groups(authorization: str | None = Header(default=None)):
    token = os.getenv("RAG_API_TOKEN", "")
    if token and not hmac.compare_digest(authorization or "", "Bearer " + token):
        raise HTTPException(401, "인증 토큰이 필요합니다")
    return [g.strip() for g in os.getenv("RAG_USER_GROUPS", "all").split(",") if g.strip()]
