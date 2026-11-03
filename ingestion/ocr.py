"""Unlimited-OCR adapter. Application calls stay inside the configured internal network.

The reviewed Baidu snapshot uses CUDA BF16 in its own infer/forward methods.
Local inference therefore runs in a separate pinned Transformers environment.
"""
import base64
import hashlib
import ipaddress
import json
import os
import re
import socket
import ssl
import subprocess
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import httpx

ROOT = Path(__file__).resolve().parents[1]
MODEL_REPO = "baidu/Unlimited-OCR"
MODEL_REVISION = "07dea832e22aefee32ad281d4b80551282e1c168"
WEIGHT_NAME = "model-00001-of-000001.safetensors"
WEIGHT_BYTES = 6672547120
WEIGHT_SHA256 = "2bc48a7a110061ea58fff65d3169367eebe3aee371ca6968dc2219c1b2855fc6"
REVIEWED_CODE_SHA256 = {
    "configuration_deepseek_v2.py": "b8470dd616ba8745fce6e27b093aef73a098863cc891b2477dcf9326a36000f7",
    "conversation.py": "ec7b6ce89bcda643de1f43269ffa66a7b2e65dc3ed30e427958f776546b4ba03",
    "deepencoder.py": "0ae2fb6d1e5ae8cf100fc32f854830acd08c821a0a1f23a94a76588c222ddcf2",
    "modeling_deepseekv2.py": "74e36e6bd0ba7bc565ef76464a99baa8e6bccb710ae9c1007b54ac30b855fa4c",
    "modeling_unlimitedocr.py": "268bdcbe12cf37bf5a2debb53faf542e56570958a5d9f3314aab3cab2cf6cb48",
}
PRIVATE_NETWORKS = [ipaddress.ip_network(net) for net in
    ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fc00::/7")]
_DET_LINE = re.compile(r"^\s*(?:<\|ref\|>(.*?)<\|/ref\|>)?\s*<\|det\|>(.*?)<\|/det\|>(.*)$")
_RUNTIME_LOCK = threading.RLock()


class OCRFailure(ValueError):
    """A failure that must fail the ingestion job, rather than indexing partial OCR."""


def offline_environment():
    os.environ["HF_HOME"] = str(ROOT / "data/ocr-cache/hf")
    os.environ["HF_MODULES_CACHE"] = str(ROOT / "data/ocr-cache/modules")
    os.environ["TORCH_HOME"] = str(ROOT / "data/ocr-cache/torch")
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE",
                 "HF_HUB_DISABLE_TELEMETRY"):
        os.environ[name] = "1"


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def validate_snapshot(model_dir):
    """Check required local files and reviewed code before trust_remote_code executes."""
    root = Path(model_dir).resolve()
    required = ["config.json", "tokenizer.json", "tokenizer_config.json",
                "special_tokens_map.json", "model.safetensors.index.json", WEIGHT_NAME,
                *REVIEWED_CODE_SHA256]
    if not root.is_dir() or any(not (root / name).is_file() for name in required):
        raise OCRFailure("Unlimited-OCR local snapshot is missing; run scripts/download_ocr_model.py online first.")
    if (root / WEIGHT_NAME).stat().st_size != WEIGHT_BYTES:
        raise OCRFailure("Unlimited-OCR weights are incomplete; verify the offline model manifest.")
    for name, expected in REVIEWED_CODE_SHA256.items():
        if sha256_file(root / name) != expected:
            raise OCRFailure(f"Unlimited-OCR reviewed source checksum mismatch: {name}")
    # auto_map must refer only to this pinned local module, never another repository.
    try:
        config = json.loads((root / "config.json").read_text(encoding="utf8"))
        if config.get("auto_map") != {
            "AutoConfig": "modeling_unlimitedocr.UnlimitedOCRConfig",
            "AutoModel": "modeling_unlimitedocr.UnlimitedOCRForCausalLM"}:
            raise ValueError("auto_map")
        weights = json.loads((root / "model.safetensors.index.json").read_text(encoding="utf8"))
        if set(weights["weight_map"].values()) != {WEIGHT_NAME}:
            raise ValueError("weight_map")
    except (ValueError, KeyError, TypeError) as exc:
        raise OCRFailure("Unlimited-OCR local model configuration does not match the pinned snapshot.") from exc
    return root


