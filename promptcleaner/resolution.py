"""Explicit choices for existing result files."""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class ConflictChoice:
    action: Literal['overwrite', 'rename', 'skip']
    name: str | None = None
    digest: str | None = None


def fingerprint(path: Path) -> str | None:
    if path.is_symlink() or not path.is_file():
        return None
    with path.open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def validate_name(name: str, extension: str) -> str:
    name = name.strip()
    if not name or name in ('.', '..') or re.search(r'[<>:"/\\|?*\x00-\x1f]', name) or name.endswith(('.', ' ')):
        raise ValueError('경로 없이 올바른 파일 이름만 입력해 주세요.')
    if not Path(name).suffix:
        name += extension
    if Path(name).suffix.lower() != extension.lower():
        raise ValueError('원본과 같은 확장자를 사용해 주세요.')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if name.split('.')[0].upper() in reserved or len(name) > 240:
        raise ValueError('Windows에서 사용할 수 없는 파일 이름입니다.')
    return name


def available_name(folder: Path, original: str, reserved: set[str] | None = None) -> str:
    path = Path(original)
    reserved = reserved or set()
    for index in range(1, 10000):
        name = f'{path.stem}_{index}{path.suffix}'
        target = folder / name
        if name.casefold() not in reserved and not target.exists() and not target.is_symlink():
            return name
    raise ValueError('사용 가능한 새 파일 이름을 만들 수 없습니다.')
