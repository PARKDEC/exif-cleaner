import csv
import threading

from PIL import Image

from test_cleaner import png_bytes, sandbox
from promptcleaner.batch import find_conflicts, run_input
from promptcleaner.resolution import ConflictChoice


def test_folder_flow_ignores_subfolders_and_continues_after_bad_images(sandbox):
    source = sandbox / '입력 폴더'
    source.mkdir()
    (source / '좋은 그림.png').write_bytes(png_bytes(Image.new('RGB', (10, 12))))
    (source / 'broken.jpg').write_bytes(b'not an image')
    (source / 'notes.txt').write_text('not an image')
    nested = source / 'subfolder'
    nested.mkdir()
    (nested / 'private.png').write_bytes(png_bytes(Image.new('RGB', (10, 10))))
    originals = {p: p.read_bytes() for p in source.rglob('*') if p.is_file()}
    progress = []
    result = run_input(source, on_progress=lambda done, total, row: progress.append((done, total)))
    assert result.total == 2
    assert result.success == 1
    assert result.failed == 1
    assert (result.output_folder / 'ex_좋은 그림.png').is_file()
    assert not (result.output_folder / 'ex_broken.jpg').exists()
    assert not (result.output_folder / 'ex_private.png').exists()
    assert all(p.read_bytes() == data for p, data in originals.items())
    assert progress[0] == (0, 2)
    assert progress[-1] == (2, 2)
    with (result.output_folder / '처리결과.csv').open(encoding='utf-8-sig', newline='') as handle:
        assert len(list(csv.DictReader(handle))) == 2


def test_repeated_runs_have_separate_results(sandbox):
    (sandbox / 'image.png').write_bytes(png_bytes(Image.new('RGB', (10, 10))))
    first = run_input(sandbox)
    original_output = (first.output_folder / 'ex_image.png').read_bytes()
    second = run_input(sandbox)
    assert first.output_folder != second.output_folder
    assert (first.output_folder / 'ex_image.png').read_bytes() == original_output
    assert second.total == 1


def test_empty_folder_does_not_make_an_output_folder(sandbox):
    result = run_input(sandbox)
    assert result.total == 0
    assert result.output_folder is None
    assert list(sandbox.iterdir()) == []


def test_cancel_finishes_current_image_and_leaves_rest_untouched(sandbox):
    for i in range(3):
        (sandbox / f'{i}.png').write_bytes(png_bytes(Image.new('RGB', (10, 10))))
    stop = threading.Event()
    def progress(done, total, row):
        if done == 1:
            stop.set()
    result = run_input(sandbox, on_progress=progress, cancel_event=stop)
    assert result.cancelled
    assert result.success == 1
    assert result.failed == 0
    assert len(list(result.output_folder.glob('ex_*.png'))) == 1


def test_folder_must_exist(sandbox):
    import pytest
    with pytest.raises(ValueError):
        run_input(sandbox / 'missing')


def test_custom_output_saves_directly_in_selected_folder(sandbox):
    source, destination = sandbox / '원본', sandbox / '내가 지정한 결과 폴더'
    source.mkdir()
    destination.mkdir()
    image = source / '사진.png'
    original = png_bytes(Image.new('RGB', (10, 10)))
    image.write_bytes(original)
    result = run_input(source, output_folder=destination)
    assert result.output_folder == destination.resolve()
    assert result.success == 1
    assert (destination / 'ex_사진.png').is_file()
    assert (destination / '처리결과.csv').is_file()
    assert list(source.iterdir()) == [image]
    assert image.read_bytes() == original


def test_custom_output_rejects_source_folder_and_missing_folder(sandbox):
    import pytest
    (sandbox / 'a.png').write_bytes(png_bytes(Image.new('RGB', (10, 10))))
    with pytest.raises(ValueError, match='원본'):
        run_input(sandbox, output_folder=sandbox)
    with pytest.raises(ValueError, match='저장'):
        run_input(sandbox, output_folder=sandbox / 'missing')
    assert len(list(sandbox.iterdir())) == 1


