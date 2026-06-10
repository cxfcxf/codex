from pathlib import Path
from codex.config import OVERLAP_WORDS


def extract_pdf(path: Path) -> list[dict]:
    import fitz
    doc = fitz.open(str(path))
    pages = []
    for i in range(len(doc)):
        text = doc[i].get_text("text")
        if text.strip():
            pages.append({"page": i + 1, "text": text})
    doc.close()
    return pages


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


def extract_txt(path: Path) -> list[dict]:
    return _text_to_pages(path.read_text(encoding="utf-8", errors="ignore"))


def extract_docx(path: Path) -> list[dict]:
    from docx import Document
    doc = Document(str(path))
    text = "\n".join(p.text for p in doc.paragraphs if p.text.strip())
    return _text_to_pages(text)


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
    suffix = path.suffix.lower()
    extractors = {
        ".pdf": extract_pdf, ".epub": extract_epub,
        ".txt": extract_txt, ".docx": extract_docx,
    }
    if suffix not in extractors:
        raise ValueError(f"Unsupported format: {suffix}")
    return extractors[suffix](path)


def _tail_words(text: str, n: int) -> str:
    words = text.split()
    return " ".join(words[-n:]) if len(words) > n else text


def suggest_settings(pages: list[dict]) -> dict:
    is_structured = bool(pages and pages[0].get("chapter"))
    total_words = sum(len(p["text"].split()) for p in pages)
    n_pages = len(pages)
    avg_wpp = total_words / n_pages if n_pages else 300

    workers = 1

    # Dense text (short lines, poetry, drama) needs smaller chunks
    if total_words < 8_000:
        chunk_size = 600
    elif avg_wpp < 120:
        chunk_size = 1000
    else:
        chunk_size = 1500

    return {
        "is_structured": is_structured,
        "total_words": total_words,
        "total_pages": n_pages,
        "workers": workers,
        "chunk_size": chunk_size,
        "extract_chunk_size": chunk_size * 2,
        "overlap_words": max(50, chunk_size // 10),
    }


def chunk_pages(pages: list[dict], chunk_size: int, with_overlap: bool = False) -> list[dict]:
    chunks, current_texts, current_words = [], [], 0
    start_page = pages[0]["page"]
    for p in pages:
        wc = len(p["text"].split())
        if current_words + wc > chunk_size and current_texts:
            chunks.append({
                "id": len(chunks),
                "text": "\n".join(current_texts),
                "start_page": start_page,
                "end_page": p["page"] - 1,
                "word_count": current_words,
            })
            current_texts, current_words, start_page = [p["text"]], wc, p["page"]
        else:
            current_texts.append(p["text"])
            current_words += wc
    if current_texts:
        chunks.append({
            "id": len(chunks),
            "text": "\n".join(current_texts),
            "start_page": start_page,
            "end_page": pages[-1]["page"],
            "word_count": current_words,
        })
    if with_overlap:
        for i in range(1, len(chunks)):
            chunks[i]["context"] = _tail_words(chunks[i - 1]["text"], OVERLAP_WORDS)
    return chunks
