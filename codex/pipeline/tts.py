import asyncio
import json
import re
import threading
from pathlib import Path

DEFAULT_VOICE = "zh-CN-XiaoxiaoNeural"
MAX_SYNTH_TRIES = 5  # transient 503s from the edge-tts endpoint are common

# ── Text cleanup for TTS ──────────────────────────────────────────────────────

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


# ── EPUB → chapters ───────────────────────────────────────────────────────────

def _toc_titles(path: Path) -> dict:
    """Map document basename → title from the EPUB's table of contents."""
    from ebooklib import epub

    book = epub.read_epub(str(path))
    titles: dict[str, str] = {}

    def walk(items):
        for it in items:
            entry, children = it if isinstance(it, tuple) else (it, None)
            href = getattr(entry, "href", None)
            title = getattr(entry, "title", None)
            if href and title:
                titles.setdefault(Path(href.split("#")[0]).name, title.strip())
            if children:
                walk(children)

    walk(book.toc)
    return titles


def extract_chapters(path: Path) -> list[dict]:
    """Flat [{title, paragraphs, speak_title}] — one chapter per content document,
    titled from the EPUB's own TOC."""
    from codex.pipeline.extract import analyze_epub_structure

    toc = _toc_titles(path)
    chapters: list[dict] = []
    for ch in analyze_epub_structure(path, min_para_chars=0):
        paragraphs = [clean_text(p) for p in ch["text"].split("\n\n")]
        paragraphs = [p for p in paragraphs if p]
        if not paragraphs:
            continue
        title = toc.get(Path(ch["filename"]).name) or ch["title"]
        chapters.append({
            "title": title,
            "paragraphs": paragraphs,
            # a bare numeric filename stem is not a real title — don't read it aloud
            "speak_title": not re.fullmatch(r"\d+", title),
        })
    return chapters


# ── Worker ────────────────────────────────────────────────────────────────────

def tts_worker(
    job_id: str,
    file_path: Path,
    config: dict,
    loop: asyncio.AbstractEventLoop,
    jobs: dict,
    job_queues: dict,
    cancel_events: dict,
):
    def emit(**kwargs):
        asyncio.run_coroutine_threadsafe(job_queues[job_id].put(kwargs), loop)

    def finish():
        asyncio.run_coroutine_threadsafe(job_queues[job_id].put(None), loop)

    cancel = cancel_events.get(job_id, threading.Event())
    voice = config.get("voice") or DEFAULT_VOICE
    book_dir = Path(config["book_dir"])
    tts_dir = book_dir / "tts"
    chapters_dir = tts_dir / "chapters"
    chapters_dir.mkdir(parents=True, exist_ok=True)

    try:
        emit(type="status", message="Reading EPUB…")
        items = []
        for ch in extract_chapters(file_path):
            # edge-tts splits long text internally — one request per chapter is fine
            text = "\n".join(ch["paragraphs"])
            if ch["speak_title"]:
                text = ch["title"] + "\n" + text
            items.append({"title": ch["title"], "text": text})
        total = len(items)
        if not total:
            raise ValueError("No readable text found in EPUB")
        emit(type="status", message=f"{total} chapters · {voice}")
        emit(type="total", total=total)

        # Resume only when the chapter plan and voice match the previous run
        manifest_path = tts_dir / "manifest.json"
        if manifest_path.exists():
            old = json.loads(manifest_path.read_text(encoding="utf-8"))
            if old.get("voice") != voice or old.get("total") != total:
                for p in chapters_dir.glob("*.mp3"):
                    p.unlink()
                emit(type="status", message="EPUB or voice changed — starting fresh.")
        manifest_path.write_text(
            json.dumps({"voice": voice, "total": total}, ensure_ascii=False),
            encoding="utf-8",
        )

        def out_path(idx: int, title: str) -> Path:
            safe = re.sub(r"[^\w一-鿿]+", "_", title)[:40]
            return chapters_dir / f"{idx:03d}_{safe}.mp3"

        done = sum(1 for idx, it in enumerate(items) if out_path(idx, it["title"]).exists())
        if done:
            emit(type="progress", done=done, total=total)
            emit(type="status", message=f"Resuming — {done} chapters already voiced.")

        async def synth(text: str, path: Path):
            import edge_tts
            communicate = edge_tts.Communicate(text, voice)
            await communicate.save(str(path))

        async def run_all():
            nonlocal done
            for idx, item in enumerate(items):
                if cancel.is_set():
                    return
                out = out_path(idx, item["title"])
                if out.exists():
                    continue
                emit(type="status", message=f"⟶ voicing {out.stem}…")
                part = out.with_suffix(".part")
                ok = False
                for attempt in range(MAX_SYNTH_TRIES):
                    if cancel.is_set():
                        return
                    try:
                        await synth(item["text"], part)
                        part.rename(out)
                        ok = True
                        break
                    except Exception as exc:
                        part.unlink(missing_ok=True)
                        if attempt + 1 < MAX_SYNTH_TRIES:
                            delay = min(60, 5 * 2 ** attempt)
                            emit(type="status",
                                 message=f"↻ {out.stem}: {exc} — retry "
                                         f"{attempt + 1}/{MAX_SYNTH_TRIES - 1} in {delay}s")
                            await asyncio.sleep(delay)
                        else:
                            emit(type="status",
                                 message=f"⚠ chapter failed ({out.stem}) "
                                         f"after {MAX_SYNTH_TRIES} tries: {exc}")
                if not ok:
                    continue
                done += 1
                emit(type="progress", done=done, total=total)
                emit(type="tts_chapter", name=out.stem, book=book_dir.name, file=out.name)

        asyncio.run(run_all())

        if cancel.is_set():
            jobs[job_id]["status"] = "cancelled"
            emit(type="cancelled", message="Stopped.")
            return

        jobs[job_id]["status"] = "done"
        emit(type="done", chapters=done, missing=total - done)

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        emit(type="error", message=str(e))
    finally:
        cancel_events.pop(job_id, None)
        finish()
