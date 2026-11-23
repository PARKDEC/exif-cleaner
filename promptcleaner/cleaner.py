"""Create verified copies. Original files are only ever opened for reading."""

import hashlib
import io
import os
import uuid
import warnings
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from .containers import (PNG_SIGNATURE, build_png, is_jpeg_prompt_part,
                         is_prompt_chunk, jpeg_parts, parse_png)
from .stealth import erase_hidden, find_hidden
from .recycle import RecycleError, move_to_recycle_bin
from .resolution import fingerprint

MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_PIXELS = 50_000_000


@dataclass(frozen=True)
class CleanResult:
    source: Path
    destination: Path
    changed: bool
    hidden_removed: tuple[str, ...]
    verified: bool = True


def open_checked(data: bytes) -> Image.Image:
    with warnings.catch_warnings():
        warnings.simplefilter('error', Image.DecompressionBombWarning)
        image = Image.open(io.BytesIO(data))
        if image.width * image.height > MAX_PIXELS:
            raise ValueError('5천만 픽셀보다 큰 이미지는 지원하지 않습니다.')
        if getattr(image, 'n_frames', 1) != 1:
            raise ValueError('애니메이션·다중 이미지 파일은 지원하지 않습니다.')
        if image.getexif().get(274, 1) not in (None, 1):
            raise ValueError('회전 정보가 있는 이미지입니다. 방향을 적용해 저장한 뒤 다시 시도해 주세요.')
        image.load()
        return image


def sanitize_png(data: bytes, image: Image.Image) -> tuple[bytes, tuple[str, ...]]:
    chunks, trailer = parse_png(data)
    if any(c.kind == b'acTL' for c in chunks):
        raise ValueError('애니메이션 PNG는 지원하지 않습니다.')
    kept = [c for c in chunks if not is_prompt_chunk(c)]
    blocks = find_hidden(image)
    if not blocks:
        return (data if len(kept) == len(chunks) and not trailer else build_png(kept)), ()

    if chunks[0].payload[8] != 8:
        raise ValueError('8비트 PNG의 숨김 정보만 안전하게 처리할 수 있습니다.')
    cleaned = erase_hidden(image, blocks)
    encoded = io.BytesIO()
    # Only use the new image header and compressed pixels. Preserve all other
    # retained chunks verbatim, including ICC, gamma, DPI and unrelated text.
    cleaned.info.clear()
    cleaned.save(encoded, format='PNG', compress_level=6)
    fresh, _ = parse_png(encoded.getvalue())
    new_header = next(c for c in fresh if c.kind == b'IHDR')
    new_pixels = [c for c in fresh if c.kind == b'IDAT']
    output = []
    inserted = False
    for c in kept:
        if c.kind == b'IHDR':
            output.append(new_header)
        elif c.kind == b'IDAT':
            if not inserted:
                output.extend(new_pixels)
                inserted = True
        else:
            output.append(c)
    return build_png(output), tuple(block.kind for block in blocks)


def verify_candidate(original: bytes, candidate: bytes, before: Image.Image,
                     hidden: tuple[str, ...]) -> None:
    with open_checked(candidate) as after:
        if after.format != before.format or after.size != before.size or after.mode != before.mode:
            raise ValueError('이미지 형식·크기·색상 모드 보존 검증에 실패했습니다.')
        if after.getexif():
            raise ValueError('EXIF 제거 검증에 실패했습니다.')
        if before.format == 'JPEG':
            old_parts, _ = jpeg_parts(original)
            new_parts, trailing = jpeg_parts(candidate)
            if trailing or any(is_jpeg_prompt_part(m) for m, _ in new_parts):
                raise ValueError('JPG 생성 정보 제거 검증에 실패했습니다.')
            if [(m, p) for m, p in old_parts if not is_jpeg_prompt_part(m)] != new_parts:
                raise ValueError('JPG 압축 데이터 보존 검증에 실패했습니다.')
        else:
            old_chunks, _ = parse_png(original)
            new_chunks, trailing = parse_png(candidate)
            if trailing or any(is_prompt_chunk(c) for c in new_chunks) or find_hidden(after):
                raise ValueError('PNG 프롬프트 제거 검증에 실패했습니다.')
            variable = {b'IHDR', b'IDAT'} if hidden else set()
            expected = [c.raw for c in old_chunks if not is_prompt_chunk(c) and c.kind not in variable]
            actual = [c.raw for c in new_chunks if c.kind not in variable]
            if expected != actual:
                raise ValueError('PNG 표시 정보·기타 메타데이터 보존 검증에 실패했습니다.')
        if not hidden:
            if before.tobytes() != after.tobytes():
                raise ValueError('픽셀 보존 검증에 실패했습니다.')
        else:
            channels = len(before.getbands())
            allowed = set()
            if 'alpha' in hidden:
                allowed.add(3)
            if 'rgb' in hidden:
                allowed.update((0, 1, 2))
            for channel in range(channels):
                a = before.getchannel(channel).tobytes()
                b = after.getchannel(channel).tobytes()
                if a != b and (channel not in allowed or any(abs(x - y) > 1 for x, y in zip(a, b))):
                    raise ValueError('숨김 정보 제거 중 허용 범위를 넘는 픽셀 변경이 감지되었습니다.')
            # RGB color-key transparency can change discontinuously when a
            # single RGB bit changes. Validate rendered alpha as well.
            before_alpha = before.convert('RGBA').getchannel('A').tobytes()
            after_alpha = after.convert('RGBA').getchannel('A').tobytes()
            if before_alpha != after_alpha and ('alpha' not in hidden or any(
                    abs(x - y) > 1 for x, y in zip(before_alpha, after_alpha))):
                raise ValueError('숨김 정보 제거 후 투명도 보존 검증에 실패했습니다.')


