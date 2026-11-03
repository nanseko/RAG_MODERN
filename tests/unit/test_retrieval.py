from retrieval.hybrid import acl_filter, dedup
from retrieval.rrf import rrf_fuse


def test_rrf_order():
    a = ["d1", "d2", "d3"]
    b = ["d2", "d1", "d4"]
    fused = rrf_fuse([a, b], k=60)
    assert fused[0] in ("d1", "d2") and "d4" in fused


def test_acl_filter_blocks_unauthorized_document():
    docs = [{"chunk_id": "c1", "allowed_groups": ["radar_team"]}]
    assert acl_filter(docs, ["production_team"]) == []
    assert len(acl_filter(docs, ["radar_team"])) == 1


def test_duplicate_chunk_removal():
    docs = [{"chunk_id": "c1", "text": "출력 저하"}, {"chunk_id": "c2", "text": "출력 저하"}]
    assert len(dedup(docs)) == 1
