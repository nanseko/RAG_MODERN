"""Extract the verified LibreOffice MSI into the project, without system installation."""
import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import struct
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MSI = ROOT / "vendor/services/LibreOffice_26.8.1_Win_x86-64.msi"
EXPECTED = "c6298b10cfa1748bcbedad94c62ce771e240e5021bfd6d7a967cd571b04b5312"

def safe_name(value):
    value = value.split(":", 1)[0].split("|")[-1]
    if not value or value in (".", "..") or any(c in value for c in "/\\:") or any(ord(c) < 32 for c in value):
        raise ValueError("Unsafe MSI target name")
    return value

def product_directory(key, rows):
    parts, seen = [], set()
    while key != "INSTALLLOCATION":
        if key not in rows:
            return None
        if key in seen:
            raise ValueError("Cyclic MSI directory")
        seen.add(key)
        parent, value = rows[key]
        if key == "TARGETDIR":
            return None
        target = value.split(":", 1)[0]
        if target != ".":
            parts.append(safe_name(target))
        key = parent
    return Path(*reversed(parts))

def _rows(database, statement, width):
    view = database.OpenView(statement)
    view.Execute(None)
    try:
        record = view.Fetch()
        while record:
            yield [record.GetString(i) for i in range(1, width + 1)]
            record = view.Fetch()
    finally:
        view.Close()

def extract_stream(msi, name, target):
    api = ctypes.WinDLL("msi")
    uint = ctypes.c_uint
    api.MsiOpenDatabaseW.argtypes = [ctypes.c_wchar_p, ctypes.c_void_p, ctypes.POINTER(uint)]
    api.MsiDatabaseOpenViewW.argtypes = [uint, ctypes.c_wchar_p, ctypes.POINTER(uint)]
    api.MsiViewExecute.argtypes = [uint, uint]
    api.MsiViewFetch.argtypes = [uint, ctypes.POINTER(uint)]
    api.MsiRecordReadStream.argtypes = [uint, uint, ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32)]
    api.MsiCloseHandle.argtypes = [uint]
    database, view, record = uint(), uint(), uint()
    def check(code):
        if code:
            raise OSError("Read-only MSI operation failed", code)
    try:
        check(api.MsiOpenDatabaseW(str(msi), None, ctypes.byref(database)))
        check(api.MsiDatabaseOpenViewW(database, "SELECT Data FROM _Streams WHERE Name='" + name + "'", ctypes.byref(view)))
        check(api.MsiViewExecute(view, 0))
        check(api.MsiViewFetch(view, ctypes.byref(record)))
        buffer = ctypes.create_string_buffer(1024 * 1024)
        with target.open("wb") as output:
            while True:
                size = ctypes.c_uint32(len(buffer))
                check(api.MsiRecordReadStream(record, 1, buffer, ctypes.byref(size)))
                if not size.value:
                    break
                output.write(buffer.raw[:size.value])
    finally:
        for handle in (record, view, database):
            if handle.value:
                api.MsiCloseHandle(handle)

def validate_cabinet(path, expected_ids):
    with path.open("rb") as stream:
        header = stream.read(36)
        if len(header) != 36 or header[:4] != b"MSCF":
            raise ValueError("Invalid MSI cabinet")
        offset = struct.unpack_from("<I", header, 16)[0]
        count = struct.unpack_from("<H", header, 28)[0]
        stream.seek(offset)
        for _ in range(count):
            record = stream.read(16)
            if len(record) != 16:
                raise ValueError("Truncated cabinet")
            name = bytearray()
            while True:
                byte = stream.read(1)
                if not byte or len(name) > 255:
                    raise ValueError("Invalid cabinet member")
                if byte == b"\0":
                    break
                name.extend(byte)
            value = name.decode("ascii")
            if value not in expected_ids or not re.fullmatch(r"[A-Za-z0-9_.]+", value) or value in (".", ".."):
                raise ValueError("Cabinet member outside MSI file table")

def extract(destination=ROOT / "vendor/runtime/libreoffice"):
    if os.name != "nt":
        raise ValueError("Windows extraction only")
    destination = Path(destination).resolve()
    allowed = (ROOT / "vendor/runtime").resolve()
    if not destination.is_relative_to(allowed) or destination == allowed:
        raise ValueError("LibreOffice target must be inside vendor/runtime")
    if destination.exists() and any(destination.iterdir()):
        raise ValueError("Existing runtime left unchanged")
    digest = hashlib.sha256()
    with MSI.open("rb") as source:
        for block in iter(lambda: source.read(8 * 1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != EXPECTED:
        raise ValueError("LibreOffice MSI checksum mismatch")
    import msilib
    database = msilib.OpenDatabase(str(MSI), 0)
    dirs = {key: (parent, name) for key, parent, name in _rows(database, "SELECT Directory,Directory_Parent,DefaultDir FROM Directory", 3)}
    components = {key: directory for key, directory in _rows(database, "SELECT Component,Directory_ FROM Component", 2)}
    files = list(_rows(database, "SELECT File,Component_,FileName FROM File", 3))
    ids = {row[0] for row in files}
    cabinets = [row[0] for row in _rows(database, "SELECT Cabinet FROM Media", 1)]
    allowed.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="libreoffice-extract-", dir=allowed) as temp:
        staging = Path(temp).resolve()
        if not staging.is_relative_to(allowed):
            raise ValueError("Extraction escaped project")
        flat = staging / "cabinet-files"
        flat.mkdir()
        for cabinet in cabinets:
            if not cabinet.startswith("#") or not re.fullmatch(r"#[A-Za-z0-9_.]+", cabinet):
                raise ValueError("External MSI cabinets are not accepted")
            cab = staging / cabinet[1:]
            extract_stream(MSI, cabinet[1:], cab)
            validate_cabinet(cab, ids)
            with (staging / "expand.log").open("ab") as output:
                subprocess.run(["expand.exe", "-F:*", str(cab), str(flat)], stdout=output,
                    stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, check=True, timeout=180,
                    creationflags=subprocess.CREATE_NO_WINDOW)
        product = staging / "product"
        product.mkdir()
        count = 0
        for file_id, component, filename in files:
            relative = product_directory(components[component], dirs)
            if relative is None:
                continue
            target = (product / relative / safe_name(filename)).resolve()
            if not target.is_relative_to(product):
                raise ValueError("MSI path escaped product")
            source = flat / file_id
            if not source.is_file():
                raise ValueError("Cabinet file is missing: " + file_id)
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            count += 1
        binary = product / "program/soffice.exe"
        if not binary.is_file():
            raise ValueError("LibreOffice executable missing")
        (product / "extraction.json").write_text(json.dumps({"source_sha256": EXPECTED, "files": count,
            "system_install": False}, indent=2), encoding="utf8")
        if destination.exists():
            destination.rmdir()  # verified empty above; never recursively remove an existing runtime
        product.replace(destination)
    return {"files": count, "soffice": str(destination / "program/soffice.exe"), "system_install": False}

def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    print(json.dumps(extract(), indent=2))

if __name__ == "__main__":
    main()