def clean_image(source: Path, destination: Path, *, overwrite_digest: str | None = None) -> CleanResult:
    source, destination = Path(source), Path(destination)
    if source.resolve() == destination.resolve():
        raise ValueError('원본 경로에는 저장할 수 없습니다.')
    if destination.exists() or destination.is_symlink():
        if overwrite_digest is None:
            raise FileExistsError('같은 이름의 결과 파일이 이미 있습니다.')
        if destination.is_symlink() or not destination.is_file() or source.samefile(destination):
            raise ValueError('원본이나 일반 파일이 아닌 대상에는 덮어쓸 수 없습니다.')
        if fingerprint(destination) != overwrite_digest:
            raise ValueError('확인 이후 기존 결과가 변경되어 덮어쓰기를 중지했습니다.')
    elif overwrite_digest is not None:
        raise ValueError('확인한 기존 결과 파일이 사라졌습니다. 다시 확인해 주세요.')
    if source.is_symlink() or not source.is_file():
        raise ValueError('일반 이미지 파일만 처리할 수 있습니다.')
    if source.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
        raise ValueError('PNG와 JPG 파일만 지원합니다.')
    if source.stat().st_size > MAX_FILE_BYTES:
        raise ValueError('256MB보다 큰 파일은 지원하지 않습니다.')
    with source.open('rb') as handle:
        data = handle.read(MAX_FILE_BYTES + 1)
    if len(data) > MAX_FILE_BYTES:
        raise ValueError('256MB보다 큰 파일은 지원하지 않습니다.')
    if data.startswith(PNG_SIGNATURE):
        parse_png(data)
    original_hash = hashlib.sha256(data).digest()
    with open_checked(data) as image:
        expected_format = 'PNG' if source.suffix.lower() == '.png' else 'JPEG'
        if image.format != expected_format:
            raise ValueError('파일 확장자와 실제 이미지 형식이 다릅니다.')
        hidden = ()
        if data.startswith(PNG_SIGNATURE):
            candidate, hidden = sanitize_png(data, image)
        else:
            parts, _ = jpeg_parts(data)
            candidate = b''.join(p for m, p in parts if not is_jpeg_prompt_part(m))
        verify_candidate(data, candidate, image, hidden)

    with source.open('rb') as handle:
        if hashlib.file_digest(handle, 'sha256').digest() != original_hash:
            raise ValueError('처리 도중 원본 파일이 변경되었습니다. 다시 시도해 주세요.')
    destination.parent.mkdir(parents=True, exist_ok=True)
    pending_folder = destination.parent / '_processing'
    pending_folder.mkdir(exist_ok=True)
    pending = pending_folder / (uuid.uuid4().hex + '.pending')
    # Failed writes remain under _processing; never expose partial ex_ images.
    with pending.open('xb') as handle:
        handle.write(candidate)
        handle.flush()
        os.fsync(handle.fileno())
    with pending.open('rb') as handle:
        if hashlib.file_digest(handle, 'sha256').digest() != hashlib.sha256(candidate).digest():
            raise ValueError('저장된 사본의 무결성 검증에 실패했습니다.')
    recycled = False
    if overwrite_digest is not None:
        if fingerprint(destination) != overwrite_digest:
            raise ValueError('확인 이후 기존 결과가 변경되어 덮어쓰기를 중지했습니다.')
        move_to_recycle_bin(destination, overwrite_digest)
        recycled = True
    try:
        publish_candidate(pending, destination, candidate)
    except OSError as exc:
        if recycled:
            raise RecycleError(f'기존 결과는 휴지통으로 이동되어 복구 가능합니다: {destination.resolve()}. '
                               f'새 결과 게시에 실패하여 작업을 중지했습니다. 검증된 사본: {pending}') from exc
        raise
    return CleanResult(source, destination, candidate != data, hidden)


def publish_candidate(pending: Path, destination: Path, candidate: bytes) -> None:
    if os.name == 'nt':
        # Windows rename fails rather than replacing an existing destination.
        pending.rename(destination)
    else:
        # Exclusive creation on other systems, without deleting staging files.
        with destination.open('xb') as handle:
            handle.write(candidate)
