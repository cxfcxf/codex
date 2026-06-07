import asyncio
import json
import threading
import uuid
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from codex.config import WORKSPACE_DIR, DEFAULT_BASE_URL, MODEL, DEFAULT_TEMPERATURE
from codex.pipeline.extract import extract_text, suggest_settings
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
    suffix = Path(file.filename).suffix.lower()
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
    api_key: str = Form(default=""),
    base_url: str = Form(default=DEFAULT_BASE_URL),
    model: str = Form(default=MODEL),
    src_lang: str = Form("en"),
    tgt_lang: str = Form("zh"),
    output_format: str = Form("epub"),
    workers: int = Form(1),
    chunk_size: int = Form(1500),
    extract_chunk_size: int = Form(3000),
    extract_sample_pct: int = Form(10),
    overlap_words: int = Form(150),
    temperature: float = Form(default=DEFAULT_TEMPERATURE),
    base_glossary_file: Optional[UploadFile] = File(None),
    glossary_file: Optional[UploadFile] = File(None),
):
    job_id = str(uuid.uuid4())
    book_stem = Path(file.filename).stem
    book_dir = WORKSPACE_DIR / book_stem
    book_dir.mkdir(parents=True, exist_ok=True)

    suffix = Path(file.filename).suffix.lower()
    file_path = book_dir / f"input{suffix}"
    file_path.write_bytes(await file.read())

    jobs[job_id] = {"status": "pending", "result_path": None, "error": None}
    job_queues[job_id] = asyncio.Queue()
    cancel_events[job_id] = threading.Event()

    base_glossary_content = None
    if base_glossary_file and base_glossary_file.filename:
        base_glossary_content = (await base_glossary_file.read()).decode("utf-8")

    glossary_content = None
    if glossary_file and glossary_file.filename:
        glossary_content = (await glossary_file.read()).decode("utf-8")

    config = {
        "api_key": api_key.strip(),
        "base_url": base_url.strip(),
        "model": model.strip(),
        "src_lang": src_lang,
        "tgt_lang": tgt_lang,
        "output_format": output_format,
        "workers": max(1, min(workers, 20)),
        "chunk_size": max(200, min(chunk_size, 5000)),
        "extract_chunk_size": max(500, min(extract_chunk_size, 10000)),
        "extract_sample_pct": max(5, min(extract_sample_pct, 100)),
        "overlap_words": max(0, min(overlap_words, 500)),
        "base_glossary_content": base_glossary_content,
        "glossary_content": glossary_content,
        "temperature": temperature,
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