def clean_ocr_text(raw):
    """Strip Baidu grounding markers, retaining Markdown/table contents."""
    if not isinstance(raw, str):
        raise OCRFailure("Unlimited-OCR returned malformed text.")
    raw = raw.replace("<｜end▁of▁sentence｜>", "").replace("<|endoftext|>", "")
    blocks, current, skip_image = [], [], False
    for line in raw.splitlines():
        matched = _DET_LINE.match(line)
        if matched:
            if current:
                blocks.append("\n".join(current))
                current = []
            reference, detection, text = matched.groups()
            category = reference or detection.split("[", 1)[0].strip()
            skip_image = category.lower() == "image"
            if not skip_image and text.strip():
                current.append(text.strip())
        elif not skip_image:
            # Preserve ordinary Markdown, including blank lines within a block.
            current.append(line.rstrip())
    if current:
        blocks.append("\n".join(current))
    text = "\n\n".join(part.strip() for part in blocks if part.strip()).strip()
    if not text:
        raise OCRFailure("Unlimited-OCR returned empty text; check the official prompt and decode settings.")
    return text


def internal_api_url(value):
    try:
        url = urlsplit(value)
        if (url.scheme not in ("http", "https") or not url.hostname or url.username
                or url.password or url.query or url.fragment):
            raise ValueError("URL")
        addresses = {ipaddress.ip_address(item[4][0])
                     for item in socket.getaddrinfo(url.hostname, url.port or 80, type=socket.SOCK_STREAM)}
        if not addresses or any(not (addr.is_loopback or any(addr in net for net in PRIVATE_NETWORKS))
                                for addr in addresses):
            raise ValueError("public address")
        path = url.path.rstrip("/")
        if not path.endswith("/chat/completions"):
            path += "/chat/completions" if path.endswith("/v1") else "/v1/chat/completions"
        return urlunsplit((url.scheme, url.netloc, path, "", ""))
    except (ValueError, OSError) as exc:
        raise OCRFailure("RAG_OCR_API_URL must resolve only to loopback or private internal addresses.") from exc


