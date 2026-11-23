import time
import gc
import tkinter as tk
from types import SimpleNamespace

import pytest
from PIL import Image

from test_cleaner import png_bytes, sandbox
from promptcleaner.ui import CleanerApp
from promptcleaner.resolution import ConflictChoice


@pytest.fixture
def app(tk_host):
    root = tk.Toplevel(tk_host)
    root.withdraw()
    instance = CleanerApp(root)
    yield instance
    instance.stop_event.set()
    if instance.worker is not None:
        instance.worker.join(timeout=3)
        assert not instance.worker.is_alive(), 'Image worker did not stop during test cleanup'
    if root.winfo_exists():
        root.after_cancel(instance._poll_id)
        root.destroy()
    gc.collect()


def finish(app):
    deadline = time.monotonic() + 15
    while (app.busy or (app.pending and not app.queue_paused)) and time.monotonic() < deadline:
        app.root.update()
        time.sleep(.01)
    app.root.update()
    assert not app.busy


def test_drop_with_korean_spaces_and_braces_runs_real_processing(app, sandbox):
    source = sandbox / '한글 폴더 {공백 포함}'
    source.mkdir()
    (source / '사진.png').write_bytes(png_bytes(Image.new('RGB', (12, 12))))
    app.on_drop(SimpleNamespace(data=tk._join([str(source)])))
    assert app.busy
    assert not app.choose_button.instate(['disabled'])
    finish(app)
    assert app.last_result.success == 1
    assert (app.last_result.output_folder / 'ex_사진.png').is_file()
    assert not app.open_button.instate(['disabled'])
    assert not app.choose_button.instate(['disabled'])


def test_folder_picker_uses_same_processing_flow(app, sandbox, monkeypatch):
    (sandbox / 'a.png').write_bytes(png_bytes(Image.new('RGB', (12, 12))))
    monkeypatch.setattr('promptcleaner.ui.filedialog.askdirectory', lambda **kwargs: str(sandbox))
    app.choose_folder()
    finish(app)
    assert app.last_result.success == 1


def test_unsupported_input_is_rejected_without_starting(app, sandbox):
    file = sandbox / 'a.txt'
    file.write_text('not an image')
    app.on_drop(SimpleNamespace(data=tk._join([str(file)])))
    assert not app.busy
    assert 'PNG' in app.status_text.get()
    app.on_drop(SimpleNamespace(data=tk._join([str(sandbox / 'missing')])))
    assert not app.busy
    assert '찾을 수' in app.status_text.get()


def test_folder_button_fits_inside_drop_area_at_current_display_scale(app):
    app.root.deiconify()
    app.root.update_idletasks()
    assert app.choose_button.winfo_ismapped()
    bottom = app.choose_button.winfo_rooty() + app.choose_button.winfo_height()
    assert bottom <= app.drop_frame.winfo_rooty() + app.drop_frame.winfo_height()


def test_bottom_controls_remain_visible_with_queue_and_long_result_path(app):
    app.folder_text.set('결과: C:\\' + '\\'.join(['긴 결과 폴더 이름'] * 18))
    app.root.deiconify()
    app.root.update_idletasks()
    assert app.open_button.winfo_ismapped()
    assert app.cancel_button.winfo_ismapped()
    bottom = app.open_button.winfo_rooty() + app.open_button.winfo_height()
    assert bottom <= app.root.winfo_rooty() + app.root.winfo_height()


def test_select_custom_output_then_input_folder(app, sandbox, monkeypatch):
    source, destination = sandbox / '입력', sandbox / '출력'
    source.mkdir()
    destination.mkdir()
    (source / 'a.png').write_bytes(png_bytes(Image.new('RGB', (12, 12))))
    selections = iter([str(destination), str(source)])
    monkeypatch.setattr('promptcleaner.ui.filedialog.askdirectory', lambda **kwargs: next(selections))
    app.choose_output_folder()
    assert app.output_folder == destination.resolve()
    assert str(destination.resolve()) in app.output_text.get()
    app.choose_folder()
    assert app.output_button.instate(['disabled'])
    assert app.reset_output_button.instate(['disabled'])
    finish(app)
    assert app.last_result.output_folder == destination.resolve()
    assert (destination / 'ex_a.png').is_file()
    assert not app.output_button.instate(['disabled'])


def test_drop_respects_custom_output_and_reset_restores_default(app, sandbox, monkeypatch):
    source, destination = sandbox / '입력', sandbox / '결과'
    source.mkdir()
    destination.mkdir()
    (source / 'a.png').write_bytes(png_bytes(Image.new('RGB', (12, 12))))
    monkeypatch.setattr('promptcleaner.ui.filedialog.askdirectory', lambda **kwargs: str(destination))
    app.choose_output_folder()
    app.on_drop(SimpleNamespace(data=tk._join([str(source)])))
    finish(app)
    assert app.last_result.output_folder == destination.resolve()
    app.reset_output_folder()
    assert app.output_folder is None
    app.enqueue_paths([source])
    finish(app)
    assert app.last_result.output_folder.parent == source.resolve()


def test_same_input_and_output_is_rejected_before_processing(app, sandbox, monkeypatch):
    monkeypatch.setattr('promptcleaner.ui.filedialog.askdirectory', lambda **kwargs: str(sandbox))
    app.choose_output_folder()
    app.enqueue_paths([sandbox])
    assert not app.busy
    assert '원본' in app.status_text.get()


