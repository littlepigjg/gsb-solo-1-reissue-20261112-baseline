"""Create a portable source ZIP without interpreter, caches or runtime data."""
import zipfile
from pathlib import Path
import os


ROOT = Path(os.path.relpath(Path(__file__).parents[1]))
EXCLUDED = {".venv", "__pycache__", ".pytest_cache", ".git", "dist"}


def main():
    destination = ROOT / "dist"
    destination.mkdir(exist_ok=True)
    archive_path = destination / "image-studio-source.zip"
    count = 0
    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(ROOT.rglob("*")):
            relative = path.relative_to(ROOT)
            if any(part in EXCLUDED for part in relative.parts):
                continue
            if not path.is_file():
                continue
            if relative.parts[0] in {"data", "logs"} and path.name != ".gitkeep":
                continue
            archive.write(path, Path("gsb_1") / relative)
            count += 1
    print(f"Packaged {count} files: {archive_path} ({archive_path.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
