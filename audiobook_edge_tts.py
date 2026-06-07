#!/usr/bin/env python3
"""
Audiobook TTS — epub → MP3 via edge-tts (Microsoft Neural TTS)
Resumes automatically if interrupted. Run again to pick up where it left off.
"""

import argparse
import asyncio
import json
import re
import subprocess
import sys
from pathlib import Path

EPUB_PATH   = Path("eisenhorn_chinese_fixed.epub")
CHUNKS_DIR  = Path("tts_chunks_edge")
PROGRESS    = Path("tts_progress_edge.json")
DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"


# ── Text extraction ────────────────────────────────────────────────────────────

PAGE_RE = re.compile(r'第\s*\d+[–—\-~～]\d+\s*页|第\s*\d+\s*页')


_PUNCT_SINGLE = str.maketrans({
    '：': '，', '；': '，', '—': '，',
    '《': '', '》': '', '〈': '', '〉': '',
    '【': '', '】': '', '「': '', '」': '',
    '『': '', '』': '', '〔': '', '〕': '',
    '※': '', '★': '', '☆': '', '　': ' ',
})

def clean_text(text: str) -> str:
    text = PAGE_RE.sub('', text)
    text = text.replace('——', '，')
    text = text.translate(_PUNCT_SINGLE)
    text = re.sub(r'[，。！？、]{2,}', lambda m: m.group()[0], text)
    text = re.sub(r'\n+', '，', text)
    return text.strip('，').strip()


def extract_epub(path: Path):
    import ebooklib
    from ebooklib import epub
    from bs4 import BeautifulSoup

    book = epub.read_epub(str(path))
    books = []
    current_book = None

    for item in book.get_items():
        if item.get_type() != ebooklib.ITEM_DOCUMENT:
            continue
        soup = BeautifulSoup(item.get_content(), "html.parser")

        for el in soup.find_all(["h1", "h2", "p"]):
            if el.name in ("h1", "h2"):
                title = el.get_text(strip=True)
                if not title:
                    continue
                MAIN_BOOKS = {"Xenos", "Malleus", "Hereticus"}
                if title in MAIN_BOOKS:
                    # Top-level book boundary
                    current_book = {"title": title, "chapters": []}
                    books.append(current_book)
                else:
                    # Chapter (with or without — separator)
                    if current_book is None:
                        current_book = {"title": "Unknown", "chapters": []}
                        books.append(current_book)
                    current_book["chapters"].append({"title": title, "paragraphs": []})
            elif el.name == "p":
                text = clean_text(el.get_text(separator=""))
                if not text:
                    continue
                if current_book is None:
                    current_book = {"title": "Unknown", "chapters": []}
                    books.append(current_book)
                if not current_book["chapters"]:
                    current_book["chapters"].append({"title": None, "paragraphs": []})
                current_book["chapters"][-1]["paragraphs"].append(text)

    return [b for b in books if any(c["paragraphs"] for c in b["chapters"])]


_EN_NUMS = {
    'one':'一','two':'二','three':'三','four':'四','five':'五',
    'six':'六','seven':'七','eight':'八','nine':'九','ten':'十',
    'eleven':'十一','twelve':'十二','thirteen':'十三','fourteen':'十四',
    'fifteen':'十五','sixteen':'十六','seventeen':'十七','eighteen':'十八',
    'nineteen':'十九','twenty':'二十','twenty-one':'二十一',
    'twenty-two':'二十二','twenty-three':'二十三','twenty-four':'二十四',
    'twenty-five':'二十五','twenty-six':'二十六','twenty-seven':'二十七',
    'twenty-eight':'二十八','twenty-nine':'二十九','thirty':'三十',
}
_EN_SPECIAL = {'epilogue':'尾声','prologue':'序章','interlude':'间章'}


def _normalize_title(title: str) -> str:
    if '—' in title or '-' in title:
        sep = '—' if '—' in title else '-'
        book_part, _, ch_part = title.partition(sep)
        ch_key = ch_part.strip().lower()
        if ch_key in _EN_SPECIAL:
            ch_cn = _EN_SPECIAL[ch_key]
        elif ch_key in _EN_NUMS:
            ch_cn = f"第{_EN_NUMS[ch_key]}章"
        else:
            ch_cn = ch_part.strip()
        return f"{book_part.strip()} {ch_cn}".strip()
    return title


BUNDLE_CHARS = 2000


def build_chunks(books: list) -> tuple[list[str], list[dict]]:
    """Bundle paragraphs into ~BUNDLE_CHARS chunks per TTS call, one title chunk per chapter."""
    chunks, chapters = [], []
    ch_idx = 0
    for book in books:
        for ch in book["chapters"]:
            start = len(chunks)
            title_cn = _normalize_title(ch["title"]).strip() if ch["title"] else ""
            if title_cn:
                chunks.append(title_cn)
            buf = ""
            for para in ch["paragraphs"]:
                if not para.strip():
                    continue
                if buf and len(buf) + len(para) + 1 > BUNDLE_CHARS:
                    chunks.append(buf)
                    buf = para
                else:
                    buf = (buf + "\n" + para).strip() if buf else para
            if buf:
                chunks.append(buf)
            end = len(chunks)
            if end > start:
                label = title_cn or f"章节{ch_idx+1}"
                chapters.append({"title": label, "start": start, "end": end})
                ch_idx += 1
    return chunks, chapters


