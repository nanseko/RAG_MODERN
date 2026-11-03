from dataclasses import dataclass, field


@dataclass
class RAGState:
    query: str
    user_groups: list = field(default_factory=list)
    candidates: list = field(default_factory=list)
    context: list = field(default_factory=list)
    rewritten: str = ""
