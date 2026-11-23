import hashlib

import pytest
from PIL import Image

from test_cleaner import png_bytes, sandbox
from promptcleaner.batch import find_conflicts, run_input
from promptcleaner.resolution import ConflictChoice, available_name, validate_name
from promptcleaner.cleaner import clean_image
from promptcleaner.recycle import RecycleError


def inputs(sandbox):
    source, destination = sandbox / 'input', sandbox / 'output'
    source.mkdir()
    destination.mkdir()
    original = source / 'a.png'
    original.write_bytes(png_bytes(Image.new('RGB', (16, 12), 'red')))
    existing = destination / 'ex_a.png'
    existing.write_bytes(png_bytes(Image.new('RGB', (16, 12), 'blue')))
    return source, destination, original, existing


def test_rename_preserves_existing_result_and_saves_requested_name(sandbox):
    source, destination, original, existing = inputs(sandbox)
    old = existing.read_bytes()
    result = run_input(source, output_folder=destination,
                       resolutions={'a.png': ConflictChoice('rename', name='my_copy.png')})
    assert result.success == 1
    assert existing.read_bytes() == old
    assert (destination / 'my_copy.png').read_bytes() == original.read_bytes()
    assert result.files[0].destination == destination / 'my_copy.png'


def test_overwrite_recycles_only_after_candidate_is_verified(sandbox, monkeypatch):
    from promptcleaner import cleaner
    source, destination, original, existing = inputs(sandbox)
    old, before = existing.read_bytes(), original.read_bytes()
    digest = find_conflicts(source, destination)[0].digest
    recycled = []
    def recycle(path, expected_digest):
        assert list((destination / '_processing').glob('*.pending'))
        assert hashlib.sha256(path.read_bytes()).hexdigest() == expected_digest
        backup = sandbox / 'recoverable-test-backup.png'
        path.rename(backup)
        recycled.append(backup)
    monkeypatch.setattr(cleaner, 'move_to_recycle_bin', recycle)
    result = run_input(source, output_folder=destination,
                       resolutions={'a.png': ConflictChoice('overwrite', digest=digest)})
    assert result.success == 1
    assert recycled[0].read_bytes() == old
    assert original.read_bytes() == before
    assert existing.read_bytes() == original.read_bytes()
    assert '복구 가능' in result.files[0].detail


def test_invalid_candidate_never_recycles_existing_file(sandbox, monkeypatch):
    from promptcleaner import cleaner
    source, destination, original, existing = inputs(sandbox)
    original.write_bytes(b'broken png')
    old = existing.read_bytes()
    monkeypatch.setattr(cleaner, 'move_to_recycle_bin', lambda *args: pytest.fail('Unexpected recycle'))
    with pytest.raises(Exception):
        clean_image(original, existing, overwrite_digest=hashlib.sha256(old).hexdigest())
    assert existing.read_bytes() == old


def test_changed_existing_result_is_not_overwritten(sandbox, monkeypatch):
    from promptcleaner import cleaner
    source, destination, original, existing = inputs(sandbox)
    digest = find_conflicts(source, destination)[0].digest
    changed = png_bytes(Image.new('RGB', (16, 12), 'green'))
    existing.write_bytes(changed)
    monkeypatch.setattr(cleaner, 'move_to_recycle_bin', lambda *args: pytest.fail('Unexpected recycle'))
    result = run_input(source, output_folder=destination,
                       resolutions={'a.png': ConflictChoice('overwrite', digest=digest)})
    assert result.failed == 1
    assert existing.read_bytes() == changed


def test_recycle_failure_stops_batch_without_deleting_anything(sandbox, monkeypatch):
    from promptcleaner import cleaner
    source, destination, original, existing = inputs(sandbox)
    (source / 'b.png').write_bytes(original.read_bytes())
    old = existing.read_bytes()
    def fail(*args):
        raise RecycleError('휴지통 작업 실패')
    monkeypatch.setattr(cleaner, 'move_to_recycle_bin', fail)
    digest = find_conflicts(source, destination)[0].digest
    result = run_input(source, output_folder=destination,
                       resolutions={'a.png': ConflictChoice('overwrite', digest=digest)})
    assert result.safety_stopped
    assert result.failed == 1
    assert existing.read_bytes() == old
    assert not (destination / 'ex_b.png').exists()


