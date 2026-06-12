import asyncio
import json
import re
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from codex.config import WORKSPACE_DIR, DEFAULT_BASE_URL, MODEL
from codex.pipeline.extract import extract_text, suggest_settings
from codex.pipeline.tts import tts_worker, DEFAULT_VOICE
from codex.worker import translation_worker

app = FastAPI()


@app.middleware("http")
async def no_cache_static(request, call_next):
    response = await call_next(request)
    if request.url.path.endswith((".js", ".css")):
        response.headers["Cache-Control"] = "no-cache, no-store"
    return response


jobs: dict = {}
job_queues: dict = {}
cancel_events: dict = {}


@app.post("/api/analyze")
async def analyze_file(file: UploadFile = File(...)):
    import tempfile
    suffix = Path(file.filename or "file").suffix.lower()
    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)
    try:
        pages = extract_text(tmp_path)
        return suggest_settings(pages)
    except Exception as e:
        raise HTTPException(400, f"Could not read file: {e}")
    finally:
        tmp_path.unlink(missing_ok=True)


@app.post("/api/translate")
async def start_translation(
    file: UploadFile = File(...),
    base_url: str = Form(default=DEFAULT_BASE_URL),
    model: str = Form(default=MODEL),
    src_lang: str = Form("en"),
    tgt_lang: str = Form("zh"),
    workers: int = Form(1),
    tolerance_percent: int = Form(2),
    fix_pass: bool = Form(False),
    base_glossary_file: Optional[UploadFile] = File(None),
):
    job_id = str(uuid.uuid4())
    filename = file.filename or "book"
    if Path(filename).suffix.lower() != ".epub":
        raise HTTPException(400, "Only EPUB manuscripts are supported.")
    book_stem = Path(filename).stem
    book_dir = WORKSPACE_DIR / book_stem
    book_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(filename).suffix.lower()
    file_path = book_dir / f"input{suffix}"
    file_path.write_bytes(await file.read())

    jobs[job_id] = {"status": "pending", "result_path": None, "error": None}
    job_queues[job_id] = asyncio.Queue()
    cancel_events[job_id] = threading.Event()

    base_glossary_content = None
    if base_glossary_file and base_glossary_file.filename:
        base_glossary_content = (await base_glossary_file.read()).decode("utf-8")

    config = {
        "api_key": "dummy",
        "base_url": base_url.strip(),
        "model": model.strip(),
        "src_lang": src_lang,
        "tgt_lang": tgt_lang,
        "workers": max(1, min(workers, 20)),
        "tolerance_percent": max(0, min(tolerance_percent, 10)),
        "fix_pass": fix_pass,
        "base_glossary_content": base_glossary_content,
        "original_stem": book_stem,
        "book_dir": str(book_dir),
    }

    loop = asyncio.get_event_loop()
    thread = threading.Thread(
        target=translation_worker,
        args=(job_id, file_path, config, loop, jobs, job_queues, cancel_events),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


@app.post("/api/tts")
async def start_tts(file: UploadFile = File(...), voice: str = Form(default=DEFAULT_VOICE)):
    filename = file.filename or "book"
    if not filename.lower().endswith(".epub"):
        raise HTTPException(400, "EPUB file required")
    job_id = str(uuid.uuid4())
    book_dir = WORKSPACE_DIR / Path(filename).stem
    (book_dir / "tts").mkdir(parents=True, exist_ok=True)
    file_path = book_dir / "tts" / "input.epub"
    file_path.write_bytes(await file.read())

    jobs[job_id] = {"status": "pending", "result_path": None, "error": None}
    job_queues[job_id] = asyncio.Queue()
    cancel_events[job_id] = threading.Event()

    config = {"voice": voice.strip() or DEFAULT_VOICE, "book_dir": str(book_dir)}
    loop = asyncio.get_event_loop()
    thread = threading.Thread(
        target=tts_worker,
        args=(job_id, file_path, config, loop, jobs, job_queues, cancel_events),
        daemon=True,
    )
    thread.start()
    return {"job_id": job_id}


@app.get("/api/tts/library")
async def tts_library():
    books = []
    for d in sorted(WORKSPACE_DIR.iterdir()):
        ch_dir = d / "tts" / "chapters"
        if not ch_dir.is_dir():
            continue
        files = sorted(ch_dir.glob("*.mp3"))
        if files:
            books.append({
                "book": d.name,
                "chapters": [{"name": f.stem, "file": f.name} for f in files],
            })
    return books


_VOICE_RE = re.compile(r"^[a-zA-Z]{2,3}-[a-zA-Z]{2,10}-[a-zA-Z0-9]+$")


@app.get("/api/tts/preview/{voice}")
async def tts_voice_preview(voice: str):
    if not _VOICE_RE.match(voice):
        raise HTTPException(400, "Invalid voice name")
    cache_dir = WORKSPACE_DIR / "_voice_previews"
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / f"{voice}.mp3"
    if not path.exists():
        import edge_tts
        sample = ("你好，这是本朗读声音的预览。愿黄金王座的光辉指引你。"
                  if voice.lower().startswith("zh") else
                  "Hello, this is a preview of this narration voice.")
        try:
            await edge_tts.Communicate(sample, voice).save(str(path))
        except Exception as e:
            path.unlink(missing_ok=True)
            raise HTTPException(502, f"Preview failed: {e}")
    return FileResponse(str(path), media_type="audio/mpeg")


@app.get("/api/tts/audio/{book}/{filename}")
async def tts_audio(book: str, filename: str):
    path = (WORKSPACE_DIR / book / "tts" / "chapters" / filename).resolve()
    if not path.is_relative_to(WORKSPACE_DIR.resolve()) or not path.exists():
        raise HTTPException(404, "Not found")
    return FileResponse(str(path), media_type="audio/mpeg")


@app.get("/api/progress/{job_id}")
async def progress_stream(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")

    async def event_generator():
        queue = job_queues[job_id]
        while True:
            event = await queue.get()
            if event is None:
                break
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.post("/api/cancel/{job_id}")
async def cancel_job(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    if job_id in cancel_events:
        cancel_events[job_id].set()
    jobs[job_id]["status"] = "cancelled"
    return {"ok": True}


@app.get("/api/download/{job_id}")
async def download_file(job_id: str):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    result_path = jobs[job_id].get("result_path")
    if not result_path or not Path(str(result_path)).exists():
        raise HTTPException(404, "Output file not ready")
    p = Path(str(result_path))
    return FileResponse(str(p), filename=p.name)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
