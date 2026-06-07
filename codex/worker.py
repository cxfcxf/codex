import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI

from codex.config import (
    MODEL, DEFAULT_TEMPERATURE,
    TRANSLATE_WORKERS, CHUNK_SIZE_WORDS, EXTRACT_CHUNK_WORDS,
    LANG_NAMES_EN,
)
from codex.pipeline.extract import extract_text, chunk_pages, analyze_epub_structure
from codex.pipeline.glossary import (
    build_extract_prompt, build_resolve_prompt, build_cap_extract_prompt,
    extract_terms_one, extract_capitalized_terms, translate_cap_terms,
    merge_terms, auto_resolve,
)
from codex.pipeline.translate import build_system_prompt, translate_chunk
from codex.pipeline.output import generate_output, build_translated_epub


def translation_worker(
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
    src_lang = config["src_lang"]
    tgt_lang = config["tgt_lang"]
    workers = config.get("workers", TRANSLATE_WORKERS)
    model = config.get("model", MODEL)
    temperature = config.get("temperature", DEFAULT_TEMPERATURE)
    lock = threading.Lock()

    # Per-book workspace directories
    book_dir = Path(config["book_dir"])
    chapters_dir = book_dir / "translation_chapters"
    glossary_path = book_dir / "glossary.json"
    jobs_dir = book_dir / "jobs"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = OpenAI(api_key=config["api_key"] or "dummy", base_url=config["base_url"])

        # ── Resolve glossary ───────────────────────────────────────────────────
        # Priority: uploaded file > existing book glossary.json > none
        glossary: dict = {}
        if config.get("glossary_content"):
            glossary = json.loads(config["glossary_content"])
            glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8")
            emit(type="status", message=f"Using uploaded glossary ({len(glossary)} terms).", phase="extract")
        elif glossary_path.exists():
            glossary = json.loads(glossary_path.read_text(encoding="utf-8"))
            emit(type="status", message=f"Loaded existing glossary ({len(glossary)} terms).", phase="extract")

        keep_english: frozenset = frozenset(
            k.lower() for k, v in glossary.items()
            if v.get("translation", k) == k
        ) if glossary else frozenset()

        # Load base glossary if provided (used as seed context for all extraction passes)
        base_raw: list[dict] = []
        if config.get("base_glossary_content"):
            try:
                base_dict = json.loads(config["base_glossary_content"])
                base_raw = [
                    {"original": k, "translation": v["translation"],
                     "type": v.get("type", "concept"), "source": v.get("source", "official")}
                    for k, v in base_dict.items() if v.get("translation")
                ]
                emit(type="status", message=f"Loaded base glossary ({len(base_raw)} terms).", phase="extract")
            except Exception as exc:
                emit(type="status", message=f"Base glossary load failed: {exc}", phase="extract")

        # ── EPUB input: preserve original structure, chapter-by-chapter ────────
        if file_path.suffix.lower() == ".epub":
            emit(type="status", message="Analyzing EPUB structure…", phase="extract")
            chapters = analyze_epub_structure(file_path)
            total = len(chapters)
            emit(type="status", message=f"{total} chapters found", phase="extract")

            # Generate glossary from EPUB text if none exists yet
            if not glossary:
                translatable = [ch for ch in chapters if ch.get("text")]

                full_text = "\n\n".join(ch["text"] for ch in translatable)
                cap_terms = extract_capitalized_terms(full_text)
                from codex.pipeline.extract import chunk_pages as _chunk_pages
                extract_chunk_size = config.get("extract_chunk_size", EXTRACT_CHUNK_WORDS)
                epub_pages = [{"page": idx + 1, "text": ch["text"]} for idx, ch in enumerate(translatable)]
                all_chunks = _chunk_pages(epub_pages, extract_chunk_size)
                sample_pct = config.get("extract_sample_pct", 20) / 100.0
                n_sample = max(2, int(len(all_chunks) * sample_pct))
                n_bands = 5
                per_band = max(1, n_sample // n_bands)
                band_size = len(all_chunks) / n_bands
                sampled = []
                for b in range(n_bands):
                    band = all_chunks[int(b * band_size): int((b + 1) * band_size)]
                    if band:
                        step = len(band) / per_band
                        sampled += [band[int(i * step)] for i in range(per_band)]

                cap_glossary_path = book_dir / "glossary_cap.json"
                chunk_glossary_path = book_dir / "glossary_chunks.json"
                cap_cached = cap_glossary_path.exists()
                chunks_cached = chunk_glossary_path.exists()
                pending = (0 if cap_cached else 1) + (0 if chunks_cached else len(sampled))
                total_steps = max(1, pending)
                done_steps = [0]
                emit(type="total", total=total_steps, phase="extract")

                def _step(label):
                    done_steps[0] += 1
                    emit(type="progress", done=done_steps[0], total=total_steps,
                         pages=label, phase="extract")

                # Pass 1: capitalized terms (base glossary as known context)
                if cap_cached:
                    cap_raw = json.loads(cap_glossary_path.read_text(encoding="utf-8"))
                    emit(type="status", message=f"Loaded cap glossary ({len(cap_raw)} terms) from cache.", phase="extract")
                else:
                    emit(type="status", message=f"Found {len(cap_terms)} capitalized terms, translating…", phase="extract")
                    cap_prompt = build_cap_extract_prompt(src_lang, tgt_lang)
                    try:
                        cap_raw = translate_cap_terms(
                            client, cap_prompt, cap_terms, model=model, temperature=temperature,
                            cancel=cancel, on_batch_done=lambda _: None,
                            known_glossary=base_raw or None,
                        )
                    except InterruptedError:
                        jobs[job_id]["status"] = "cancelled"
                        emit(type="cancelled", message="Stopped.")
                        return
                    cap_glossary_path.write_text(json.dumps(cap_raw, ensure_ascii=False, indent=2), encoding="utf-8")
                    _step("cap terms")

                if cancel.is_set():
                    jobs[job_id]["status"] = "cancelled"
                    emit(type="cancelled", message="Stopped.")
                    return

                # Pass 2: sampled chunks (base + cap as known context)
                if chunks_cached:
                    chunk_raw = json.loads(chunk_glossary_path.read_text(encoding="utf-8"))
                    emit(type="status", message=f"Loaded chunk glossary ({len(chunk_raw)} terms) from cache.", phase="extract")
                else:
                    chunk_raw = []
                    emit(type="status", message=f"Sampling {len(sampled)} segments for context extraction…", phase="extract")
                    extract_prompt = build_extract_prompt(src_lang, tgt_lang)
                    chunk_known = base_raw + cap_raw
                    for chunk in sampled:
                        if cancel.is_set():
                            jobs[job_id]["status"] = "cancelled"
                            emit(type="cancelled", message="Stopped.")
                            return
                        try:
                            _, terms = extract_terms_one(client, extract_prompt, chunk, model=model, temperature=temperature, cancel=cancel, known_glossary=chunk_known)
                            chunk_raw.extend(terms)
                        except InterruptedError:
                            jobs[job_id]["status"] = "cancelled"
                            emit(type="cancelled", message="Stopped.")
                            return
                        _step(f"pages {chunk['start_page']}–{chunk['end_page']}")
                    chunk_glossary_path.write_text(json.dumps(chunk_raw, ensure_ascii=False, indent=2), encoding="utf-8")

                glossary = merge_terms(base_raw + cap_raw + chunk_raw)
                n_conflicts = sum(1 for v in glossary.values() if v.get("conflict"))
                msg = (f"Resolving {n_conflicts} conflicts across {len(glossary)} terms…"
                       if n_conflicts else f"No conflicts — {len(glossary)} terms ready.")
                emit(type="status", message=msg, phase="resolve")

                if n_conflicts and not cancel.is_set():
                    resolve_prompt = build_resolve_prompt(src_lang, tgt_lang)
                    glossary = auto_resolve(client, resolve_prompt, glossary, model=model, temperature=temperature, cancel=cancel)

                glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8")
                emit(type="status", message=f"Glossary saved ({len(glossary)} terms).", phase="resolve")

                keep_english = frozenset(
                    k.lower() for k, v in glossary.items()
                    if v.get("translation", k) == k
                )

            system_prompt = build_system_prompt(src_lang, tgt_lang, glossary or None)

            translations: dict[str, str] = {}
            done = 0
            pending = []

            emit(type="total", total=total, phase="translate")
            emit(type="status", message="Checking resumed chapters…", phase="translate")
            for ch in chapters:
                safe = Path(ch["filename"]).stem
                txt_path = chapters_dir / f"{safe}.txt"
                if txt_path.exists():
                    translations[ch["filename"]] = txt_path.read_text(encoding="utf-8")
                    done += 1
                    emit(type="progress", done=done, total=total,
                         chapter=f"[resumed] {ch['title']}", phase="translate")
                else:
                    pending.append({"ch": ch, "txt_path": txt_path})

            if pending:
                emit(type="status",
                     message=f"Translating {len(pending)} chapters ({workers} workers)…",
                     phase="translate")

                def translate_chapter_item(item):
                    result = translate_chunk(
                        client, system_prompt,
                        {"id": 0, "text": item["ch"]["text"]},
                        tgt_lang, model=model, temperature=temperature,
                        keep=keep_english, cancel=cancel,
                    )
                    return item, result

                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = {pool.submit(translate_chapter_item, item): item for item in pending}
                    for future in as_completed(futures):
                        if cancel.is_set():
                            for f in futures:
                                f.cancel()
                            jobs[job_id]["status"] = "cancelled"
                            emit(type="cancelled", message="Stopped.")
                            return
                        item, result = future.result()
                        ch = item["ch"]
                        item["txt_path"].write_text(result, encoding="utf-8")
                        with lock:
                            translations[ch["filename"]] = result
                            done += 1
                            emit(type="progress", done=done, total=total,
                                 chapter=ch["title"], phase="translate")

            emit(type="status", message="Generating output file…", phase="generate")
            lang = LANG_NAMES_EN.get(tgt_lang, tgt_lang)
            stem = config.get("original_stem", file_path.stem)
            out_path = jobs_dir / f"{stem}_{lang}.epub"
            result_path = build_translated_epub(file_path, translations, out_path)

        # ── Non-EPUB: full glossary pipeline ───────────────────────────────────
        else:
            emit(type="status", message="Reading file…", phase="extract")
            pages = extract_text(file_path)
            extract_chunk_size = config.get("extract_chunk_size", EXTRACT_CHUNK_WORDS)
            chunk_size = config.get("chunk_size", CHUNK_SIZE_WORDS)

            if not glossary:
                full_text = "\n\n".join(p["text"] for p in pages)
                cap_terms = extract_capitalized_terms(full_text)
                all_chunks = chunk_pages(pages, extract_chunk_size)
                sample_pct = config.get("extract_sample_pct", 20) / 100.0
                n_sample = max(2, int(len(all_chunks) * sample_pct))
                n_bands = 5
                per_band = max(1, n_sample // n_bands)
                band_size = len(all_chunks) / n_bands
                sampled = []
                for b in range(n_bands):
                    band = all_chunks[int(b * band_size): int((b + 1) * band_size)]
                    if band:
                        step = len(band) / per_band
                        sampled += [band[int(i * step)] for i in range(per_band)]

                cap_glossary_path = book_dir / "glossary_cap.json"
                chunk_glossary_path = book_dir / "glossary_chunks.json"
                cap_cached = cap_glossary_path.exists()
                chunks_cached = chunk_glossary_path.exists()
                pending = (0 if cap_cached else 1) + (0 if chunks_cached else len(sampled))
                total_steps = max(1, pending)
                done_steps = [0]
                emit(type="total", total=total_steps, phase="extract")

                def _step(label):
                    done_steps[0] += 1
                    emit(type="progress", done=done_steps[0], total=total_steps,
                         pages=label, phase="extract")

                # Pass 1: capitalized terms (base glossary as known context)
                if cap_cached:
                    cap_raw = json.loads(cap_glossary_path.read_text(encoding="utf-8"))
                    emit(type="status", message=f"Loaded cap glossary ({len(cap_raw)} terms) from cache.", phase="extract")
                else:
                    emit(type="status", message=f"Found {len(cap_terms)} capitalized terms, translating…", phase="extract")
                    cap_prompt = build_cap_extract_prompt(src_lang, tgt_lang)
                    try:
                        cap_raw = translate_cap_terms(
                            client, cap_prompt, cap_terms, model=model, temperature=temperature,
                            cancel=cancel, on_batch_done=lambda _: None,
                            known_glossary=base_raw or None,
                        )
                    except InterruptedError:
                        jobs[job_id]["status"] = "cancelled"
                        emit(type="cancelled", message="Stopped.")
                        return
                    cap_glossary_path.write_text(json.dumps(cap_raw, ensure_ascii=False, indent=2), encoding="utf-8")
                    _step("cap terms")

                if cancel.is_set():
                    jobs[job_id]["status"] = "cancelled"
                    emit(type="cancelled", message="Stopped.")
                    return

                # Pass 2: sampled chunks (base + cap as known context)
                if chunks_cached:
                    chunk_raw = json.loads(chunk_glossary_path.read_text(encoding="utf-8"))
                    emit(type="status", message=f"Loaded chunk glossary ({len(chunk_raw)} terms) from cache.", phase="extract")
                else:
                    chunk_raw = []
                    emit(type="status", message=f"Sampling {len(sampled)} segments for context extraction…", phase="extract")
                    extract_prompt = build_extract_prompt(src_lang, tgt_lang)
                    chunk_known = base_raw + cap_raw
                    for chunk in sampled:
                        if cancel.is_set():
                            jobs[job_id]["status"] = "cancelled"
                            emit(type="cancelled", message="Stopped.")
                            return
                        try:
                            _, terms = extract_terms_one(client, extract_prompt, chunk, model=model, temperature=temperature, cancel=cancel, known_glossary=chunk_known)
                            chunk_raw.extend(terms)
                        except InterruptedError:
                            jobs[job_id]["status"] = "cancelled"
                            emit(type="cancelled", message="Stopped.")
                            return
                        _step(f"pages {chunk['start_page']}–{chunk['end_page']}")
                    chunk_glossary_path.write_text(json.dumps(chunk_raw, ensure_ascii=False, indent=2), encoding="utf-8")

                glossary = merge_terms(base_raw + cap_raw + chunk_raw)
                n_conflicts = sum(1 for v in glossary.values() if v.get("conflict"))
                msg = (f"Resolving {n_conflicts} conflicts across {len(glossary)} terms…"
                       if n_conflicts else f"No conflicts — {len(glossary)} terms ready.")
                emit(type="status", message=msg, phase="resolve")

                if n_conflicts and not cancel.is_set():
                    resolve_prompt = build_resolve_prompt(src_lang, tgt_lang)
                    glossary = auto_resolve(client, resolve_prompt, glossary, model=model, temperature=temperature, cancel=cancel)

                glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8")
                emit(type="status", message=f"Glossary saved ({len(glossary)} terms).", phase="resolve")

                keep_english = frozenset(
                    k.lower() for k, v in glossary.items()
                    if v.get("translation", k) == k
                )

            if cancel.is_set():
                jobs[job_id]["status"] = "cancelled"
                emit(type="cancelled", message="Stopped.")
                return

            translate_chunks = chunk_pages(pages, chunk_size, with_overlap=True)
            total_translate = len(translate_chunks)
            emit(type="total", total=total_translate, phase="translate")
            emit(type="status", message=f"Translating {total_translate} segments…", phase="translate")

            system_prompt = build_system_prompt(src_lang, tgt_lang, glossary)
            completed = {}
            done_translate = [0]

            def do_translate(chunk):
                return chunk, translate_chunk(
                    client, system_prompt, chunk, tgt_lang,
                    model=model, temperature=temperature,
                    keep=keep_english, cancel=cancel,
                )

            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(do_translate, c): c for c in translate_chunks}
                for future in as_completed(futures):
                    if cancel.is_set():
                        for f in futures:
                            f.cancel()
                        jobs[job_id]["status"] = "cancelled"
                        emit(type="cancelled", message="Stopped.")
                        return
                    chunk, translation = future.result()
                    with lock:
                        done_translate[0] += 1
                        completed[str(chunk["id"])] = translation
                        emit(type="progress", done=done_translate[0], total=total_translate,
                             pages=f"{chunk['start_page']}–{chunk['end_page']}", phase="translate")

            emit(type="status", message="Generating output file…", phase="generate")
            ext = {"epub": ".epub", "pdf": ".pdf", "html": ".html", "txt": ".txt"}[config["output_format"]]
            out_path = jobs_dir / f"{file_path.stem}{ext}"
            result_path = generate_output(translate_chunks, completed, file_path.stem, config["output_format"], out_path)

        jobs[job_id]["status"] = "done"
        jobs[job_id]["result_path"] = result_path
        emit(type="done", filename=result_path.name, size=result_path.stat().st_size)

    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        emit(type="error", message=str(e))
    finally:
        cancel_events.pop(job_id, None)
        finish()
