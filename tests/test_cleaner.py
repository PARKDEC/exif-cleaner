import gzip
import hashlib
import io
import json
import os
import struct
import uuid
import zlib
from pathlib import Path

import pytest
from PIL import Image, PngImagePlugin

from promptcleaner.cleaner import clean_image


@pytest.fixture
def sandbox():
    # Deliberately retained: no automatic permanent removal of test artifacts.
    base = Path(os.environ.get('PROMPTCLEANER_TEST_ROOT', 'work/test-artifacts'))
    folder = base / uuid.uuid4().hex
    folder.mkdir(parents=True)
    return folder


def png_bytes(image, **kwargs):
    stream = io.BytesIO()
    image.save(stream, format='PNG', **kwargs)
    return stream.getvalue()


def chunk(kind, payload):
    return struct.pack('>I', len(payload)) + kind + payload + struct.pack('>I', zlib.crc32(kind + payload))


def hidden_image(kind='alpha', compressed=True):
    image = Image.new('RGBA' if kind == 'alpha' else 'RGB', (128, 96), (82, 121, 160, 255) if kind == 'alpha' else (82, 121, 160))
    payload = json.dumps({'Description': 'private prompt', 'Comment': json.dumps({'prompt': 'private prompt', 'uc': 'negative prompt'})}).encode()
    payload = gzip.compress(payload) if compressed else payload
    magic = ('stealth_png' if kind == 'alpha' else 'stealth_rgb') + ('comp' if compressed else 'info')
    message = magic.encode() + struct.pack('>I', len(payload) * 8) + payload
    bits = [int(b) for byte in message for b in f'{byte:08b}']
    pixels = image.load()
    index = 0
    for x in range(image.width):
        for y in range(image.height):
            pixel = list(pixels[x, y])
            for channel in ([3] if kind == 'alpha' else [0, 1, 2]):
                if index < len(bits):
                    pixel[channel] = (pixel[channel] & 254) | bits[index]
                    index += 1
            pixels[x, y] = tuple(pixel)
    return image, message


def independent_lsb_bytes(image, kind, count):
    pixels = image.convert('RGBA').load()
    bits = []
    for x in range(image.width):
        for y in range(image.height):
            for channel in ([3] if kind == 'alpha' else [0, 1, 2]):
                bits.append(pixels[x, y][channel] & 1)
                if len(bits) == count * 8:
                    return bytes(int(''.join(map(str, bits[i:i + 8])), 2) for i in range(0, len(bits), 8))
    return b''


def test_png_removes_nai_text_and_exif_preserving_other_metadata_and_pixels(sandbox):
    image = Image.new('RGBA', (32, 24), (20, 40, 60, 128))
    info = PngImagePlugin.PngInfo()
    info.add_text('Description', 'private prompt')
    info.add_text('Comment', '{"prompt":"private prompt","seed":123}')
    info.add_text('Author', 'keep this author')
    info.add_text('parameters', 'private prompt', zip=True)
    info.add_itxt('XML:com.adobe.xmp', '<prompt>private prompt</prompt>')
    exif = Image.Exif()
    exif[270] = 'private prompt'
    source = sandbox / '테스트 그림.PNG'
    source.write_bytes(png_bytes(image, pnginfo=info, exif=exif, dpi=(300, 300)))
    original = source.read_bytes()
    target = sandbox / 'ex_테스트 그림.PNG'
    result = clean_image(source, target)
    assert result.verified
    assert source.read_bytes() == original
    with Image.open(target) as cleaned:
        cleaned.load()
        assert cleaned.tobytes() == image.tobytes()
        assert cleaned.size == image.size
        assert cleaned.format == 'PNG'
        assert cleaned.info['Author'] == 'keep this author'
        assert round(cleaned.info['dpi'][0]) == 300
        assert not cleaned.getexif()
        assert not {'Description', 'Comment', 'parameters', 'XML:com.adobe.xmp'} & cleaned.info.keys()


