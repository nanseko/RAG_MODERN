"""Qwen3 local embeddings: left padding, LAST token pooling, normalized vectors."""
import os
from functools import lru_cache

from models.runtime import MODEL_LOCK, device, model_path

TASK = "Given a web search query, retrieve relevant passages that answer the query"


@lru_cache(maxsize=1)
def _load(model_dir):
    import torch
    from transformers import AutoModel, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(model_dir, padding_side="left", local_files_only=True)
    chosen_device = device()
    model = AutoModel.from_pretrained(model_dir, dtype=torch.float32, local_files_only=True)
    return tok, model.to(chosen_device).eval()


def _instruct(query):
    return f"Instruct: {TASK}\nQuery:{query}"


def embed(texts, is_query=False, model_dir=None):
    import torch
    import torch.nn.functional as F
    if not texts:
        return []
    model_dir = model_dir or model_path("models/qwen3-embedding-0.6b", "RAG_EMBEDDING_MODEL_DIR")
    batch_size = int(os.getenv("RAG_EMBED_BATCH_SIZE", "4"))
    if batch_size < 1:
        raise ValueError("embedding batch size must be positive")
    vectors = []
    with MODEL_LOCK:
        tok, model = _load(model_dir)
        for start in range(0, len(texts), batch_size):
            inputs = [_instruct(t) if is_query else t for t in texts[start:start + batch_size]]
            batch = tok(inputs, padding=True, truncation=True, max_length=2048,
                        return_tensors="pt").to(model.device)
            with torch.inference_mode():
                states = model(**batch).last_hidden_state
                # Left-padded sequences all end at the final position, regardless of their lengths.
                vectors.extend(F.normalize(states[:, -1, :], p=2, dim=1).cpu().tolist())
    return vectors
