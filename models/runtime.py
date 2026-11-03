"""Serialize local GPU/CPU inference and model initialization across API workers."""
import os
import threading
from pathlib import Path

from app.settings import ROOT

MODEL_LOCK = threading.RLock()


def model_path(default, env):
    candidate = Path(os.getenv(env, default))
    return str(candidate if candidate.is_absolute() else ROOT / candidate)


def device():
    import torch
    requested = os.getenv("RAG_MODEL_DEVICE", "auto")
    return ("cuda" if torch.cuda.is_available() else "cpu") if requested == "auto" else requested
