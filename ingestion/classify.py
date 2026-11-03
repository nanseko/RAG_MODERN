"""자동 분류: 규칙 1차 + 라벨 샘플 kNN 전파.

신규 문서는 색인 시 자동 분류·스탬프. 구문서는 refresh가 버전 기준으로 갱신.
사람 손이 필요한 경우: label_samples.json에 10~20개만 라벨하면 전파됨.
"""
METADATA_VERSION = "v2"

RULES = [
    ("시험", ("시험", "test", "측정", "검증", "평가")),
    ("이슈·장애", ("장애", "이슈", "고장", "불량", "trouble", "fail", "에러", "오류")),
    ("보고서", ("보고", "report", "결과서", "분석서")),
    ("매뉴얼", ("매뉴얼", "manual", "가이드", "절차서", "사용법")),
    ("회의·공지", ("회의", "공지", "minutes", "주간", "월간")),
]


def classify(title: str, file_name: str, text: str, identifiers: dict) -> str:
    blob = f"{title or ''} {file_name or ''} {(text or '')[:500]}".lower()
    for category, keywords in RULES:
        if any(k in blob for k in keywords):
            return category
    return "기타"


def _cosine(a, b) -> float:
    denom = (sum(x * x for x in a) ** 0.5) * (sum(x * x for x in b) ** 0.5)
    return sum(x * y for x, y in zip(a, b)) / denom if denom else 0.0


def propagate(vector, labeled: list, threshold: float = 0.8) -> str:
    """labeled=[(vector, category)]. 가장 가까운 라벨을 임계값 이상이면 전파."""
    best, score = "기타", threshold
    for vec, category in labeled:
        s = _cosine(vector, vec)
        if s >= score:
            best, score = category, s
    return best
