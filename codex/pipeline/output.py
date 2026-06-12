import zipfile
from pathlib import Path


def build_translated_epub(original_path: Path, translations: dict, out_path: Path) -> Path:
    """Copy original EPUB zip, replacing <p> text with translations keyed by ebooklib filename."""
    from bs4 import BeautifulSoup

    tmp_path = out_path.with_suffix(".tmp.epub")
    with zipfile.ZipFile(str(original_path), "r") as zin, \
         zipfile.ZipFile(str(tmp_path), "w", zipfile.ZIP_DEFLATED) as zout:
        zip_lookup = {}
        for zip_path in (i.filename for i in zin.infolist()):
            for key, content in translations.items():
                if zip_path == key or zip_path.endswith("/" + key):
                    zip_lookup[zip_path] = content
                    break

        for info in zin.infolist():
            data = zin.read(info.filename)
            compress = zipfile.ZIP_STORED if info.filename == "mimetype" else zipfile.ZIP_DEFLATED

            if info.filename in zip_lookup:
                soup = BeautifulSoup(data.decode("utf-8"), "html.parser")
                translated_lines = [l.strip() for l in zip_lookup[info.filename].split("\n") if l.strip()]
                existing = soup.find_all("p")
                for i, p in enumerate(existing):
                    if i < len(translated_lines):
                        anchors = [a for a in p.find_all("a") if a.get("id") or a.get("href")]
                        p.clear()
                        p.append(translated_lines[i])
                        for a in anchors:
                            p.insert(0, a)
                    else:
                        p.decompose()
                if len(translated_lines) > len(existing):
                    body = soup.find("body") or soup
                    for line in translated_lines[len(existing):]:
                        new_p = soup.new_tag("p")
                        new_p.string = line
                        body.append(new_p)
                data = soup.encode("utf-8")

            zout.writestr(info, data, compress_type=compress)
    tmp_path.replace(out_path)
    return out_path