def test_reusing_custom_output_preserves_existing_images_and_reports(sandbox):
    source, destination = sandbox / 'input', sandbox / 'output'
    source.mkdir()
    destination.mkdir()
    (source / 'a.png').write_bytes(png_bytes(Image.new('RGB', (10, 10))))
    first = run_input(source, output_folder=destination)
    original_image = (destination / 'ex_a.png').read_bytes()
    original_report = (destination / '처리결과.csv').read_bytes()
    second = run_input(source, output_folder=destination)
    assert first.success == 1
    assert second.failed == 1
    assert (destination / 'ex_a.png').read_bytes() == original_image
    assert (destination / '처리결과.csv').read_bytes() == original_report
    assert len(list(destination.glob('처리결과*.csv'))) == 2


def test_conflict_preflight_lists_exact_source_and_destination_without_writing(sandbox):
    source, destination = sandbox / 'input', sandbox / 'output'
    source.mkdir()
    destination.mkdir()
    (source / 'a.png').write_bytes(png_bytes(Image.new('RGB', (10, 10))))
    existing = destination / 'ex_a.png'
    existing.write_bytes(png_bytes(Image.new('RGB', (10, 10), 'blue')))
    before = existing.read_bytes()
    conflicts = find_conflicts(source, destination)
    assert len(conflicts) == 1
    assert conflicts[0].source == source / 'a.png'
    assert conflicts[0].destination == existing
    assert existing.read_bytes() == before
    assert list(destination.iterdir()) == [existing]


def test_approved_conflicts_are_skipped_not_failed_and_other_images_continue(sandbox):
    source, destination = sandbox / 'input', sandbox / 'output'
    source.mkdir()
    destination.mkdir()
    for name in ('a.png', 'b.png'):
        (source / name).write_bytes(png_bytes(Image.new('RGB', (10, 10))))
    existing = destination / 'ex_a.png'
    existing.write_bytes(png_bytes(Image.new('RGB', (10, 10), 'blue')))
    before = existing.read_bytes()
    result = run_input(source, output_folder=destination, resolutions={'a.png': ConflictChoice('skip')})
    assert result.success == 1
    assert result.failed == 0
    assert result.skipped == 1
    assert existing.read_bytes() == before
    assert (destination / 'ex_b.png').is_file()
    with (destination / '처리결과.csv').open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle))
    assert rows[0]['결과'] == '건너뜀'


def test_single_file_default_output_does_not_process_its_siblings(sandbox):
    source = sandbox / '선택한 이미지.png'
    original = png_bytes(Image.new('RGB', (20, 12), 'red'))
    source.write_bytes(original)
    (sandbox / '선택하지 않은 이미지.png').write_bytes(png_bytes(Image.new('RGB', (20, 12), 'blue')))
    result = run_input(source)
    assert result.total == 1
    assert result.success == 1
    assert result.output_folder.parent == sandbox.resolve()
    assert (result.output_folder / 'ex_선택한 이미지.png').is_file()
    assert not (result.output_folder / 'ex_선택하지 않은 이미지.png').exists()
    assert source.read_bytes() == original


def test_single_file_custom_output_and_conflict_preflight(sandbox):
    source = sandbox / '사진.JPG'
    Image.new('RGB', (20, 12), 'green').save(source)
    destination = sandbox / 'output'
    destination.mkdir()
    result = run_input(source, output_folder=destination)
    assert result.success == 1
    assert (destination / 'ex_사진.JPG').is_file()
    conflicts = find_conflicts(source, destination)
    assert len(conflicts) == 1
    assert conflicts[0].source == source


def test_single_file_rejects_unsupported_input_and_its_original_folder(sandbox):
    import pytest
    source = sandbox / 'a.png'
    source.write_bytes(png_bytes(Image.new('RGB', (10, 10))))
    with pytest.raises(ValueError, match='원본'):
        run_input(source, output_folder=sandbox)
    text = sandbox / 'a.txt'
    text.write_text('not an image')
    with pytest.raises(ValueError, match='PNG'):
        run_input(text)
