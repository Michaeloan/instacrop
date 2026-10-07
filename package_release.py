"""Package the crop-only executable and dependency licenses."""
from pathlib import Path
from importlib import metadata
import hashlib
import shutil
import zipfile

root = Path(__file__).parent
folder = root / 'release/v1.0.3/InstaCrop'
if not (folder / 'InstaCrop.exe').is_file():
    raise SystemExit('Run build.ps1 first')
shutil.copyfile(root / 'README.md', folder / '使用说明.md')
for name in ['numpy', 'opencv-python-headless', 'Pillow', 'pillow-heif', 'pywebview', 'pythonnet', 'clr_loader', 'cffi', 'bottle', 'proxy_tools', 'typing_extensions', 'pyinstaller']:
    try:
        dist = metadata.distribution(name)
    except metadata.PackageNotFoundError:
        continue
    for file in dist.files or []:
        if file.name.upper().startswith(('LICENSE', 'COPYING', 'NOTICE')) and '..' not in file.parts:
            source = Path(dist.locate_file(file))
            if source.is_file():
                target = folder / 'licenses' / name / str(file)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
files = sorted(p for p in folder.rglob('*') if p.is_file())
if any(p.suffix.lower() in {'.polascan', '.mov', '.mp4'} or p.name in {'auth.json', 'manifest.json', 'tasks.json'} or any(part in {'ffmpeg', 'livephotobox'} for part in p.relative_to(folder).parts) for p in files):
    raise RuntimeError('Unexpected private state or media tools in distribution')
target = root / 'release/InstaCrop-Windows-1.0.3.zip'
with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
    for path in files:
        archive.write(path, path.relative_to(folder.parent))
with zipfile.ZipFile(target) as archive:
    assert archive.testzip() is None
digest = hashlib.file_digest(target.open('rb'), 'sha256').hexdigest()
target.with_suffix('.sha256.txt').write_text(digest + '  ' + target.name + '\n', encoding='utf-8')
print(target)
