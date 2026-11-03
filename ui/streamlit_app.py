import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.environment import load_environment

load_environment()

import requests
import streamlit as st
from rag.conversation import SEARCH_NOTICE, validate_analysis

API = os.getenv("RAG_API_BASE", "http://localhost:8000").rstrip("/")
TOKEN = os.getenv("RAG_API_TOKEN", "")
HEADERS = {"Authorization": "Bearer " + TOKEN} if TOKEN else {}


def api_request(method, path, **kwargs):
    response = requests.request(method, API + path, headers=HEADERS, **kwargs)
    if not response.ok:
        try:
            detail = response.json().get("detail", response.reason)
        except ValueError:
            detail = response.reason
        raise RuntimeError(f"서버 응답 {response.status_code}: {detail}")
    return response


def preprocess():
    with st.spinner("문서를 전처리하고 있습니다. 문서 수와 CPU 성능에 따라 수 분 걸릴 수 있습니다."):
        st.session_state["scan_result"] = api_request("POST", "/api/v1/scan", timeout=1800).json()


st.set_page_config(page_title="사내 문서 질문기", layout="wide")
st.title("사내 문서 질문기")
st.caption("현상을 먼저 확인하고, 사내 문서에서 관련 근거를 찾아 해결책을 검토합니다.")

if st.button("새 현상 상담"):
    for key in ("answer", "answered_question", "chat_error", "last_answer_history_index"):
        st.session_state.pop(key, None)
    st.session_state["chat_history"] = []
    st.session_state["source_downloads"] = {}
    st.rerun()

try:
    health = api_request("GET", "/health", timeout=5).json()
    if health["llm_mode"] == "mock":
        st.info("모의 답변 모드 · 실제 LLM 서버가 연결되면 의미 검토와 종합 답변을 사용할 수 있습니다.")
    else:
        st.caption(f"LLM 연결 설정됨 · {health['llm_model']} (실제 응답은 질문 시 확인)")
    if health["embedding_mode"] != "qwen":
        st.warning("키워드 데모 모드입니다. 의미 검색을 사용하려면 로컬 임베딩 모델을 활성화하세요.")
except (requests.RequestException, RuntimeError) as e:
    st.error(f"RAG 서버에 연결할 수 없습니다. run.bat으로 서버를 실행하세요. {e}")

with st.sidebar:
    st.subheader("문서 전처리")
    st.write("문서 파일을 data/docs에 넣은 뒤 실행하세요.")
    if st.button("전처리 실행", use_container_width=True):
        try:
            preprocess()
        except (requests.RequestException, RuntimeError) as e:
            st.error(str(e))
    if result := st.session_state.get("scan_result"):
        if result.get("status") == "BUSY":
            st.info("다른 전처리 작업이 실행 중입니다.")
        elif result.get("status") in ("QUEUED", "PENDING"):
            st.info("전처리 작업을 대기열에 등록했습니다. 최근 전처리 상태에서 진행을 확인하세요.")
            if result.get("delivery_error"):
                st.warning("작업은 저장됐으나 작업 서버 연결에 실패했습니다. 연결 복구 후 재시도됩니다.")
        else:
            st.success(f"완료 {result['done']}건 · 실패 {result.get('failed', 0)}건")
            for item in result.get("results", []):
                if item["status"] == "FAILED":
                    st.error(f"{item['file_name']}: {item['error']}")
    with st.expander("최근 전처리 상태"):
        if st.button("상태 새로고침"):
            try:
                jobs = api_request("GET", "/api/v1/jobs", timeout=10).json()["jobs"]
                st.session_state["jobs"] = jobs
            except (requests.RequestException, RuntimeError) as e:
                st.error(str(e))
        for job in st.session_state.get("jobs", [])[:10]:
            st.write(f"{job['file_name']} · {job['status']} · {job['stage']}")
            if job.get("error"):
                st.caption(job["error"])

with st.form("question_form"):
    question = st.text_input("질문", placeholder="고온시험에서 출력이 감소한 사례 알려줘")
    submitted = st.form_submit_button("질문하기", type="primary")

LIBRARY_CSS = """
<style>
.library{display:flex;gap:6px;align-items:flex-end;justify-content:center;padding:18px 8px 8px;}
.book{width:26px;border-radius:3px 3px 0 0;animation:skim 1.1s ease-in-out infinite;}
.book:nth-child(1){height:64px;background:#c0564f;}
.book:nth-child(2){height:84px;background:#4f7ec0;animation-delay:.15s;}
.book:nth-child(3){height:72px;background:#4fa36b;animation-delay:.3s;}
.book:nth-child(4){height:92px;background:#c0a04f;animation-delay:.45s;}
.book:nth-child(5){height:68px;background:#7a5fc0;animation-delay:.6s;}
.book:nth-child(6){height:80px;background:#4fb8c0;animation-delay:.75s;}
@keyframes skim{0%,100%{transform:skewY(0deg)}50%{transform:skewY(-9deg)}}
.shelf{height:8px;background:#8a6b46;border-radius:2px;margin:0 20px;}
.search-note{text-align:center;color:var(--text-color,#555);}
@media(prefers-reduced-motion:reduce){.book{animation:none;}}
</style>
<div class="library"><div class="book"></div><div class="book"></div><div class="book"></div>
<div class="book"></div><div class="book"></div><div class="book"></div></div>
<div class="shelf"></div>
"""

if "chat_history" not in st.session_state:
    st.session_state["chat_history"] = []

for message_index, msg in enumerate(st.session_state["chat_history"]):
    if st.session_state.get("answer") and message_index == st.session_state.get("last_answer_history_index"):
        continue
    with st.chat_message(msg["role"]):
        st.write(msg["content"])

