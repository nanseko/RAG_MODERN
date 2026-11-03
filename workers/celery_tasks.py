"""Worker tasks — 로컬은 동기 실행, prod는 Celery+Redis로 교체해도 동일 함수 사용.

모든 task는 idempotent: 파이프라인이 해시 비교로 Skip하고 동일 job 재실행 시
DONE을 그대로 반환하므로 중복 청크가 생기지 않는다.
"""


def collect_document(conn, docs_dir: str = "data/docs") -> list:
    from ingestion.collector import scan_dir
    return scan_dir(conn, docs_dir)


def run_pipeline_job(conn, job_id: str) -> dict:
    from ingestion.pipeline import run_job
    return run_job(conn, job_id)


def parse_document(path: str):
    from ingestion.parser import parse_file
    return parse_file(path)


def chunk_document(sections: list):
    from ingestion.chunker import hybrid_chunk
    return hybrid_chunk(sections)


def index_document_task(conn, doc: dict, chunks: list) -> str:
    from ingestion.indexer import index_document
    return index_document(conn, doc, chunks)
