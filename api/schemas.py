from typing import Literal

from pydantic import BaseModel, Field, field_validator


class QueryRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    search_query: str | None = Field(default=None, min_length=1, max_length=4000)
    conversation_id: str | None = None
    # Backward-compatible field, never trusted for permission assignment.
    user_groups: list[str] = Field(default_factory=lambda: ["all"])

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("질문을 입력하세요")
        return value


class ChatMessage(BaseModel):
    role: Literal["user", "assistant"]
    content: str = Field(min_length=1, max_length=4000)


class ChatRequest(BaseModel):
    query: str = Field(min_length=1, max_length=4000)
    history: list[ChatMessage] = Field(default_factory=list, max_length=32)
    conversation_id: str | None = None
    user_groups: list[str] = Field(default_factory=lambda: ["all"])

    @field_validator("query")
    @classmethod
    def nonblank(cls, value):
        value = value.strip()
        if not value:
            raise ValueError("질문을 입력하세요")
        return value


class QueryResponse(BaseModel):
    answer: str
    sources: list[dict]
    request_id: str
    retrieval_count: int
    cycles: int = 1
    llm_mode: str
    answer_status: str
    answer_review_status: str = "not_checked"
    answer_revision_count: int = 0
    cycle_trace: list[dict] = Field(default_factory=list)
    rejected_count: int = 0
    total_latency: float
