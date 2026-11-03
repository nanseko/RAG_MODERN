"""Qwen3-Reranker local yes/no-token probabilities."""
from functools import lru_cache

from models.runtime import MODEL_LOCK, device, model_path

PREFIX = ('<|im_start|>system\nJudge whether the Document meets the requirements '
          'based on the Query and the Instruct provided. Note that the answer '
          'can only be "yes" or "no".<|im_end|>\n<|im_start|>user\n')
SUFFIX = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"
TASK = "Given a web search query, retrieve relevant passages that answer the query"


@lru_cache(maxsize=1)
def _load(model_dir):
    import torch
    from transformers import AutoModelForCausalLM, AutoTokenizer
    tok = AutoTokenizer.from_pretrained(model_dir, padding_side="left", local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(model_dir, dtype=torch.float32, local_files_only=True).to(device()).eval()
    return tok, model, (tok.convert_tokens_to_ids("no"), tok.convert_tokens_to_ids("yes"))


def _fmt(query, doc):
    return f"<Instruct>: {TASK}\n<Query>: {query}\n<Document>: {doc}"


def score_pairs(pairs, model_dir=None, max_length=2048):
    import torch
    if not pairs:
        return []
    model_dir = model_dir or model_path("models/qwen3-reranker-0.6b", "RAG_RERANKER_MODEL_DIR")
    with MODEL_LOCK:
        tok, model, (no_id, yes_id) = _load(model_dir)
        pre = tok.encode(PREFIX, add_special_tokens=False)
        suf = tok.encode(SUFFIX, add_special_tokens=False)
        enc = tok([_fmt(q, d) for q, d in pairs], padding=False,
                  truncation=True, return_attention_mask=False,
                  max_length=max_length - len(pre) - len(suf))
        enc["input_ids"] = [pre + ids + suf for ids in enc["input_ids"]]
        batch = tok.pad(enc, padding=True, return_tensors="pt").to(model.device)
        with torch.inference_mode():
            logits = model(**batch).logits[:, -1, :]
            scores = torch.softmax(torch.stack([logits[:, no_id], logits[:, yes_id]], dim=1), dim=1)[:, 1]
        return scores.cpu().tolist()
