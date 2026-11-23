import threading
import tkinter as tk
from types import SimpleNamespace

import pytest
from PIL import Image

from test_cleaner import png_bytes, sandbox
from test_ui import app, finish
from promptcleaner.resolution import ConflictChoice


@pytest.fixture
def blocked_cleaner(monkeypatch):
    from promptcleaner import batch
    real = batch.clean_image
    entered, release = threading.Event(), threading.Event()
    calls = []
    def gated(source, destination):
        calls.append(source)
        if not entered.is_set():
            entered.set()
            if not release.wait(10):
                raise RuntimeError('Test did not release the first real image operation')
        return real(source, destination)
    monkeypatch.setattr(batch, 'clean_image', gated)
    yield SimpleNamespace(entered=entered, release=release, calls=calls)
    release.set()


def image_file(folder, name, color='red'):
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    path.write_bytes(png_bytes(Image.new('RGB', (24, 16), color)))
    return path


def test_individual_file_drop_processes_only_that_file(app, sandbox):
    source = image_file(sandbox, '선택한 파일.png')
    image_file(sandbox, '다른 파일.png', 'blue')
    assert app.on_drop(SimpleNamespace(data=tk._join([str(source)]))) == 'copy'
    finish(app)
    assert app.last_result.total == 1
    assert (app.last_result.output_folder / 'ex_선택한 파일.png').is_file()
    assert not (app.last_result.output_folder / 'ex_다른 파일.png').exists()


def test_files_and_folders_dropped_while_busy_are_processed_in_fifo_order(app, sandbox, blocked_cleaner):
    first = image_file(sandbox / 'one', 'a.png')
    middle = image_file(sandbox / 'two', 'b.png')
    last = image_file(sandbox / 'three', 'c.png')
    app.enqueue_paths([first])
    assert blocked_cleaner.entered.wait(3)
    assert app._drop_enter(None) == 'copy'
    assert app.on_drop(SimpleNamespace(data=tk._join([str(middle.parent), str(last)]))) == 'copy'
    assert len(app.pending) == 2
    assert len(app.queue_table.get_children()) == 2
    assert not app.choose_button.instate(['disabled'])
    assert not app.file_button.instate(['disabled'])
    blocked_cleaner.release.set()
    finish(app)
    assert blocked_cleaner.calls == [first.resolve(), middle.resolve(), last.resolve()]
    assert len(app.completed_results) == 3
    assert len(app.table.get_children()) == 3
    assert not app.pending


def test_multiple_file_picker_can_add_to_a_running_queue(app, sandbox, blocked_cleaner, monkeypatch):
    first = image_file(sandbox / 'one', 'a.png')
    b = image_file(sandbox / 'two', 'b.png')
    c = image_file(sandbox / 'three', 'c.png')
    app.enqueue_paths([first])
    assert blocked_cleaner.entered.wait(3)
    monkeypatch.setattr('promptcleaner.ui.filedialog.askopenfilenames', lambda **kwargs: (str(b), str(c)))
    app.choose_files()
    assert [job.source for job in app.pending] == [b.resolve(), c.resolve()]
    blocked_cleaner.release.set()
    finish(app)
    assert len(app.completed_results) == 3


def test_stop_preserves_waiting_jobs_and_resume_continues(app, sandbox, blocked_cleaner):
    first = image_file(sandbox / 'active', 'a.png')
    image_file(first.parent, 'b.png')
    later = image_file(sandbox / 'later', 'c.png')
    destination = sandbox / 'out'
    destination.mkdir()
    app.output_folder = destination
    app.enqueue_paths([first.parent])
    assert blocked_cleaner.entered.wait(3)
    app.enqueue_paths([later])
    app.cancel()
    blocked_cleaner.release.set()
    finish(app)
    assert app.queue_paused
    assert len(app.pending) == 1
    assert not (destination / 'ex_c.png').exists()
    assert app.last_result.cancelled
    app.resume_queue()
    finish(app)
    assert (destination / 'ex_c.png').is_file()
    assert not app.pending


def test_clear_queue_does_not_stop_the_active_file(app, sandbox, blocked_cleaner):
    first = image_file(sandbox / 'active', 'a.png')
    later = image_file(sandbox / 'later', 'b.png')
    app.enqueue_paths([first])
    assert blocked_cleaner.entered.wait(3)
    app.enqueue_paths([later])
    app.clear_queue()
    assert app.busy
    assert not app.stop_event.is_set()
    assert not app.pending
    blocked_cleaner.release.set()
    finish(app)
    assert len(app.completed_results) == 1
    assert app.last_result.success == 1