@pytest.mark.parametrize('kind', ['alpha', 'rgb'])
@pytest.mark.parametrize('compressed', [False, True])
def test_hidden_prompt_is_erased_not_just_its_header(sandbox, kind, compressed):
    image, message = hidden_image(kind, compressed)
    assert independent_lsb_bytes(image, kind, len(message)) == message
    source = sandbox / 'hidden.png'
    source.write_bytes(png_bytes(image))
    digest = hashlib.sha256(source.read_bytes()).digest()
    target = sandbox / 'ex_hidden.png'
    result = clean_image(source, target)
    assert result.hidden_removed == (kind,)
    assert hashlib.sha256(source.read_bytes()).digest() == digest
    with Image.open(target) as cleaned:
        cleaned.load()
        extracted = independent_lsb_bytes(cleaned, kind, len(message))
        assert extracted == (b'\xff' if kind == 'alpha' else b'\x00') * len(message)
        assert cleaned.mode == image.mode
        assert cleaned.size == image.size
        for before, after in zip(image.tobytes(), cleaned.tobytes()):
            assert abs(before - after) <= 1
        if kind == 'alpha':
            assert image.convert('RGB').tobytes() == cleaned.convert('RGB').tobytes()


@pytest.mark.parametrize('mode', ['RGB', 'RGBA', 'P', 'L', 'I;16'])
def test_no_prompt_png_is_copied_byte_for_byte(sandbox, mode):
    source = sandbox / 'clean.png'
    source.write_bytes(png_bytes(Image.new(mode, (20, 20))))
    target = sandbox / 'ex_clean.png'
    result = clean_image(source, target)
    assert source.read_bytes() == target.read_bytes()
    assert not result.changed


def test_jpeg_removes_metadata_without_recompressing(sandbox):
    source = sandbox / 'camera.JPG'
    exif = Image.Exif()
    exif[37510] = b'ASCII\x00\x00\x00private prompt'
    stream = io.BytesIO()
    Image.new('RGB', (32, 24), (40, 70, 100)).save(stream, 'JPEG', quality=91, progressive=True, exif=exif, icc_profile=b'preserve-icc')
    original = stream.getvalue()
    comment = b'private prompt'
    original = original[:2] + b'\xff\xfe' + struct.pack('>H', len(comment) + 2) + comment + original[2:]
    source.write_bytes(original)
    target = sandbox / 'ex_camera.JPG'
    clean_image(source, target)
    with Image.open(source) as before, Image.open(target) as after:
        assert before.tobytes() == after.tobytes()
        assert before.quantization == after.quantization
        assert after.info['icc_profile'] == b'preserve-icc'
        assert not after.getexif()
        assert 'comment' not in after.info
    assert b'private prompt' not in target.read_bytes()
    assert source.read_bytes() == original


def test_never_overwrites_a_destination_or_source(sandbox):
    source = sandbox / 'a.png'
    source.write_bytes(png_bytes(Image.new('RGB', (8, 8))))
    target = sandbox / 'ex_a.png'
    target.write_bytes(b'existing data')
    with pytest.raises(FileExistsError):
        clean_image(source, target)
    assert target.read_bytes() == b'existing data'
    with pytest.raises(ValueError):
        clean_image(source, source)


def test_corrupt_input_never_produces_a_shareable_output(sandbox):
    source = sandbox / 'broken.png'
    data = bytearray(png_bytes(Image.new('RGB', (8, 8))))
    data[20] ^= 1
    source.write_bytes(data)
    target = sandbox / 'ex_broken.png'
    with pytest.raises(ValueError):
        clean_image(source, target)
    assert not target.exists()


def test_text_after_idat_and_appended_data_are_removed(sandbox):
    source = sandbox / 'late.png'
    data = png_bytes(Image.new('RGB', (8, 8)))
    data = data[:-12] + chunk(b'tEXt', b'Comment\x00private prompt') + data[-12:] + b'private trailer'
    source.write_bytes(data)
    target = sandbox / 'ex_late.png'
    clean_image(source, target)
    assert b'private' not in target.read_bytes()


def test_animation_is_reported_instead_of_silently_flattened(sandbox):
    source = sandbox / 'animated.png'
    a, b = Image.new('RGBA', (10, 10), 'red'), Image.new('RGBA', (10, 10), 'blue')
    a.save(source, save_all=True, append_images=[b], duration=100, loop=0)
    target = sandbox / 'ex_animated.png'
    with pytest.raises(ValueError, match='애니메이션'):
        clean_image(source, target)
    assert not target.exists()


def test_hidden_rgb_does_not_break_color_key_transparency(sandbox):
    image, _ = hidden_image('rgb', False)
    source = sandbox / 'color-key.png'
    source.write_bytes(png_bytes(image, transparency=(82, 121, 160)))
    target = sandbox / 'ex_color-key.png'
    with pytest.raises(ValueError, match='투명도'):
        clean_image(source, target)
    assert not target.exists()
