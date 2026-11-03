"""Serve the pinned CUDA OCR model, or infer one local PNG in an isolated environment."""
import argparse
import base64
import binascii
import hmac
import io
import ipaddress
import os
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ingestion.ocr import LocalModelRuntime, OCRFailure, clean_ocr_text

MAX_IMAGE_BYTES = 25 * 1024 * 1024


def create_app(runtime, api_key=""):
    from fastapi import FastAPI, Header, HTTPException
    app = FastAPI(title="Internal Unlimited-OCR", docs_url=None, redoc_url=None)

    def authenticated(authorization):
        if api_key and not hmac.compare_digest(authorization or "", "Bearer " + api_key):
            raise HTTPException(status_code=401, detail="Invalid OCR API key.")

    @app.get("/health")
    def health(authorization: str | None = Header(default=None)):
        authenticated(authorization)
        return {"status": "ready", "model": "Unlimited-OCR", "runtime": "cuda-bf16"}

    @app.get("/v1/models")
    def models(authorization: str | None = Header(default=None)):
        authenticated(authorization)
        return {"object": "list", "data": [{"id": "Unlimited-OCR", "object": "model",
                                           "owned_by": "internal"}]}

    @app.post("/v1/chat/completions")
    def completions(payload: dict, authorization: str | None = Header(default=None)):
        authenticated(authorization)
        try:
            if payload.get("stream") or payload.get("model") not in ("Unlimited-OCR", "baidu/Unlimited-OCR"):
                raise ValueError("Only non-streaming Unlimited-OCR requests are supported.")
            messages = payload["messages"]
            if len(messages) != 1 or messages[0]["role"] != "user":
                raise ValueError("One user message is required.")
            content = messages[0]["content"]
            if len(content) != 2 or content[0] != {"type": "text", "text": "<image>document parsing."}:
                raise ValueError("The official single-page document parsing prompt is required.")
            if content[1]["type"] != "image_url":
                raise ValueError("One local image data URI is required.")
            uri = content[1]["image_url"]["url"]
            prefix = "data:image/png;base64,"
            if not isinstance(uri, str) or not uri.startswith(prefix) or len(uri) > MAX_IMAGE_BYTES * 4 // 3 + 100:
                raise ValueError("Only local PNG data URIs up to 25 MB are accepted.")
            png = base64.b64decode(uri[len(prefix):], validate=True)
            if not png or len(png) > MAX_IMAGE_BYTES:
                raise ValueError("Image size is invalid.")
            from PIL import Image
            with Image.open(io.BytesIO(png)) as image:
                if image.format != "PNG":
                    raise ValueError("Image must be a PNG.")
                image.verify()
        except (KeyError, TypeError, ValueError, IndexError, binascii.Error, OSError) as exc:
            raise HTTPException(status_code=400, detail="Invalid single-page OCR request.") from exc
        try:
            text = clean_ocr_text(runtime.recognize(png))
        except OCRFailure as exc:
            raise HTTPException(status_code=503, detail="OCR model failed to produce complete text.") from exc
        return {"id": "ocr-" + uuid.uuid4().hex, "object": "chat.completion",
            "model": "Unlimited-OCR", "choices": [{"index": 0, "message": {
                "role": "assistant", "content": text}, "finish_reason": "stop"}]}
    return app


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-dir", default=str(ROOT / "models/unlimited-ocr"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=10000)
    parser.add_argument("--input-image")
    parser.add_argument("--output-text")
    args = parser.parse_args()
    runtime = LocalModelRuntime(args.model_dir)
    if args.input_image:
        if not args.output_text:
            parser.error("--output-text is required with --input-image")
        text = runtime.recognize(Path(args.input_image).read_bytes())
        Path(args.output_text).write_text(text, encoding="utf8")
        return
    key = os.getenv("RAG_OCR_SERVER_KEY", "")
    try:
        local = ipaddress.ip_address(args.host).is_loopback
    except ValueError:
        local = args.host.lower() == "localhost"
    if not local and not key:
        parser.error("RAG_OCR_SERVER_KEY is required when listening outside loopback")
    runtime.load()  # Startup fails visibly before health can claim model readiness.
    import uvicorn
    uvicorn.run(create_app(runtime, api_key=key), host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    try:
        main()
    except OCRFailure as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
