"""Process a single image or a folder and write a prompt-free CSV report."""

import csv
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable

from .cleaner import clean_image
from .recycle import RecycleError
from .resolution import ConflictChoice, fingerprint, validate_name


@dataclass(frozen=True)
class FileResult:
    name: str
    success: bool
    detail: str
    skipped: bool = False
    destination: Path | None = None


@dataclass(frozen=True)
class FileConflict:
    source: Path
    destination: Path
    digest: str | None = None


@dataclass
class BatchResult:
    output_folder: Path | None
    total: int
    files: list[FileResult] = field(default_factory=list)
    cancelled: bool = False
    report_error: str = ''
    safety_stopped: bool = False

    @property
    def success(self):
        return sum(row.success for row in self.files)

    @property
    def failed(self):
        return sum(not row.success and not row.skipped for row in self.files)

    @property
    def skipped(self):
        return sum(row.skipped for row in self.files)


def collect_images(source: Path) -> list[Path]:
    source = Path(source)
    if source.is_file():
        if source.is_symlink():
            raise ValueError('바로 가기 대신 원본 이미지 파일을 선택해 주세요.')
        if source.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
            raise ValueError('PNG와 JPG 이미지 파일만 지원합니다.')
        return [source]
    if not source.is_dir():
        raise ValueError('이미지 파일이나 폴더를 찾을 수 없습니다.')
    return sorted((p for p in source.iterdir() if not p.is_symlink() and p.is_file()
                   and p.suffix.lower() in ('.png', '.jpg', '.jpeg')), key=lambda p: p.name.casefold())


def find_conflicts(source: Path, output_folder: Path) -> tuple[FileConflict, ...]:
    conflicts = []
    for image in collect_images(Path(source)):
        target = Path(output_folder) / ('ex_' + image.name)
        if target.exists() or target.is_symlink():
            try:
                digest = fingerprint(target)
            except OSError:
                # Rename/skip remain possible without permission to read the
                # old file. Overwrite stays disabled without a confirmed hash.
                digest = None
            conflicts.append(FileConflict(image, target, digest))
    return tuple(conflicts)


def make_output_folder(source: Path) -> Path:
    base = 'prompt_cleaned_' + datetime.now().strftime('%Y%m%d_%H%M%S')
    for index in range(10000):
        target = source / (base if index == 0 else f'{base}_{index}')
        try:
            target.mkdir()
            return target
        except FileExistsError:
            continue
    raise FileExistsError('새 결과 폴더를 만들 수 없습니다.')


def run_input(source: Path,
               on_progress: Callable[[int, int, FileResult | None], None] | None = None,
               cancel_event: threading.Event | None = None,
               *, output_folder: Path | None = None,
               resolutions: dict[str, ConflictChoice] | None = None) -> BatchResult:
    source = Path(source)
    if source.is_symlink() and source.is_file():
        raise ValueError('바로 가기 대신 원본 이미지 파일을 선택해 주세요.')
    source = source.resolve()
    images = collect_images(source)
    original_folder = source if source.is_dir() else source.parent
    if output_folder is not None:
        output_folder = Path(output_folder).resolve()
        if not output_folder.is_dir():
            raise ValueError('지정한 저장 폴더를 찾을 수 없습니다. 저장 위치를 다시 선택해 주세요.')
        if output_folder == original_folder:
            raise ValueError('원본 폴더와 다른 저장 폴더를 선택해 주세요.')
    result = BatchResult(None, len(images))
    if on_progress:
        on_progress(0, result.total, None)
    if not images:
        return result
    result.output_folder = output_folder if output_folder is not None else make_output_folder(original_folder)
    resolutions = resolutions or {}
    reserved = {('ex_' + image.name).casefold() for image in images
                if resolutions.get(image.name, ConflictChoice('skip')).action != 'rename'}
    for image in images:
        if cancel_event and cancel_event.is_set():
            result.cancelled = True
            break
        choice = resolutions.get(image.name)
        if choice is not None and choice.action == 'skip':
            row = FileResult(image.name, False, '중복 파일 · 사용자가 제외하고 계속 선택', skipped=True)
            result.files.append(row)
            if on_progress:
                on_progress(len(result.files), result.total, row)
            continue
        try:
            target = result.output_folder / ('ex_' + image.name)
            if choice is not None and choice.action == 'rename':
                name = validate_name(choice.name or '', image.suffix)
                if name.casefold() in reserved:
                    raise ValueError('다른 결과와 저장할 이름이 겹칩니다.')
                reserved.add(name.casefold())
                target = result.output_folder / name
            if choice is not None and choice.action == 'overwrite':
                if not choice.digest:
                    raise ValueError('덮어쓰기 대상의 확인 정보가 없습니다.')
                cleaned = clean_image(image, target, overwrite_digest=choice.digest)
            elif choice is None or choice.action == 'rename':
                cleaned = clean_image(image, target)
            else:
                raise ValueError('알 수 없는 중복 처리 선택입니다.')
            detail = '검증 완료 · ' + ('숨김 정보 포함 제거' if cleaned.hidden_removed else '생성 정보 제거' if cleaned.changed else '제거할 생성 정보 없음')
            if choice is not None and choice.action == 'overwrite':
                detail += f' · 기존 결과 휴지통 이동 완료(복구 가능): {target.resolve()}'
            row = FileResult(image.name, True, detail, destination=target)
        except Exception as exc:
            detail = str(exc) if isinstance(exc, (ValueError, OSError)) else '처리 오류: ' + str(exc)
            row = FileResult(image.name, False, detail)
            if isinstance(exc, RecycleError):
                result.safety_stopped = True
        result.files.append(row)
        if on_progress:
            on_progress(len(result.files), result.total, row)
        if result.safety_stopped:
            break
    try:
        write_report(result.output_folder, result.files)
    except OSError as exc:
        result.report_error = str(exc)
    return result


def write_report(folder: Path, files: list[FileResult]) -> None:
    for index in range(10000):
        name = '처리결과.csv' if index == 0 else f'처리결과_{index}.csv'
        try:
            handle = (folder / name).open('x', encoding='utf-8-sig', newline='')
            break
        except FileExistsError:
            continue
    else:
        raise FileExistsError('새 처리결과 보고서를 만들 수 없습니다.')
    with handle:
        writer = csv.writer(handle)
        writer.writerow(['파일명', '결과', '설명', '저장 파일'])
        for row in files:
            # Prevent spreadsheet formula execution from an image filename.
            name = "'" + row.name if row.name.startswith(('=', '+', '-', '@', '\t', '\r')) else row.name
            status = '건너뜀' if row.skipped else '성공' if row.success else '실패'
            writer.writerow([name, status, row.detail, str(row.destination or '')])