if submitted:
    if not question.strip():
        st.warning("질문을 입력하세요.")
    elif question.strip() in ("전처리", "전처리 실행", "/preprocess"):
        try:
            preprocess()
            st.success("전처리 결과를 왼쪽에서 확인하세요.")
        except (requests.RequestException, RuntimeError) as e:
            st.error(str(e))
    else:
        st.session_state.pop("answer", None)
        st.session_state.pop("answered_question", None)
        st.session_state["source_downloads"] = {}
        try:
            history = list(st.session_state["chat_history"])
            if len(history) > 32:
                raise RuntimeError("상담이 길어졌습니다. 새 현상 상담을 시작해 현재 현상을 요약해주세요.")
            observations = "\n".join([m["content"] for m in history if m["role"] == "user"] + [question])
            if len(observations) > 4000:
                raise RuntimeError("현상 설명이 길어졌습니다. 새 현상 상담에서 확인한 사실을 요약해주세요.")
            with st.spinner("사내 LLM이 현상을 분석하고 있습니다."):
                chat = validate_analysis(api_request(
                    "POST", "/api/v1/chat", json={"query": question, "history": history},
                    timeout=float(os.getenv("RAG_QUERY_TIMEOUT", "600"))).json())
            st.session_state["chat_history"].append({"role": "user", "content": question})
            if chat.get("analysis"):
                st.session_state["chat_history"].append({"role": "assistant", "content": chat["analysis"]})
            if chat["type"] == "clarify":
                st.session_state["chat_history"].append({"role": "assistant", "content": chat["message"]})
                st.rerun()
            st.session_state["chat_history"].append({"role": "assistant", "content": SEARCH_NOTICE})
            with st.chat_message("user"):
                st.write(question)
            with st.chat_message("assistant"):
                if chat.get("analysis"):
                    st.write(chat["analysis"])
                st.write(SEARCH_NOTICE)
                loading = st.empty()
                loading.markdown(LIBRARY_CSS + '<p class="search-note" role="status" aria-live="polite">'
                                 '관련 이슈를 찾고 근거를 검토하는 중…</p>', unsafe_allow_html=True)
                try:
                    st.session_state["answer"] = api_request(
                        "POST", "/api/v1/query", json={"query": observations, "search_query": chat["refined_query"]},
                        timeout=float(os.getenv("RAG_QUERY_TIMEOUT", "600"))).json()
                    st.session_state["answered_question"] = question
                    st.session_state["chat_history"].append({
                        "role": "assistant", "content": st.session_state["answer"]["answer"][:4000]})
                    st.session_state["last_answer_history_index"] = len(st.session_state["chat_history"]) - 1
                finally:
                    loading.empty()
            st.rerun()
        except (requests.RequestException, RuntimeError) as e:
            st.error(str(e))

if answer := st.session_state.get("answer"):
    st.subheader(st.session_state.get("answered_question", "답변"))
    if answer["answer_status"] in ("no_evidence", "insufficient_evidence", "citation_error", "review_failed"):
        st.warning(answer["answer"])
    else:
        st.write(answer["answer"])
    st.caption(f"검토 {answer['cycles']}회 · 관련 문서 {answer['retrieval_count']}건 · "
               f"제외 {answer.get('rejected_count', 0)}건 · {answer['total_latency']}초")
    review_status = answer.get("answer_review_status", "not_checked")
    if review_status == "passed":
        st.caption("LLM이 문서 근거·질문 적합성·논리 일관성을 검토했습니다.")
    sources, seen = [], set()
    for source in answer["sources"]:
        identity = source.get("document_id") or source["download_url"]
        if identity not in seen:
            seen.add(identity)
            sources.append(source)
    if sources:
        st.subheader("관련 문서 다운로드")
    for i, source in enumerate(sources, 1):
        st.write(f"{i}. {source['title']} · {source['version']}")
        pages = ", ".join(map(str, source.get("pages", []))) or "페이지 정보 없음"
        st.caption(f"{source['sid']} · 페이지 {pages}")
        artifact_key = source["document_id"] + "_" + source["version"]
        if not TOKEN:
            st.link_button("원본 다운로드", API + source["download_url"])
            st.link_button("Markdown 다운로드", API + source["markdown_url"])
        else:
            cache = st.session_state.setdefault("source_downloads", {})
            if st.button("다운로드 준비", key="prepare_" + artifact_key):
                try:
                    files = []
                    for kind, label in (("download", "원본"), ("markdown", "Markdown")):
                        response = api_request("GET", source[kind + "_url"], timeout=30)
                        filename = source["title"] if kind == "download" else source["title"] + ".md"
                        files.append((kind, label, filename, response.content,
                                      response.headers.get("Content-Type", "application/octet-stream")))
                    cache[artifact_key] = files
                except (requests.RequestException, RuntimeError) as e:
                    st.error(str(e))
            for kind, label, filename, content, mime in cache.get(artifact_key, []):
                st.download_button(label + " 다운로드", content, file_name=filename, mime=mime,
                                   key=kind + "_" + artifact_key)
        with st.expander("근거 내용", expanded=False):
            st.text(source["content"])
    with st.expander("검색·검토 과정"):
        for cycle in answer.get("cycle_trace", []):
            st.write(f"{cycle['cycle']}회: {cycle['search_query']}")
            st.caption(f"후보 청크 {cycle['candidates']}개 · 채택 {len(cycle['accepted_ids'])}건 · "
                       f"제외 {len(cycle['rejected_ids'])}건")
