import logging
import time

import httpx
from fastapi import APIRouter, Depends, HTTPException

from api.dependencies import user_groups
from api.schemas import ChatRequest, QueryRequest, QueryResponse
from database.db import get_conn
from models.gemma_client import get_client
from rag.conversation import analyze
from rag.graph import run_rag
from security.prompt_security import sanitize

router = APIRouter(prefix="/api/v1")
logger = logging.getLogger(__name__)


@router.post("/chat")
def chat(req: ChatRequest, groups=Depends(user_groups)):
    client = get_client()
    try:
        history = [{"role": m.role, "content": sanitize(m.content)} for m in req.history]
        out = analyze(sanitize(req.query), history, client)
    except httpx.TimeoutException as exc:
        raise HTTPException(504, "현상 분석 응답 시간이 초과되었습니다. 다시 시도하세요.") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, "사내 LLM의 현상 분석 요청에 실패했습니다. 다시 시도하세요.") from exc
    except RuntimeError as exc:
        raise HTTPException(502, "현상 분석 결과를 확인하지 못했습니다. 검색을 시작하지 않았습니다.") from exc
    finally:
        client.close()
    out["groups"] = groups
    return out


@router.post("/query", response_model=QueryResponse)
def query(req: QueryRequest, groups=Depends(user_groups)):
    started = time.monotonic()
    conn = get_conn()
    try:
        options = {"search_query": sanitize(req.search_query)} if req.search_query is not None else {}
        out = run_rag(sanitize(req.query), groups, conn, **options)
    except httpx.TimeoutException as e:
        raise HTTPException(504, "LLM 응답 시간이 초과되었습니다. 다시 시도하세요.") from e
    except httpx.HTTPError as e:
        raise HTTPException(502, "LLM 서버 연결 또는 응답에 실패했습니다.") from e
    except RuntimeError as e:
        raise HTTPException(502, str(e)) from e
    finally:
        conn.close()
    out["total_latency"] = round(time.monotonic() - started, 3)
    logger.info("rag request_id=%s cycles=%d sources=%d status=%s latency=%.3f",
                out["request_id"], out["cycles"], out["retrieval_count"],
                out["answer_status"], out["total_latency"])
    return out
