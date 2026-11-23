"""Build a portable Windows folder; never replace an existing build."""

import argparse
import importlib.metadata
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--destination', type=Path, required=True)
    parser.add_argument('--work-root', type=Path, required=True)
    args = parser.parse_args()
    source = Path(__file__).resolve().parent
    if (args.destination / 'PromptCleaner').exists():
        parser.error('Destination already contains PromptCleaner; choose a new destination.')
    stamp = datetime.now().strftime('%Y%m%d_%H%M%S_%f')
    scratch = args.work_root.resolve() / ('build_' + stamp)
    scratch.mkdir(parents=True)
    assets = source / 'promptcleaner' / 'assets'
    assets.mkdir(exist_ok=True)
    icon = Image.new('RGBA', (256, 256), (0, 0, 0, 0))
    draw = ImageDraw.Draw(icon)
    draw.rounded_rectangle((8, 8, 248, 248), radius=52, fill='#255C48')
    draw.rounded_rectangle((51, 69, 205, 190), radius=15, fill='#EAF2ED')
    draw.rounded_rectangle((51, 55, 119, 91), radius=10, fill='#EAF2ED')
    draw.line((88, 133, 115, 157, 169, 104), fill='#255C48', width=17, joint='curve')
    icon.save(assets / 'icon.png')
    icon.save(assets / 'icon.ico', sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    subprocess.run([
        sys.executable, '-m', 'PyInstaller', '--onedir', '--windowed',
        '--name', 'PromptCleaner', '--icon', str(assets / 'icon.ico'),
        '--add-data', f'{assets}:promptcleaner/assets',
        '--distpath', str(args.destination.resolve()),
        '--workpath', str(scratch / 'build'), '--specpath', str(scratch),
        str(source / 'main.py'),
    ], check=True, cwd=source)
    output = args.destination.resolve() / 'PromptCleaner'
    (output / '사용법.md').write_bytes((source / 'README.md').read_bytes())
    licenses = output / 'licenses'
    licenses.mkdir()
    for package in ('Pillow', 'tkinterdnd2'):
        distribution = importlib.metadata.distribution(package)
        for item in distribution.files or ():
            if 'license' in str(item).lower() and str(item).lower().endswith(('.txt', 'license', '.md', '.terms')):
                path = Path(distribution.locate_file(item))
                if path.is_file():
                    (licenses / (package + '-' + path.name)).write_bytes(path.read_bytes())
    python_license = Path(sys.base_prefix) / 'LICENSE.txt'
    if python_license.exists():
        (licenses / 'Python-LICENSE.txt').write_bytes(python_license.read_bytes())


if __name__ == '__main__':
    main()
