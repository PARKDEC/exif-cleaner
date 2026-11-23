"""Detect known stealth PNGinfo formats without decompressing untrusted payloads."""

from dataclasses import dataclass
from itertools import islice

from PIL import Image

MAGIC_SIZE = len(b'stealth_pngcomp')
HEADER_SIZE = MAGIC_SIZE + 4


@dataclass(frozen=True)
class HiddenBlock:
    kind: str
    bits: int


def carrier_bits(image: Image.Image, kind: str):
    pixels = image.load()
    for x in range(image.width):
        for y in range(image.height):
            pixel = pixels[x, y]
            for channel in ((3,) if kind == 'alpha' else (0, 1, 2)):
                yield pixel[channel] & 1


def header_bytes(image: Image.Image, kind: str) -> bytes:
    bits = list(islice(carrier_bits(image, kind), HEADER_SIZE * 8))
    return bytes(sum(bits[i + j] << (7 - j) for j in range(8)) for i in range(0, len(bits) - 7, 8))


def find_hidden(image: Image.Image) -> tuple[HiddenBlock, ...]:
    rgba = image.convert('RGBA')
    blocks = []
    for kind, prefix in (('alpha', b'stealth_png'), ('rgb', b'stealth_rgb')):
        header = header_bytes(rgba, kind)
        if header[:MAGIC_SIZE] not in (prefix + b'info', prefix + b'comp'):
            continue
        if len(header) < HEADER_SIZE:
            raise ValueError('숨김 프롬프트의 길이 정보가 잘렸습니다.')
        payload_bits = int.from_bytes(header[MAGIC_SIZE:HEADER_SIZE], 'big')
        capacity = image.width * image.height * (1 if kind == 'alpha' else 3)
        total = HEADER_SIZE * 8 + payload_bits
        if payload_bits % 8 or total > capacity:
            raise ValueError('숨김 프롬프트의 길이 정보가 손상되었습니다.')
        blocks.append(HiddenBlock(kind, total))
    return tuple(blocks)


def erase_hidden(image: Image.Image, blocks: tuple[HiddenBlock, ...]) -> Image.Image:
    if image.mode not in ('RGB', 'RGBA'):
        raise ValueError('이 PNG 색상 모드의 숨김 정보는 안전하게 처리할 수 없습니다.')
    result = image.copy()
    pixels = result.load()
    for block in blocks:
        remaining = block.bits
        channels = (3,) if block.kind == 'alpha' else (0, 1, 2)
        for x in range(result.width):
            for y in range(result.height):
                values = list(pixels[x, y])
                for channel in channels:
                    if remaining:
                        # Restore opaque alpha values; RGB carriers change by at most 1.
                        values[channel] = (values[channel] | 1) if block.kind == 'alpha' else (values[channel] & 254)
                        remaining -= 1
                pixels[x, y] = tuple(values)
                if not remaining:
                    break
            if not remaining:
                break
    return result
