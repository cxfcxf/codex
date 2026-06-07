#!/home/siegfried/vibecoding/book-translator/.venv/bin/python3
"""Remove OceanofPDF.com watermark divs from every text entry in an EPUB."""

import argparse
import re
import sys
import zipfile
from pathlib import Path

# Matches the injected watermark block regardless of minor whitespace variation.
# Anchored to the closing </div> so partial matches don't wipe legitimate content.
WATERMARK_RE = re.compile(
    r'\s*<div[^>]*>\s*<p>\s*<a\s+href=["\']https?://oceanofpdf\.com["\'][^>]*>'
    r'.*?</a>\s*</p>\s*</div>',
    re.IGNORECASE | re.DOTALL,
)

TEXT_EXTENSIONS = {".html", ".htm", ".xhtml", ".xml", ".opf", ".ncx"}


def clean_content(data: bytes) -> tuple[bytes, int]:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data, 0
    cleaned, count = WATERMARK_RE.subn("", text)
    return cleaned.encode("utf-8"), count


def trim_epub(src: Path, dst: Path) -> None:
    total_removed = 0
    files_touched = 0

    with zipfile.ZipFile(src, "r") as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        # mimetype must be first and uncompressed per EPUB spec
        if "mimetype" in zin.namelist():
            zout.writestr(
                zipfile.ZipInfo("mimetype"),
                zin.read("mimetype"),
                compress_type=zipfile.ZIP_STORED,
            )

        for item in zin.infolist():
            if item.filename == "mimetype":
                continue

            data = zin.read(item.filename)
            ext = Path(item.filename).suffix.lower()

            if ext in TEXT_EXTENSIONS:
                cleaned, count = clean_content(data)
                if count:
                    total_removed += count
                    files_touched += 1
                data = cleaned

            zout.writestr(item, data)

    print(f"Removed {total_removed} watermark block(s) from {files_touched} file(s).")
    print(f"Saved: {dst}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Strip OceanofPDF watermarks from an EPUB.")
    parser.add_argument("epub", help="Path to the source EPUB file")
    args = parser.parse_args()

    src = Path(args.epub).resolve()
    if not src.is_file():
        sys.exit(f"Error: file not found: {src}")
    if src.suffix.lower() != ".epub":
        sys.exit(f"Error: not an EPUB file: {src}")

    dst = src.with_stem(src.stem + "_trimmed")
    if dst.exists():
        print(f"Output already exists, overwriting: {dst}")

    trim_epub(src, dst)


if __name__ == "__main__":
    main()
