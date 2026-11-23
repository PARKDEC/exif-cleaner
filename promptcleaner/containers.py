"""Edit metadata containers while retaining compressed image data."""

import struct
import zlib
from dataclasses import dataclass

PNG_SIGNATURE = b'\x89PNG\r\n\x1a\n'
TEXT_TYPES = {b'tEXt', b'zTXt', b'iTXt'}
GENERATION_KEYS = {
    'description', 'comment', 'parameters', 'prompt', 'negative_prompt',
    'negative prompt', 'workflow', 'generation data', 'generation parameters',
    'xml:com.adobe.xmp', 'raw profile type exif', 'raw profile type xmp',
    'raw profile type iptc', 'invokeai_metadata', 'invokeai_graph',
    'nai', 'novelai', 'usercomment', 'exif', 'xmp',
}


@dataclass(frozen=True)
class PngChunk:
    kind: bytes
    payload: bytes
    raw: bytes


def parse_png(data: bytes) -> tuple[list[PngChunk], bytes]:
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError('올바른 PNG 파일이 아닙니다.')
    chunks = []
    offset = len(PNG_SIGNATURE)
    while offset + 12 <= len(data):
        size = struct.unpack_from('>I', data, offset)[0]
        end = offset + 12 + size
        if end > len(data):
            raise ValueError('PNG 데이터가 잘렸습니다.')
        kind = data[offset + 4:offset + 8]
        payload = data[offset + 8:end - 4]
        expected = struct.unpack_from('>I', data, end - 4)[0]
        if zlib.crc32(kind + payload) != expected:
            raise ValueError('PNG 손상 검사(CRC)에 실패했습니다.')
        chunks.append(PngChunk(kind, payload, data[offset:end]))
        offset = end
        if kind == b'IEND':
            if size or chunks[0].kind != b'IHDR' or not any(c.kind == b'IDAT' for c in chunks):
                raise ValueError('PNG 구조가 올바르지 않습니다.')
            return chunks, data[end:]
    raise ValueError('PNG 종료 정보를 찾지 못했습니다.')


def is_prompt_chunk(chunk: PngChunk) -> bool:
    if chunk.kind == b'eXIf':
        return True
    if chunk.kind not in TEXT_TYPES:
        return False
    key = chunk.payload.split(b'\x00', 1)[0].decode('latin1').strip().lower()
    return key in GENERATION_KEYS or 'prompt' in key


def build_png(chunks: list[PngChunk]) -> bytes:
    return PNG_SIGNATURE + b''.join(c.raw for c in chunks)


def jpeg_parts(data: bytes) -> tuple[list[tuple[int, bytes]], bytes]:
    """Include entropy scans as marker 0; handle progressive scans and stuffing."""
    if not data.startswith(b'\xff\xd8'):
        raise ValueError('올바른 JPG 파일이 아닙니다.')
    parts = [(0xD8, data[:2])]
    offset = 2
    while offset < len(data):
        start = offset
        if data[offset] != 0xFF:
            raise ValueError('JPG 마커가 손상되었습니다.')
        while offset < len(data) and data[offset] == 0xFF:
            offset += 1
        if offset >= len(data):
            break
        marker = data[offset]
        offset += 1
        if marker == 0xD9:
            parts.append((marker, data[start:offset]))
            return parts, data[offset:]
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            parts.append((marker, data[start:offset]))
            continue
        if marker in (0, 0xD8) or offset + 2 > len(data):
            raise ValueError('JPG 구조가 올바르지 않습니다.')
        length = struct.unpack_from('>H', data, offset)[0]
        if length < 2 or offset + length > len(data):
            raise ValueError('JPG 데이터가 잘렸습니다.')
        offset += length
        parts.append((marker, data[start:offset]))
        if marker == 0xDA:
            scan_start = offset
            while offset < len(data):
                found = data.find(b'\xff', offset)
                if found < 0 or found + 1 >= len(data):
                    raise ValueError('JPG 이미지 데이터가 잘렸습니다.')
                next_byte = data[found + 1]
                if next_byte == 0 or 0xD0 <= next_byte <= 0xD7:
                    offset = found + 2
                    continue
                if next_byte == 0xFF:
                    offset = found + 1
                    continue
                parts.append((0, data[scan_start:found]))
                offset = found
                break
    raise ValueError('JPG 종료 정보를 찾지 못했습니다.')


def is_jpeg_prompt_part(marker: int) -> bool:
    # APP1: EXIF/XMP; APP13: IPTC descriptions; COM: arbitrary comments.
    return marker in (0xE1, 0xED, 0xFE)