def make_conflict(sandbox):
    source, destination = sandbox / 'input', sandbox / 'output'
    source.mkdir()
    destination.mkdir()
    (source / 'a.png').write_bytes(png_bytes(Image.new('RGB', (30, 20), 'red')))
    (destination / 'ex_a.png').write_bytes(png_bytes(Image.new('RGB', (30, 20), 'blue')))
    return source, destination


def test_collision_modal_cancel_starts_no_processing(app, sandbox, monkeypatch):
    source, destination = make_conflict(sandbox)
    app.output_folder = destination
    calls = []
    def cancel_dialog(parent, conflicts):
        calls.append(conflicts)
        return None
    monkeypatch.setattr('promptcleaner.ui.show_conflicts', cancel_dialog)
    app.enqueue_paths([source])
    assert len(calls) == 1
    assert calls[0][0].destination == destination / 'ex_a.png'
    assert not app.busy
    assert app.last_result is None
    assert list(destination.iterdir()) == [destination / 'ex_a.png']


def test_collision_modal_continue_excludes_duplicates_instead_of_failing(app, sandbox, monkeypatch):
    source, destination = make_conflict(sandbox)
    (source / 'b.png').write_bytes(png_bytes(Image.new('RGB', (30, 20), 'green')))
    app.output_folder = destination
    monkeypatch.setattr('promptcleaner.ui.show_conflicts', lambda parent, conflicts:
                        {conflict.source.name: ConflictChoice('skip') for conflict in conflicts})
    app.enqueue_paths([source])
    finish(app)
    assert app.last_result.success == 1
    assert app.last_result.skipped == 1
    assert app.last_result.failed == 0
    assert '중복 제외 1개' in app.status_text.get()


def test_collision_dialog_displays_filename_and_both_real_thumbnails(app, sandbox):
    from PIL import ImageTk
    from promptcleaner.batch import find_conflicts
    from promptcleaner.dialogs import ConflictDialog
    source, destination = make_conflict(sandbox)
    app.root.deiconify()
    app.root.update_idletasks()
    dialog = ConflictDialog(app.root, find_conflicts(source, destination))
    app.root.update_idletasks()
    assert dialog.table.item('0', 'values') == ('ex_a.png', '미선택')
    assert len(dialog.photos) == 2
    colors = [ImageTk.getimage(photo).convert('RGB').getpixel((0, 0)) for photo in dialog.photos]
    assert (0, 0, 255) in colors
    assert (255, 0, 0) in colors
    dialog.cancel()
    assert dialog.decisions is None


def test_optional_icon_error_does_not_prevent_app_startup(tk_host, monkeypatch):
    def broken_icon(**kwargs):
        raise tk.TclError('icon unavailable')
    monkeypatch.setattr('promptcleaner.ui.tk.PhotoImage', broken_icon)
    root = tk.Toplevel(tk_host)
    root.withdraw()
    try:
        instance = CleanerApp(root)
        assert instance.icon is None
        assert not instance.file_button.instate(['disabled'])
        root.after_cancel(instance._poll_id)
    finally:
        root.destroy()


def test_dialog_requires_a_choice_and_can_commit_overwrite(app, sandbox):
    from promptcleaner.batch import find_conflicts
    from promptcleaner.dialogs import ConflictDialog
    source, destination = make_conflict(sandbox)
    dialog = ConflictDialog(app.root, find_conflicts(source, destination))
    assert dialog.apply_button.instate(['disabled'])
    assert str((destination / 'ex_a.png').resolve()) == dialog.target_text.get()
    dialog.set_choice('overwrite')
    assert not dialog.apply_button.instate(['disabled'])
    dialog.proceed()
    assert dialog.decisions['a.png'].action == 'overwrite'
    assert dialog.decisions['a.png'].digest
    assert (destination / 'ex_a.png').exists()


def test_dialog_rename_records_name_and_skip_is_separate_choice(app, sandbox, monkeypatch):
    from promptcleaner.batch import find_conflicts
    from promptcleaner.dialogs import ConflictDialog
    source, destination = make_conflict(sandbox)
    monkeypatch.setattr('promptcleaner.dialogs.simpledialog.askstring', lambda *args, **kwargs: '내 새 이름')
    dialog = ConflictDialog(app.root, find_conflicts(source, destination))
    dialog.set_choice('rename')
    assert dialog.choices['a.png'].name == '내 새 이름.png'
    dialog.set_choice('skip')
    assert dialog.choices['a.png'].action == 'skip'
    dialog.cancel()
    assert dialog.decisions is None
    assert not (destination / '내 새 이름.png').exists()


def test_ui_rename_is_applied_to_actual_output(app, sandbox, monkeypatch):
    source, destination = make_conflict(sandbox)
    old = (destination / 'ex_a.png').read_bytes()
    app.output_folder = destination
    monkeypatch.setattr('promptcleaner.ui.show_conflicts', lambda parent, conflicts:
                        {'a.png': ConflictChoice('rename', name='new.png')})
    app.enqueue_paths([source])
    finish(app)
    assert app.last_result.success == 1
    assert (destination / 'new.png').exists()
    assert (destination / 'ex_a.png').read_bytes() == old
