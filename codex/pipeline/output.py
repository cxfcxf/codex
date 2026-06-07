import zipfile
from pathlib import Path


def build_html(chunks: list[dict], translations: dict, title: str) -> str:
    paras = []
    for chunk in chunks:
        chapter = chunk.get("chapter")
        if chapter:
            # Book titles have no " — "; chapters do
            tag = "h1" if " — " not in chapter else "h2"
            paras.append(f"<{tag}>{chapter}</{tag}>")
        text = translations.get(str(chunk["id"]), "")
        for line in text.split("\n"):
            if line.strip():
                paras.append(f"<p>{line.strip()}</p>")
    body = "\n".join(paras)
    return f"""<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>{title}</title>
<style>body{{font-family:serif;font-size:16px;line-height:1.9;max-width:750px;margin:0 auto;padding:40px 20px}}
p{{margin:0 0 1em;text-indent:2em}}</style></head>
<body>{body}</body></html>"""


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


def generate_output(
    chunks: list[dict],
    translations: dict,
    title: str,
    output_format: str,
    output_path: Path,
) -> Path:
    html = build_html(chunks, translations, title)
    html_path = output_path.parent / (output_path.stem + ".html")
    html_path.write_text(html, encoding="utf-8")

    if output_format == "html":
        return html_path
    elif output_format == "epub":
        import subprocess
        subprocess.run(
            ["pandoc", str(html_path), "-o", str(output_path),
             "--metadata", f"title={title}", "--toc", "--toc-depth=2"],
            check=True, capture_output=True,
        )
        return output_path
    elif output_format == "pdf":
        import weasyprint
        weasyprint.HTML(filename=str(html_path)).write_pdf(str(output_path))
        return output_path
    elif output_format == "txt":
        from bs4 import BeautifulSoup
        output_path.write_text(BeautifulSoup(html, "html.parser").get_text(), encoding="utf-8")
        return output_path
    return html_path
