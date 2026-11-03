"""Strict answer-review contract shared by the live client and generation workflow."""
CHECKS = ("grounded", "answers_question", "logically_consistent")


def validate_review(value):
    if not isinstance(value, dict) or any(type(value.get(key)) is not bool for key in CHECKS):
        raise RuntimeError("LLM 답변 검토의 판정 형식이 올바르지 않습니다")
    issues = value.get("issues")
    if not isinstance(issues, list) or len(issues) > 5 or any(
        not isinstance(issue, str) or not issue.strip() or len(issue) > 1000 for issue in issues
    ):
        raise RuntimeError("LLM 답변 검토의 수정 사유가 올바르지 않습니다")
    if all(value[key] for key in CHECKS) and issues:
        raise RuntimeError("LLM 답변 검토 결과가 서로 모순됩니다")
    return {key: value[key] for key in CHECKS} | {"issues": issues}


def approved(value):
    return all(value[key] for key in CHECKS)
