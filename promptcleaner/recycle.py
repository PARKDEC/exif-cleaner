"""Recoverable replacement using the user's prescribed Windows helper."""

import base64
import json
import os
import subprocess
from pathlib import Path

from .resolution import fingerprint

HELPER = Path.home() / '.codex' / 'Move-ToRecycleBin.ps1'
POWERSHELL = Path(os.environ.get('SystemRoot', r'C:\Windows')) / 'System32' / 'WindowsPowerShell' / 'v1.0' / 'powershell.exe'


class RecycleError(OSError):
    pass


def move_to_recycle_bin(target: Path, expected_digest: str) -> None:
    if os.name != 'nt':
        raise RecycleError('이 환경에서 휴지통 덮어쓰기를 지원하지 않습니다.')
    raw = Path(target)
    if raw.is_symlink() or not raw.is_file():
        raise RecycleError('일반 결과 파일만 휴지통으로 보낼 수 있습니다.')
    target = raw.resolve(strict=True)
    if target == Path(target.anchor) or target == Path.home().resolve() or target.suffix.lower() not in ('.png', '.jpg', '.jpeg'):
        raise RecycleError('보호 대상 경로에는 덮어쓸 수 없습니다.')
    if target.drive.startswith('\\') or not HELPER.is_file() or not POWERSHELL.is_file():
        raise RecycleError('휴지통을 지원하는 로컬 경로와 지정된 휴지통 도구가 필요합니다.')
    if fingerprint(target) != expected_digest:
        raise RecycleError('확인 이후 기존 결과가 변경되어 덮어쓰기를 중지했습니다.')
    def quote(value):
        return "'" + str(value).replace("'", "''") + "'"
    command = (
        "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); "
        "$env:PSModulePath=[System.IO.Path]::Combine($PSHOME,'Modules'); "
        f'$target={quote(target)}; '
        "if ([System.IO.DriveInfo]::new([System.IO.Path]::GetPathRoot($target)).DriveType -ne 'Fixed') "
        "{ throw 'Recycle requires a local fixed drive.' }; "
        f"if ((Get-FileHash -LiteralPath $target -Algorithm SHA256).Hash -ne {quote(expected_digest)}) "
        "{ throw 'Existing result changed after confirmation.' }; "
        f'& {quote(HELPER)} -LiteralPath $target -Confirm:$false | ConvertTo-Json -Compress'
    )
    encoded = base64.b64encode(command.encode('utf-16-le')).decode('ascii')
    try:
        completed = subprocess.run([str(POWERSHELL), '-NoProfile', '-NonInteractive', '-OutputFormat', 'Text',
                                    '-EncodedCommand', encoded],
                                   capture_output=True, text=True, encoding='utf-8', errors='replace',
                                   creationflags=subprocess.CREATE_NO_WINDOW, timeout=60)
        if completed.returncode:
            raise RecycleError('휴지통 이동 실패: ' + completed.stderr.strip())
        result = json.loads(completed.stdout.lstrip('\ufeff').strip())
        if not isinstance(result, dict):
            raise RecycleError('휴지통 이동 결과를 확인하지 못했습니다.')
        if (result.get('Status') != 'MovedToRecycleBin' or result.get('Recoverable') is not True
                or Path(result.get('Path', '')).resolve() != target):
            raise RecycleError('휴지통 이동 완료를 확인하지 못했습니다.')
        if target.exists():
            raise RecycleError(f'기존 결과는 휴지통으로 이동되어 복구 가능합니다: {target}. '
                               '같은 경로에 다른 파일이 생겨 새 결과 저장을 중지했습니다.')
    except subprocess.TimeoutExpired as exc:
        raise RecycleError(f'휴지통 작업이 시간 내 완료되지 않아 중지했습니다. 대상 경로와 휴지통을 확인해 주세요: {target}') from exc
    except (OSError, ValueError, TypeError) as exc:
        if isinstance(exc, RecycleError):
            raise
        raise RecycleError('휴지통 이동을 완료하지 못했습니다: ' + str(exc)) from exc
