from pathlib import Path


def extract_epub(path: Path) -> list[dict]:
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup
    book = epub.read_epub(str(path))

    content_items = [
        item for item in book.get_items()
        if item.get_type() == ebooklib.ITEM_DOCUMENT
        and "nav" not in item.get_name().lower()
    ]

    # Structured EPUB (one chapter per file): return one entry per chapter
    if len(content_items) > 3:
        pages = []
        for i, item in enumerate(content_items):
            soup = BeautifulSoup(item.get_content(), "html.parser")
            h1 = soup.find("h1")
            chapter_title = h1.get_text(strip=True) if h1 else f"Section {i + 1}"
            if h1:
                h1.decompose()
            text = soup.get_text(separator="\n").strip()
            if text:
                pages.append({"page": i + 1, "text": text, "chapter": chapter_title})
        return pages

    # Flat EPUB: join and chunk by word count
    parts = []
    for item in content_items:
        soup = BeautifulSoup(item.get_content(), "html.parser")
        text = soup.get_text(separator="\n")
        if text.strip():
            parts.append(text)
    return _text_to_pages("\n\n".join(parts))


def _text_to_pages(text: str, words_per_page: int = 300) -> list[dict]:
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    pages, current, count, num = [], [], 0, 1
    for para in paragraphs:
        wc = len(para.split())
        current.append(para)
        count += wc
        if count >= words_per_page:
            pages.append({"page": num, "text": "\n".join(current)})
            num += 1
            current, count = [], 0
    if current:
        pages.append({"page": num, "text": "\n".join(current)})
    return pages


def analyze_epub_structure(path: Path, min_para_chars: int = 10) -> list[dict]:
    """Return flat list of {filename, title, text} for each content document in an EPUB."""
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(str(path))
    chapters = []
    for item in book.get_items():
        if item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")
        paras = [p.get_text(separator=" ", strip=True)
                 for p in soup.find_all("p")
                 if len(p.get_text(strip=True)) > min_para_chars]
        if not paras:
            continue
        h1 = soup.find("h1")
        title = h1.get_text(strip=True) if h1 else Path(item.file_name).stem
        chapters.append({
            "filename": item.file_name,
            "title": title,
            "text": "\n\n".join(paras),
        })
    return chapters


def extract_text(path: Path) -> list[dict]:
    if path.suffix.lower() != ".epub":
        raise ValueError(f"Unsupported format: {path.suffix}")
    return extract_epub(path)


def suggest_settings(pages: list[dict]) -> dict:
    is_structured = bool(pages and pages[0].get("chapter"))
    total_words = sum(len(p["text"].split()) for p in pages)

    return {
        "is_structured": is_structured,
        "total_words": total_words,
        "total_pages": len(pages),
    }