# ── Progress ───────────────────────────────────────────────────────────────────

def load_progress() -> set:
    if PROGRESS.exists():
        return set(json.loads(PROGRESS.read_text()).get("done", []))
    return set()

def save_progress(done: set):
    PROGRESS.write_text(json.dumps({"done": sorted(done)}, ensure_ascii=False))


# ── Generation ─────────────────────────────────────────────────────────────────

async def tts_to_file(text: str, voice: str, path: Path):
    import edge_tts
    communicate = edge_tts.Communicate(text, voice)
    await communicate.save(str(path))


async def generate(chunks: list[str], done: set, voice: str, chapters: list[dict], delay: float = 0.0):
    remaining = [i for i in range(len(chunks)) if i not in done]
    if not remaining:
        print("All chunks already done.")
        return

    print(f"Generating {len(remaining)} chunks (already done: {len(done)})…\n")

    import time
    total = len(chunks)

    for idx in remaining:
        text = chunks[idx]
        out_path = CHUNKS_DIR / f"{idx:06d}.mp3"
        t0 = time.time()
        try:
            await tts_to_file(text, voice, out_path)
            elapsed = time.time() - t0
            done.add(idx)
            save_progress(done)
            pct = len(done) / total * 100
            print(f"  [{len(done)}/{total}] {pct:.1f}%  {elapsed:.1f}s  chunk {idx}", flush=True)
            if delay:
                await asyncio.sleep(delay)
            for ch in chapters:
                if ch["start"] <= idx < ch["end"]:
                    if all(i in done for i in range(ch["start"], ch["end"])):
                        print(f"  ✓ chapter done, stitching: {ch['title']}", flush=True)
                        stitch([ch])
                    break
        except Exception as e:
            print(f"  ⚠ chunk {idx} failed: {e}", flush=True)


# ── Stitch ─────────────────────────────────────────────────────────────────────

CHAPTERS_DIR = CHUNKS_DIR / "chapters"


def stitch(chapters: list[dict]):
    CHUNKS_DIR.mkdir(exist_ok=True)
    CHAPTERS_DIR.mkdir(exist_ok=True)
    list_file = CHUNKS_DIR / "list.txt"

    for ch in chapters:
        lines, missing = [], 0
        for idx in range(ch["start"], ch["end"]):
            p = CHUNKS_DIR / f"{idx:06d}.mp3"
            if p.exists():
                lines.append(f"file '{p.resolve()}'")
            else:
                missing += 1
        if not lines:
            continue
        safe_title = re.sub(r'[^\w一-鿿]+', '_', ch["title"])[:40]
        out = CHAPTERS_DIR / f"{ch['start']:06d}_{safe_title}.mp3"
        list_file.write_text("\n".join(lines))
        if missing:
            print(f"⚠ chapter '{ch['title']}': {missing} chunks missing")
        subprocess.run([
            "ffmpeg", "-y", "-f", "concat", "-safe", "0",
            "-i", str(list_file), "-c:a", "copy",
            str(out),
        ], check=True, capture_output=True)
        print(f"  → {out.name}  ({out.stat().st_size / 1024 / 1024:.1f} MB)")


# ── Main ───────────────────────────────────────────────────────────────────────

def main(stitch_only=False, voice=DEFAULT_VOICE, epub_path=EPUB_PATH, delay=0.0):
    CHUNKS_DIR.mkdir(exist_ok=True)

    print(f"Reading {epub_path}…")
    books            = extract_epub(epub_path)
    chunks, chapters = build_chunks(books)
    print(f"  {len(books)} books · {len(chapters)} chapters → {len(chunks)} TTS chunks")
    print(f"  Voice: {voice}\n")

    if stitch_only:
        stitch(chapters)
        return

    done = load_progress()
    asyncio.run(generate(chunks, done, voice, chapters, delay=delay))

    if len(done) < len(chunks):
        print(f"\n{len(chunks) - len(done)} chunks not yet generated. Run again to resume.")
        sys.exit(1)

    stitch(chapters)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--stitch-only", action="store_true",
                        help="Skip generation, just stitch existing chunks to MP3")
    parser.add_argument("--voice", default=DEFAULT_VOICE,
                        help=f"edge-tts voice name (default: {DEFAULT_VOICE})")
    parser.add_argument("--delay", type=float, default=0.0,
                        help="Seconds to wait between chunks (e.g. 0.5 to avoid rate limiting)")
    parser.add_argument("--epub", default=str(EPUB_PATH),
                        help=f"Path to epub file (default: {EPUB_PATH})")
    args = parser.parse_args()
    main(stitch_only=args.stitch_only, voice=args.voice, epub_path=Path(args.epub),
         delay=args.delay)