class UnlimitedOCR:
    def __init__(self, *, mode="disabled", api_url="", api_key="", model="Unlimited-OCR",
                 model_dir=None, python_path=None, timeout=1200, max_tokens=8192,
                 ca_bundle=None, transport=None):
        if mode not in ("disabled", "api", "local"):
            raise OCRFailure("RAG_OCR_MODE must be disabled, api or local.")
        if timeout <= 0 or not 1 <= max_tokens <= 32768:
            raise OCRFailure("OCR timeout/token limits are invalid.")
        self.mode, self.api_key, self.model = mode, api_key, model
        self.api_url = internal_api_url(api_url) if mode == "api" else ""
        self.model_dir = Path(model_dir or ROOT / "models/unlimited-ocr")
        if not self.model_dir.is_absolute():
            self.model_dir = ROOT / self.model_dir
        default_python = ROOT / (".venv-ocr/Scripts/python.exe" if os.name == "nt" else ".venv-ocr/bin/python")
        self.python_path = Path(python_path or default_python)
        if not self.python_path.is_absolute():
            self.python_path = ROOT / self.python_path
        self.timeout, self.max_tokens = timeout, max_tokens
        self.transport = transport
        self.verify = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else True

    @classmethod
    def from_environment(cls):
        from app.environment import load_environment
        load_environment()
        return cls(
            mode=os.getenv("RAG_OCR_MODE", "disabled"),
            api_url=os.getenv("RAG_OCR_API_URL", ""),
            api_key=os.getenv("RAG_OCR_API_KEY", ""),
            model=os.getenv("RAG_OCR_MODEL", "Unlimited-OCR"),
            model_dir=os.getenv("RAG_OCR_MODEL_DIR", "models/unlimited-ocr"),
            python_path=os.getenv("RAG_OCR_PYTHON_PATH") or None,
            timeout=float(os.getenv("RAG_OCR_TIMEOUT", "1200")),
            max_tokens=int(os.getenv("RAG_OCR_MAX_TOKENS", "8192")),
            ca_bundle=os.getenv("RAG_OCR_CA_BUNDLE") or None,
        )

    def recognize(self, png):
        if not png or len(png) > 25 * 1024 * 1024:
            raise OCRFailure("OCR image is empty or exceeds the 25 MB request limit.")
        if self.mode == "disabled":
            raise OCRFailure("Unlimited-OCR is disabled; configure RAG_OCR_MODE=api or local for scanned pages.")
        if self.mode == "local":
            return self._local(png)
        return self._api(png)

    def _api(self, png):
        # Recheck DNS before each request; never use proxy environment variables or redirects.
        endpoint = internal_api_url(self.api_url)
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": [
                {"type": "text", "text": "<image>document parsing."},
                {"type": "image_url", "image_url": {
                    "url": "data:image/png;base64," + base64.b64encode(png).decode("ascii")}},
            ]}],
            "temperature": 0, "max_tokens": self.max_tokens, "stream": False,
            "skip_special_tokens": False, "vllm_xargs": {"ngram_size": 35, "window_size": 128},
        }
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            with httpx.Client(timeout=self.timeout, trust_env=False, follow_redirects=False,
                              transport=self.transport, verify=self.verify) as client:
                response = client.post(endpoint, json=payload, headers=headers)
            if response.status_code < 200 or response.status_code >= 300:
                raise OCRFailure(f"Unlimited-OCR internal API failed with HTTP {response.status_code}.")
            body = response.json()
            if body["choices"][0].get("finish_reason") == "length":
                raise OCRFailure("Unlimited-OCR output was truncated; increase the OCR token budget.")
            content = body["choices"][0]["message"]["content"]
            return clean_ocr_text(content)
        except httpx.TimeoutException as exc:
            raise OCRFailure("Unlimited-OCR internal API request timed out.") from exc
        except httpx.HTTPError as exc:
            raise OCRFailure("Unlimited-OCR internal API connection failed.") from exc
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise OCRFailure("Unlimited-OCR internal API returned a malformed response.") from exc

    def _local(self, png):
        validate_snapshot(self.model_dir)
        if not self.python_path.is_file():
            raise OCRFailure("Unlimited-OCR isolated Python is missing; prepare .venv-ocr with the CUDA requirements.")
        work = ROOT / "data/ocr-temp"
        work.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="rag-ocr-", dir=work) as directory:
            image, output = Path(directory) / "page.png", Path(directory) / "result.txt"
            image.write_bytes(png)
            env = os.environ.copy()
            for key in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE",
                        "HF_HUB_DISABLE_TELEMETRY"):
                env[key] = "1"
            try:
                completed = subprocess.run(
                    [str(self.python_path), str(ROOT / "scripts/run_ocr_server.py"),
                     "--model-dir", str(self.model_dir), "--input-image", str(image),
                     "--output-text", str(output)], cwd=ROOT, env=env, timeout=self.timeout,
                    capture_output=True, text=True, encoding="utf8", errors="replace",
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            except subprocess.TimeoutExpired as exc:
                raise OCRFailure("Unlimited-OCR local inference timed out.") from exc
            except OSError as exc:
                raise OCRFailure("Unlimited-OCR isolated Python could not start.") from exc
            if completed.returncode or not output.is_file():
                raise OCRFailure("Unlimited-OCR local inference failed; CUDA BF16 and the isolated pinned environment are required. Check the OCR service startup locally.")
            return clean_ocr_text(output.read_text(encoding="utf8"))


class LocalModelRuntime:
    """Used only by the separate OCR interpreter, never the RAG model environment."""
    def __init__(self, model_dir):
        self.model_dir = Path(model_dir)
        self.tokenizer = self.model = None

    def load(self):
        offline_environment()
        directory = validate_snapshot(self.model_dir)
        if self.model is not None:
            return
        try:
            import torch
            if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
                raise OCRFailure("Unlimited-OCR requires an NVIDIA CUDA GPU with BF16 support; CPU inference is unsupported.")
            import transformers
            if transformers.__version__ != "4.57.1":
                raise OCRFailure("Unlimited-OCR requires its isolated transformers==4.57.1 environment.")
            self.tokenizer = transformers.AutoTokenizer.from_pretrained(
                str(directory), trust_remote_code=True, local_files_only=True)
            self.model = transformers.AutoModel.from_pretrained(
                str(directory), trust_remote_code=True, local_files_only=True,
                use_safetensors=True, torch_dtype=torch.bfloat16, attn_implementation="eager").eval().cuda()
        except ImportError as exc:
            raise OCRFailure("Unlimited-OCR CUDA runtime dependencies are missing; install the isolated OCR requirements offline.") from exc

    def recognize(self, png):
        with _RUNTIME_LOCK:
            self.load()
            work = ROOT / "data/ocr-temp"
            work.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="unlimited-ocr-", dir=work) as directory:
                image = Path(directory) / "page.png"
                image.write_bytes(png)
                # eval_mode returns text before upstream visualization/bbox eval() paths.
                result = self.model.infer(
                    self.tokenizer, prompt="<image>document parsing.", image_file=str(image),
                    output_path=directory, base_size=1024, image_size=640, crop_mode=True,
                    max_length=32768, no_repeat_ngram_size=35, ngram_window=128,
                    eval_mode=True, save_results=False, temperature=0.0)
            return clean_ocr_text(result)
