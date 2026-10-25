"""Build a source-only AstrBot installation archive."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


def build() -> Path:
    root = Path(__file__).resolve().parent
    files = [
        *root.glob("*.py"),
        root / "metadata.yaml",
        root / "_conf_schema.json",
        root / "README.md",
    ]
    files += [
        path
        for directory in ("pages", ".astrbot-plugin")
        for path in (root / directory).rglob("*")
        if path.is_file()
    ]
    destination = root / "dist" / "astrbot_plugin_tool_scheduler-0.1.1.zip"
    destination.parent.mkdir(exist_ok=True)
    with ZipFile(destination, "w", compression=ZIP_DEFLATED) as archive:
        for path in sorted(files):
            if path.name != "build.py":
                archive.write(path, root.name + "/" + path.relative_to(root).as_posix())
    return destination


if __name__ == "__main__":
    print(build())