def test_rename_validation_and_automatic_name_collision_avoidance(sandbox):
    (sandbox / 'ex_a.png').write_bytes(b'existing')
    (sandbox / 'ex_a_1.png').write_bytes(b'existing')
    assert available_name(sandbox, 'ex_a.png') == 'ex_a_2.png'
    assert validate_name('새 이름', '.png') == '새 이름.png'
    for bad in ('../else.png', 'a/b.png', 'a\\b.png', 'CON.png', 'a.jpg', 'a?.png'):
        with pytest.raises(ValueError):
            validate_name(bad, '.png')


def test_publish_error_reports_recoverable_old_result_and_retains_candidate(sandbox, monkeypatch):
    from promptcleaner import cleaner
    source, destination, original, existing = inputs(sandbox)
    old = existing.read_bytes()
    backup = sandbox / 'recoverable-old.png'
    monkeypatch.setattr(cleaner, 'move_to_recycle_bin', lambda path, digest: path.rename(backup))
    def fail(*args):
        raise OSError('test publication failure')
    monkeypatch.setattr(cleaner, 'publish_candidate', fail)
    with pytest.raises(RecycleError, match='복구 가능'):
        clean_image(original, existing, overwrite_digest=hashlib.sha256(old).hexdigest())
    assert backup.read_bytes() == old
    assert list((destination / '_processing').glob('*.pending'))


def test_recycle_helper_failure_has_no_permanent_delete_fallback(sandbox, monkeypatch):
    import subprocess
    from promptcleaner import recycle
    target = sandbox / 'ex_a.png'
    target.write_bytes(b'keep existing data')
    before = target.read_bytes()
    monkeypatch.setattr(recycle.subprocess, 'run', lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 1, stdout='', stderr='helper failed'))
    with pytest.raises(RecycleError, match='휴지통 이동 실패'):
        recycle.move_to_recycle_bin(target, hashlib.sha256(before).hexdigest())
    assert target.read_bytes() == before


def test_recycle_refuses_changed_target_before_launching_helper(sandbox, monkeypatch):
    from promptcleaner import recycle
    target = sandbox / 'ex_a.png'
    target.write_bytes(b'changed existing data')
    monkeypatch.setattr(recycle.subprocess, 'run', lambda *args, **kwargs: pytest.fail('Unexpected helper launch'))
    with pytest.raises(RecycleError, match='변경'):
        recycle.move_to_recycle_bin(target, hashlib.sha256(b'old data').hexdigest())
    assert target.read_bytes() == b'changed existing data'


def test_invalid_helper_response_is_a_safety_stop_without_fallback(sandbox, monkeypatch):
    import subprocess
    from promptcleaner import recycle
    target = sandbox / 'ex_a.png'
    target.write_bytes(b'preserve this file')
    monkeypatch.setattr(recycle.subprocess, 'run', lambda *args, **kwargs:
                        subprocess.CompletedProcess(args[0], 0, stdout='[]', stderr=''))
    with pytest.raises(RecycleError):
        recycle.move_to_recycle_bin(target, hashlib.sha256(target.read_bytes()).hexdigest())
    assert target.read_bytes() == b'preserve this file'


def test_unreadable_existing_result_still_allows_rename_or_skip_review(sandbox, monkeypatch):
    from promptcleaner import batch
    source, destination, original, existing = inputs(sandbox)
    def unreadable(path):
        raise PermissionError('cannot read existing result')
    monkeypatch.setattr(batch, 'fingerprint', unreadable)
    conflicts = find_conflicts(source, destination)
    assert len(conflicts) == 1
    assert conflicts[0].digest is None
    assert conflicts[0].destination == existing
