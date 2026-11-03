# 사내 문서 문제 해결 RAG — Windows 실행판

**운영 대상: Windows + RTX 5080. 별도 Linux 서버·Docker·Redis 없이 실행합니다.**

1. 새 PC에는 Windows Python 3.12 x64와 사내 승인 NVIDIA 드라이버를 준비합니다. 프로젝트의 `.venv`·`.venv-ocr`는 다른 PC로 복사하지 말고 다시 만드세요.
2. `offline-setup.bat`, `setup-ocr-windows.bat` 순서로 실행합니다. 준비된 로컬 wheel만 사용합니다.
3. `.env`의 `GEMMA_API_URL`, `GEMMA_MODEL`, 필요하면 `GEMMA_API_KEY`를 채웁니다.
4. `check-windows.bat`으로 GPU를 확인하고 `run.bat`을 실행합니다. [질문기](http://127.0.0.1:8501)를 엽니다.
5. 자료를 `data/docs`에 넣고 **전처리 실행**을 누릅니다. 처리 완료를 확인한 뒤 현상을 설명합니다.

[Windows 설치·실행 안내](docs/windows-deployment.md) · [구현 점검](docs/implementation-audit-2026-10-14.md) · [Windows OCR](docs/windows-ocr.md)

현재 PC는 Intel 내장 GPU만 있어 실제 RTX 5080 OCR 추론을 검증하지 못했습니다. Windows OCR 격리 설치·모델 구성, 실제 DOC/PPT 변환, 별도 전처리 프로세스와 검색·다운로드 흐름은 검증했습니다. 원 설계의 실제 사내 LLM 품질·규모·부하 합격 조건은 별도 검증이 필요합니다. SSO는 사용자 요청대로 제외했습니다.

## 구성과 시작·중지

| 구성 | Windows 실행 |
|---|---|
| 질문 API·화면 | 프로젝트 `.venv`의 FastAPI·Streamlit |
| 전처리 | `RAG_TASK_BACKEND=native` — SQL에 작업을 보관하는 별도 worker |
| 임베딩·재순위 | 로컬 Qwen; 기본 app 설치는 CPU Torch |
| OCR | 같은 PC의 `.venv-ocr` + CUDA 12.8 + Unlimited-OCR, `127.0.0.1:10000` |
| 메타데이터·검색 | 기본 SQLite + sqlite-vec; Windows OpenSearch·PostgreSQL 선택 가능 |
| DOC/PPT 변환 | 함께 준비한 LibreOffice; 시스템 설치 없이 자동 탐지 |

`run.bat`은 API·화면·worker·설정된 로컬 OCR을 숨김 프로세스로 시작합니다. GPU가 없거나 OCR 환경이 잘못됐으면 해당 문제를 표시하고 핵심 앱은 시작합니다. 이미 사용 중인 포트의 다른 프로세스는 인수·종료하지 않습니다.

`status.bat`으로 상태를 확인하고 `stop.bat`으로 중지합니다. worker는 현재 작업을 마친 후 종료합니다. 작업이 오래 걸리면 중지 요청을 표시하며, 완료 후 `stop.bat`을 다시 실행하면 나머지 프로세스도 종료됩니다. 로그는 `data/runtime/logs`에 있습니다.

기본은 CPU Qwen + GPU OCR입니다. Qwen도 GPU로 옮기려면 `setup-cuda.bat`을 사용할 수 있습니다. 두 환경의 모델이 GPU 메모리를 공유하므로 실제 동시 작업·메모리·성능 검증이 필요합니다.

## 사내 LLM과 설정

프로젝트 [.env.example](.env.example)에 전체 예시가 있습니다. 셸 환경변수가 `.env`보다 우선하며 변경 후 서버를 다시 시작해야 합니다.

```dotenv
GEMMA_API_URL=http://사내-서버:포트/v1
GEMMA_MODEL=서버에-등록된-정확한-모델명
GEMMA_API_KEY=

RAG_TASK_BACKEND=native
RAG_DB_BACKEND=sqlite
RAG_SEARCH_BACKEND=sqlite
RAG_OCR_MODE=api
RAG_OCR_API_URL=http://127.0.0.1:10000/v1
RAG_OCR_PYTHON_PATH=.venv-ocr/Scripts/python.exe
```

OpenAI 호환 Chat Completions API를 사용합니다. URL은 `/v1`까지 또는 `/v1/chat/completions` 전체입니다. 모델명을 지정하는 것만으로 생성 모델이 설치되지는 않습니다. `GEMMA_API_URL`이 비어 있으면 화면에 명시된 모의 모드입니다.

## 대화 → 검색 → 해결책

질문을 바로 검색하지 않습니다. 사내 LLM이 현상을 분석하고 필요한 추가 확인을 합니다. 분석 실패·잘못된 판단 JSON을 검색 준비로 바꾸지 않습니다.

현상이 확인되면 **“사내 RAG에서 관련된 이슈 사항이 있었는지 찾아볼게요.”** 안내와 책장을 훑는 로딩 애니메이션을 표시합니다. 검색어는 다듬어도 문서 판정·재순위·답변 검토는 사용자의 원래 설명을 기준으로 합니다.

BM25와 벡터 검색을 합치고 재순위한 뒤 관련성을 검토합니다. 승인 자료를 보존하고 무관한 자료를 제외하며 최대 3회 검색합니다. 서로 다른 파일 3~5개를 목표로 하고, 같은 파일의 여러 장이나 내용이 같은 사본은 중복으로 세지 않습니다. 문서가 부족하면 부족 상태를 표시합니다.

답변은 문서의 조건·원인·조치·검증과 한계를 설명합니다. 실제 LLM이 문서 근거·질문 적합성·논리를 재검토하고 필요한 경우 한 번 수정합니다. 재검토 실패나 근거 부재이면 해결책을 확정하지 않습니다. 답변 아래에 인용 버전의 원본·Markdown 다운로드를 중복 없이 표시합니다.

이전 답변은 후속 질문의 이력에 남습니다. 다른 문제는 **새 현상 상담**으로 시작하세요. 모의 모드와 HTTP 계약 시험은 실제 사내 LLM의 답변 품질을 입증하지 않습니다.

## 문서와 전처리

| 형식 | 처리와 제한 |
|---|---|
| MD/TXT/HTML | 제목·본문; HTML 스크립트·스타일 제외 |
| PDF | 원문·페이지, 스캔·손상 페이지는 Unlimited-OCR |
| PNG/JPG/TIFF/BMP/WEBP | OCR; 다중 TIFF 지원 |
| DOCX/PPTX/XLSX | 문단·표·슬라이드·저장된 셀 값 |
| HWPX/HWP 5 | 본문·표; 암호화·배포용·구형 변형 제한 |
| DOC/PPT | 함께 준비한 LibreOffice로 PDF 변환 후 추출 |

복잡한 배치·차트 해석은 제한됩니다. DOC/PPT는 합성 자료의 실제 변환·본문 추출을 확인했습니다. 문서별 수식·표 배치 완전성은 실제 표본으로 확인해야 합니다.

원본은 `data/docs`, 버전별 Markdown은 `data/md`, 색인 시점 원본 사본은 `data/originals`에 보관합니다. 요청의 `date/docs`를 실제로 쓰려면 `RAG_DOCS_DIR=date/docs`로 설정합니다.

시작 시 스캔은 `RAG_AUTOSCAN=1`, 주기는 `RAG_SCAN_INTERVAL_MIN`으로 설정합니다. 수동만 사용하려면 둘 다 `0`으로 설정하세요. native worker가 스캔·저장된 작업·재시도·검색색인 동기화를 담당하며 API가 중지돼도 작업을 진행할 수 있습니다.

`QUEUED`는 완료가 아닙니다. 질문기의 최근 전처리 상태 또는 `GET /api/v1/jobs`에서 완료·실패를 확인하세요. 긴 작업은 heartbeat로 lease를 갱신합니다. Windows 프로필의 작업 lease는 120초이며 실패 재시도는 최대 3회입니다.

파서 처리 버전은 `ingest-v4`입니다. 다음 전처리에서는 이전 처리 버전의 자료도 재처리 대상이 됩니다. 실제 자료의 초기화·일괄 재색인은 이번 작업에서 수행하지 않았습니다.

## Windows 선택 서비스와 오프라인 자료

[Windows 서비스 안내](docs/windows-services.md)에 OpenSearch·PostgreSQL의 프로젝트 내부 초기화·시작·상태·중지를 정리했습니다. 기존 SQLite 자료를 자동 이전하지 않습니다. OpenSearch 실제 계약 시험은 통과했으며 PostgreSQL의 실제 SQL 연결 검증은 아직 완료되지 않았습니다.

`.venv-ocr`은 Torch `2.10.0+cu128`, TorchVision `0.25.0+cu128`, Transformers `4.57.1`을 고정합니다. Windows wheel 55개 약 3GB와 Unlimited-OCR 모델 약 6.8GB를 준비했습니다. [해시 lock](configs/requirements-ocr-windows.lock.txt)으로 오프라인 설치합니다. Linux용 준비 파일은 보관만 하며 Windows 설치에는 사용하지 않습니다.

`vendor/offline-manifest.json`에는 Windows app/OCR wheel, Windows 서비스 배포판, LibreOffice runtime, 모델이 있습니다. 이전 파일 손상을 다음 명령으로 확인합니다.

```powershell
.\.venv\Scripts\python.exe scripts/verify_offline_bundle.py
.\.venv\Scripts\python.exe scripts/prepare_windows_ocr.py --verify-only
.\.venv\Scripts\python.exe scripts/download_ocr_model.py --verify-only
```

Docker 배포 템플릿과 Celery 설정은 과거 선택 구성입니다. 현재 Windows 기본 실행에는 사용하지 않습니다. LibreOffice 재준비가 필요하면 `scripts/prepare_libreoffice.py`가 검증된 MSI를 시스템 설치 없이 프로젝트 안으로 추출합니다.

## API와 검증

[API 문서](http://127.0.0.1:8000/docs)

| API | 용도 |
|---|---|
| `POST /api/v1/chat` | 현상 분석·확인 질문·검색 준비 판단 |
| `POST /api/v1/query` | 준비된 문제 설명으로 RAG; 선택적 `search_query` |
| `POST /api/v1/preprocess` 또는 `/scan` | 전처리 요청 |
| `POST /api/v1/documents` | multipart `f` 업로드 |
| `GET /api/v1/documents`, `/documents/{id}`, `/jobs` | 문서·작업 상태 |
| `POST /api/v1/jobs/{id}/retry`, `/documents/{id}/reindex` | 재처리 |
| `GET /api/v1/documents/{id}/download`, `.../markdown` | 버전별 원본·MD |
| `DELETE /api/v1/documents/{id}` | 검색 대상 제외 |

`RAG_API_TOKEN` 설정 시 API Bearer 인증을 요구합니다. 권한은 서버의 `RAG_USER_GROUPS`로 정하며 본문으로 임의 부여하지 않습니다. SSO·개인별 계정 관리는 제외했습니다.

`/health`는 설정, `/ready`는 선택한 DB·검색·worker 준비 상태, `/metrics`는 프로세스의 요청·문서·작업 수를 표시합니다. OCR 준비 검사는 설정 검사와 실제 GPU 추론을 구분합니다.

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe scripts/windows_smoke.py
.\.venv\Scripts\python.exe scripts/windows_office_smoke.py
.\.venv\Scripts\python.exe scripts/e2e_check.py
```

최종 전체 회귀는 **290개 통과·5개 별도 서비스 시험 건너뜀**입니다. 실제 Qwen을 사용한 Windows 워커 통합에서 문서6개·벡터6개·출처5개와 다운로드를 확인했습니다.

시험은 임시 자료·DB로 격리됩니다. 최신 시험 로그는 `docs/windows-*.txt`에 있습니다. 실제 사내 LLM 품질·RTX 5080 OCR 정확도·대규모 성능·장애 합격 시험은 운영 PC에서 수행해야 합니다. 원 명세의 Docling·LangGraph 실제 전환과 품질 평가 대시보드도 남아 있습니다.