def test_queued_job_captures_output_folder_when_added(app, sandbox):
    a = image_file(sandbox / 'a', 'a.png')
    b = image_file(sandbox / 'b', 'b.png')
    out_a, out_b = sandbox / 'out_a', sandbox / 'out_b'
    out_a.mkdir()
    out_b.mkdir()
    app.queue_paused = True
    app.output_folder = out_a
    app.enqueue_paths([a])
    app.output_folder = out_b
    app.enqueue_paths([b])
    app.resume_queue()
    finish(app)
    assert (out_a / 'ex_a.png').exists()
    assert not (out_a / 'ex_b.png').exists()
    assert (out_b / 'ex_b.png').exists()


def test_conflicts_are_checked_at_job_start_after_previous_queued_writes(app, sandbox, blocked_cleaner, monkeypatch):
    a = image_file(sandbox / 'a', 'same.png', 'red')
    b = image_file(sandbox / 'b', 'same.png', 'blue')
    destination = sandbox / 'out'
    destination.mkdir()
    app.output_folder = destination
    shown = []
    def approve(parent, conflicts):
        assert (destination / 'ex_same.png').is_file()
        shown.append(conflicts)
        return {conflict.source.name: ConflictChoice('skip') for conflict in conflicts}
    monkeypatch.setattr('promptcleaner.ui.show_conflicts', approve)
    app.enqueue_paths([a])
    assert blocked_cleaner.entered.wait(3)
    app.enqueue_paths([b])
    assert not shown
    blocked_cleaner.release.set()
    finish(app)
    assert len(shown) == 1
    assert app.last_result.skipped == 1
    assert app.last_result.failed == 0
    with Image.open(destination / 'ex_same.png') as result:
        assert result.getpixel((0, 0)) == (255, 0, 0)


def test_cancel_conflict_dialog_pauses_remaining_queue(app, sandbox, monkeypatch):
    a = image_file(sandbox / 'a', 'a.png')
    b = image_file(sandbox / 'b', 'b.png')
    destination = sandbox / 'out'
    image_file(destination, 'ex_a.png', 'blue')
    app.output_folder = destination
    app.queue_paused = True
    app.enqueue_paths([a, b])
    monkeypatch.setattr('promptcleaner.ui.show_conflicts', lambda parent, conflicts: None)
    app.resume_queue()
    assert not app.busy
    assert app.queue_paused
    assert [job.source for job in app.pending] == [b.resolve()]
    assert not (destination / 'ex_b.png').exists()


def test_bad_image_does_not_prevent_later_jobs_from_running(app, sandbox):
    broken = sandbox / 'broken.png'
    broken.write_bytes(b'not a png')
    good = image_file(sandbox / 'good', 'good.png')
    app.enqueue_paths([broken, good])
    finish(app)
    assert len(app.completed_results) == 2
    assert app.completed_results[0].failed == 1
    assert app.completed_results[1].success == 1


def test_closing_does_not_start_queued_jobs(app, sandbox, blocked_cleaner):
    first = image_file(sandbox / 'active', 'a.png')
    later = image_file(sandbox / 'later', 'b.png')
    app.enqueue_paths([first, later])
    assert blocked_cleaner.entered.wait(3)
    app.on_close()
    assert app.closing
    assert not app.pending
    blocked_cleaner.release.set()
    finish(app)
    assert blocked_cleaner.calls == [first.resolve()]
    assert not app.root.winfo_exists()


def test_recycle_failure_pauses_remaining_queue(app, sandbox, monkeypatch):
    from promptcleaner import cleaner
    from promptcleaner.batch import find_conflicts
    from promptcleaner.recycle import RecycleError
    a = image_file(sandbox / 'a', 'a.png')
    b = image_file(sandbox / 'b', 'b.png')
    destination = sandbox / 'out'
    image_file(destination, 'ex_a.png', 'blue')
    digest = find_conflicts(a, destination)[0].digest
    app.output_folder = destination
    monkeypatch.setattr('promptcleaner.ui.show_conflicts', lambda parent, conflicts:
                        {'a.png': ConflictChoice('overwrite', digest=digest)})
    def fail(*args):
        raise RecycleError('휴지통 작업 실패')
    monkeypatch.setattr(cleaner, 'move_to_recycle_bin', fail)
    app.enqueue_paths([a, b])
    finish(app)
    assert app.last_result.safety_stopped
    assert app.queue_paused
    assert len(app.pending) == 1
    assert not (destination / 'ex_b.png').exists()
