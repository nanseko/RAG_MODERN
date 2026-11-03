"""Use a local, installed LibreOffice converter for legacy DOC/PPT and HWP fallback."""
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

GUIDE = "LibreOffice(soffice)가 필요합니다. 설치 후 RAG_SOFFICE 경로를 설정하세요. HWP 지원은 변환기 버전에 따라 다릅니다."


def _binary(binary):
    candidate = os.getenv("RAG_SOFFICE", binary)
    if "/" in candidate or "\\" in candidate:
        path = Path(candidate)
        path = path if path.is_absolute() else ROOT / path
        return str(path) if path.is_file() else None
    found = shutil.which(candidate)
    if found:
        return found
    if candidate != "soffice":
        return None
    for name in ("soffice.com", "soffice.exe"):
        bundled = ROOT / "vendor/runtime/libreoffice/program" / name
        if bundled.is_file():
            return str(bundled)
    for env in ("ProgramFiles", "ProgramFiles(x86)"):
        root = os.getenv(env)
        if root:
            p = Path(root) / "LibreOffice/program/soffice.exe"
            if p.is_file():
                return str(p)
    return None


def convert_with_soffice(path, binary="soffice", out_dir=""):
    exe = _binary(binary)
    if exe is None:
        raise ValueError(GUIDE)
    out_dir = out_dir or tempfile.mkdtemp(prefix="ragconv")
    Path(out_dir).mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="rag-lo-profile-") as profile:
        try:
            result = subprocess.run(
                [exe, "-env:UserInstallation=" + Path(profile).as_uri(),
                 "--headless", "--convert-to", "pdf", "--outdir", str(Path(out_dir).resolve()),
                 str(Path(path).resolve())],
                capture_output=True, text=True, timeout=300,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except subprocess.TimeoutExpired as e:
            raise ValueError(f"변환 시간 초과: {Path(path).name}") from e
    if result.returncode:
        raise ValueError(f"변환 실패: {(result.stderr or result.stdout)[-300:]}")
    pdf = Path(out_dir) / (Path(path).stem + ".pdf")
    if not pdf.is_file():
        raise ValueError(f"변환 결과 없음: {Path(path).name}")
    return str(pdf)
