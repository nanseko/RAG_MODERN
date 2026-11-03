"""HWP 5 native paragraph text. Images, equations and protected variants need conversion."""
import struct
import zlib
from pathlib import Path


def _paragraph_text(raw):
    # HWP paragraph controls occupy either one UTF-16 unit or an 8-unit block.
    units = struct.unpack("<" + "H" * (len(raw) // 2), raw[:len(raw) // 2 * 2])
    output = bytearray()
    i = 0
    while i < len(units):
        code = units[i]
        if code in (9, 10, 13):
            output.extend(("\t" if code == 9 else "\n").encode("utf-16le"))
        elif code >= 32:
            output.extend(struct.pack("<H", code))
        if code in set(range(1, 10)) | {11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}:
            i += 8
        else:
            i += 1
    return output.decode("utf-16le", errors="replace").strip()


def parse_hwp(path):
    import olefile
    if not olefile.isOleFile(path):
        raise ValueError("HWP 5 OLE 문서가 아닙니다")
    with olefile.OleFileIO(path) as ole:
        if not ole.exists("FileHeader"):
            raise ValueError("HWP FileHeader가 없습니다")
        header = ole.openstream("FileHeader").read()
        if len(header) < 40 or not header.startswith(b"HWP Document File"):
            raise ValueError("유효한 HWP 5 헤더가 아닙니다")
        flags = struct.unpack_from("<I", header, 36)[0]
        if flags & 6:
            raise ValueError("암호화/배포용 HWP는 승인된 변환기가 필요합니다")
        streams = sorted((s for s in ole.listdir() if len(s) == 2 and s[0] == "BodyText" and s[1].startswith("Section")),
                         key=lambda s: int(s[1][7:]))
        sections = []
        for stream in streams:
            data = ole.openstream(stream).read()
            if flags & 1:
                try:
                    data = zlib.decompress(data, -15)
                except zlib.error as e:
                    raise ValueError("HWP 압축 본문 오류") from e
            offset, paragraphs = 0, []
            while offset < len(data):
                if offset + 4 > len(data):
                    raise ValueError("HWP record header truncated")
                record = struct.unpack_from("<I", data, offset)[0]
                offset += 4
                tag, size = record & 0x3FF, (record >> 20) & 0xFFF
                if size == 0xFFF:
                    if offset + 4 > len(data):
                        raise ValueError("HWP record size truncated")
                    size = struct.unpack_from("<I", data, offset)[0]
                    offset += 4
                if offset + size > len(data):
                    raise ValueError("HWP record data truncated")
                if tag == 67:  # HWPTAG_PARA_TEXT = HWPTAG_BEGIN(16) + 51
                    paragraphs.append(_paragraph_text(data[offset:offset + size]))
                offset += size
            sections.append({"title": Path(path).name, "heading_path": "/".join(stream),
                             "page_number": None, "text": "\n".join(paragraphs)})
        if not sections:
            raise ValueError("HWP BodyText가 없습니다")
        return sections
