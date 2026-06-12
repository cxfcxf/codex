import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from openai import OpenAI

from codex.config import MODEL, DEFAULT_TEMPERATURE, LANG_NAMES_EN
from codex.pipeline.extract import analyze_epub_structure
from codex.pipeline.translate import build_system_prompt, scout_chapter, translate_chunk, correct_translation
from codex.pipeline.output import build_translated_epub

PARA_FLOOR = 50    # never bisect into blocks smaller than this many paragraphs
SPLIT_TRIES = 1    # a splittable block that misses once goes straight to bisection
FLOOR_TRIES = 5    # shape attempts at the floor before keeping the closest


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
    model = config.get("model", MODEL)
    temperature = DEFAULT_TEMPERATURE
    fix_pass = config.get("fix_pass", False)
    tolerance_percent = max(0, min(int(config.get("tolerance_percent", 1)), 10))
    workers = max(1, min(int(config.get("workers", 1)), 8))
    block_paras = max(PARA_FLOOR, min(int(config.get("block_paras", 250)), 1000))

    book_dir = Path(config["book_dir"])
    chapters_dir = book_dir / "translation_chapters"
    glossary_path = book_dir / "glossary.json"
    legacy_glossary_path = book_dir / "translation_memory.json"
    scouted_path = book_dir / "scouted.json"
    jobs_dir = book_dir / "jobs"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = OpenAI(api_key=config["api_key"] or "dummy", base_url=config["base_url"])

        # ── Phase 1: PREPARE ─────────────────────────────────────────────────
        # Build the initial glossary.
        # Priority: uploaded base glossary > saved book glossary > empty
        glossary: dict = {}

        if config.get("base_glossary_content"):
            try:
                base_dict = json.loads(config["base_glossary_content"])
                for k, v in base_dict.items():
                    if v.get("translation"):
                        glossary[k] = {
                            "translation": v["translation"],
                            "type": v.get("type", "concept"),
                            "source": v.get("source", "official"),
                        }
                emit(type="status", message=f"Loaded base glossary ({len(glossary)} terms).", phase="prepare")
            except Exception as exc:
                emit(type="status", message=f"Base glossary load failed: {exc}", phase="prepare")

        saved_glossary_path = glossary_path if glossary_path.exists() else legacy_glossary_path
        if saved_glossary_path.exists():
            saved = json.loads(saved_glossary_path.read_text(encoding="utf-8"))
            # Merge saved terms but never overwrite official base glossary entries
            for k, v in saved.items():
                if k not in glossary or glossary[k].get("source") != "official":
                    glossary[k] = v
            emit(type="status", message=f"Loaded book glossary ({len(glossary)} terms).", phase="prepare")

        def save_glossary():
            glossary_path.write_text(json.dumps(glossary, ensure_ascii=False, indent=2), encoding="utf-8")

        def _keep_set() -> frozenset:
            return frozenset(k.lower() for k, v in glossary.items() if v.get("translation", k) == k)

        def on_new_terms(terms: list[dict]) -> int:
            added = 0
            coerced = 0
            dropped = 0
            glossary_lower = {k.lower() for k in glossary}
            for t in [x for x in terms if isinstance(x, dict)]:
                orig = t.get("original", "")
                trans = t.get("translation", "")
                # Models sometimes emit lists or other junk in tool args — coerce,
                # don't crash. Only the lenient final scout attempt reaches here
                # with bad shapes, and it must not pass silently.
                if isinstance(orig, list):
                    orig = orig[0] if orig else ""
                    coerced += 1
                if isinstance(trans, list):
                    trans = trans[0] if trans else ""
                    coerced += 1
                if not isinstance(orig, str) or not isinstance(trans, str):
                    dropped += 1
                    continue
                orig, trans = orig.strip(), trans.strip()
                if not orig or not trans:
                    continue
                lo = orig.lower()
                # Skip if already known (case-insensitive)
                if lo in glossary_lower:
                    continue
                # Skip if plural of an existing term
                if (lo.endswith("s") and lo[:-1] in glossary_lower) or \
                   (lo.endswith("es") and lo[:-2] in glossary_lower):
                    continue
                ttype = t.get("type")
                glossary[orig] = {
                    "translation": trans,
                    "type": ttype if isinstance(ttype, str) and ttype else "concept",
                    "source": "tm",
                }
                glossary_lower.add(lo)
                added += 1
            if coerced or dropped:
                emit(type="status",
                     message=f"⚠ scout sent malformed terms after retries: "
                             f"{coerced} list value(s) coerced to first element, {dropped} entr(ies) dropped",
                     phase="scout")
            return added

        emit(type="status", message="Analyzing EPUB structure…", phase="prepare")
        chapters = analyze_epub_structure(file_path)
        total = len(chapters)
        emit(type="status", message=f"{total} chapters found.", phase="prepare")

        # ── Phase 2: SCOUT ───────────────────────────────────────────────────
        # Build the complete glossary for the whole book before translating a
        # single line. Tool calling is confined to this phase: if it misbehaves,
        # nothing has been translated yet. Resumable per chapter via scouted.json.
        scouted: set = set()
        if scouted_path.exists():
            try:
                scouted = set(json.loads(scouted_path.read_text(encoding="utf-8")))
            except Exception:
                scouted = set()

        to_scout = [ch for ch in chapters if ch["filename"] not in scouted]
        emit(type="total", total=total, phase="scout")
        if to_scout:
            emit(type="status",
                 message=f"Scouting {len(to_scout)} chapters for new terms "
                         f"(glossary: {len(glossary)}, {workers} scribe{'s' if workers > 1 else ''})…",
                 phase="scout")
        scout_done = total - len(to_scout)
        if scout_done:
            emit(type="progress", done=scout_done, total=total,
                 chapter=f"[resumed] {scout_done} chapters already scouted", phase="scout")
        # Scout stays sequential: scouts are short requests, and cycling 4 slots
        # through them thrashes llama.cpp's prompt cache (gemma's SWA makes saved
        # states unrestorable — every switch is a full save/evict/reprocess).
        # Measured: 4-way scout decode collapsed to ~11 t/s/slot vs ~50 sequential.
        for ch in to_scout:
            if cancel.is_set():
                raise InterruptedError("cancelled")
            emit(type="status", message=f"⟶ scouting {ch['title']}…", phase="scout")
            before = len(glossary)
            scout_chapter(
                client, model, ch["text"], tgt_lang, src_lang=src_lang,
                temperature=temperature, glossary=glossary,
                on_new_terms=on_new_terms, cancel=cancel,
            )
            added = len(glossary) - before
            # Glossary first, marker second: a crash between the two re-scouts
            # the chapter (harmless, dedup) instead of losing its terms forever
            save_glossary()
            scouted.add(ch["filename"])
            scouted_path.write_text(json.dumps(sorted(scouted), ensure_ascii=False, indent=2), encoding="utf-8")
            scout_done += 1
            note = f" · +{added} terms" if added else ""
            emit(type="progress", done=scout_done, total=total,
                 chapter=f"{ch['title']}{note} (glossary: {len(glossary)})", phase="scout")
        emit(type="status", message=f"Glossary complete: {len(glossary)} terms.", phase="scout")

        keep_english = _keep_set()

        # ── Phase 3: TRANSLATE ───────────────────────────────────────────────
        translations: dict[str, str] = {}
        done = 0
        pending = []

        emit(type="total", total=total, phase="translate")
        emit(type="status", message="Checking resumed chapters…", phase="translate")
        for ch in chapters:
            if cancel.is_set():
                raise InterruptedError("cancelled")
            safe = Path(ch["filename"]).stem
            txt_path = chapters_dir / f"{safe}.txt"
            if txt_path.exists():
                saved_text = txt_path.read_text(encoding="utf-8")
                corrected = saved_text
                if fix_pass:
                    # A restart between the translate write and the correction
                    # write leaves uncorrected text on disk — re-check on resume.
                    corrected = correct_translation(
                        client, model, saved_text, tgt_lang, src_lang, keep_english,
                        glossary=glossary, cancel=cancel,
                    )
                    if corrected != saved_text:
                        txt_path.write_text(corrected, encoding="utf-8")
                        emit(type="correction", chapter=f"[resumed] {ch['title']}", phase="translate")
                translations[ch["filename"]] = corrected
                done += 1
                emit(type="progress", done=done, total=total,
                     chapter=f"[resumed] {ch['title']}", phase="translate")
            else:
                pending.append({"ch": ch, "txt_path": txt_path})

        if pending:
            emit(type="status",
                 message=f"Translating {len(pending)} chapters (glossary: {len(glossary)} terms, "
                         f"{workers} scribe{'s' if workers > 1 else ''})…",
                 phase="translate")

            # The glossary is frozen after the scout phase, so chapters are
            # independent — safe to translate in parallel. Shared progress
            # state is guarded by a lock; real speedup needs llama.cpp
            # running with multiple slots (-np N).
            state_lock = threading.Lock()
            done_count = [done]

            def translate_one(item):
                ch = item["ch"]
                system_prompt = build_system_prompt(src_lang, tgt_lang, glossary or None)

                unit_label = [""]

                def _on_para_count(src, tgt):
                    emit(type="para_count", src=src, tgt=tgt,
                         chapter=ch["title"] + unit_label[0], phase="translate")

                part_path = item["txt_path"].with_name(item["txt_path"].name + ".part")
                part_path.unlink(missing_ok=True)
                ch_paras = [p for p in ch["text"].split("\n\n") if p.strip()]

                # Pre-split oversized chapters: a monster chapter is guaranteed
                # to fail its whole-chapter shape check, so attempting it burns
                # a full generation before bisection starts. Blocks are balanced
                # (269 @ 250 -> 135+134, never 250+19 or an over-ceiling 269),
                # so block_paras is a hard ceiling. Bisection still backstops.
                n = len(ch_paras)
                if n > block_paras:
                    k = -(-n // block_paras)  # ceil division
                    base, extra = divmod(n, k)
                    groups, idx = [], 0
                    for i in range(k):
                        size = base + (1 if i < extra else 0)
                        groups.append(ch_paras[idx:idx + size])
                        idx += size
                else:
                    groups = [ch_paras]

                blocks_note = f" in {len(groups)} blocks" if len(groups) > 1 else ""
                emit(type="status", message=f"⟶ {ch['title']} ({len(ch_paras)} ¶{blocks_note})…", phase="translate")

                def translate_unit(paras: list, start: int) -> str:
                    """Translate a paragraph block; if its shape keeps failing,
                    bisect and recurse. Blocks under 2×floor can't split — they
                    get the full retry budget and keep the closest attempt."""
                    if cancel.is_set():
                        raise InterruptedError("cancelled")
                    whole = len(paras) == len(ch_paras)
                    label = "" if whole else f" · ¶{start + 1}–{start + len(paras)}"
                    unit_label[0] = label
                    splittable = len(paras) >= 2 * PARA_FLOOR
                    if not whole:
                        emit(type="status",
                             message=f"{ch['title']}{label} ({len(paras)} ¶)…",
                             phase="translate")

                    def _on_mismatch(att, max_t, s, t):
                        emit(type="status",
                             message=f"↻ {ch['title']}{label} shape retry {att}/{max_t}: {s} → {t}",
                             phase="translate")

                    text, ok = translate_chunk(
                        client, system_prompt,
                        {"id": start, "text": "\n\n".join(paras)},
                        tgt_lang, src_lang=src_lang,
                        model=model, temperature=temperature,
                        cancel=cancel,
                        on_para_count=_on_para_count,
                        on_mismatch=_on_mismatch,
                        glossary=glossary,
                        tolerance_percent=tolerance_percent,
                        mismatch_tries=SPLIT_TRIES if splittable else FLOOR_TRIES,
                    )
                    text = text.strip()
                    if ok or not splittable:
                        if not ok:
                            emit(type="status",
                                 message=f"⚠ {ch['title']}{label} — floor reached, kept closest attempt",
                                 phase="translate")
                        with part_path.open("a", encoding="utf-8") as pf:
                            pf.write(text + "\n\n")
                        return text
                    mid = len(paras) // 2
                    emit(type="status",
                         message=f"✂ {ch['title']}{label} shape check failed — splitting into halves",
                         phase="translate")
                    left = translate_unit(paras[:mid], start)
                    right = translate_unit(paras[mid:], start + mid)
                    return left + "\n\n" + right

                parts = []
                start = 0
                for g in groups:
                    parts.append(translate_unit(g, start))
                    start += len(g)
                result = "\n\n".join(parts)
                item["txt_path"].write_text(result, encoding="utf-8")
                part_path.unlink(missing_ok=True)

                # Correction phase: fix any untranslated English lines
                if fix_pass:
                    corrected = correct_translation(
                        client, model, result, tgt_lang, src_lang, keep_english,
                        glossary=glossary, cancel=cancel,
                    )
                    if corrected != result:
                        item["txt_path"].write_text(corrected, encoding="utf-8")
                        emit(type="correction", chapter=ch["title"], phase="translate")
                        result = corrected

                with state_lock:
                    translations[ch["filename"]] = result
                    done_count[0] += 1
                    emit(type="progress", done=done_count[0], total=total,
                         chapter=ch["title"], phase="translate")

            if workers == 1:
                for item in pending:
                    if cancel.is_set():
                        raise InterruptedError("cancelled")
                    translate_one(item)
            else:
                # Biggest chapters first: the run then ends on quick small ones
                # instead of three idle scribes waiting for one monster chapter
                pending.sort(key=lambda it: len(it["ch"]["text"]), reverse=True)
                with ThreadPoolExecutor(max_workers=workers) as pool:
                    futures = [pool.submit(translate_one, item) for item in pending]
                    try:
                        for f in as_completed(futures):
                            f.result()
                    except BaseException:
                        # Stop the other scribes quickly and don't start queued ones
                        cancel.set()
                        for f in futures:
                            f.cancel()
                        raise

        save_glossary()
        emit(type="status", message=f"Glossary saved ({len(glossary)} terms).", phase="translate")

        # ── Phase 4: BIND ────────────────────────────────────────────────────
        emit(type="status", message="Generating output file…", phase="generate")
        lang = LANG_NAMES_EN.get(tgt_lang, tgt_lang)
        stem = config.get("original_stem", file_path.stem)
        out_path = jobs_dir / f"{stem}_{lang}.epub"
        result_path = build_translated_epub(file_path, translations, out_path)

        jobs[job_id]["status"] = "done"
        jobs[job_id]["result_path"] = result_path
        emit(type="done", filename=result_path.name, size=result_path.stat().st_size)

    except InterruptedError:
        jobs[job_id]["status"] = "cancelled"
        emit(type="cancelled", message="Stopped.")
    except Exception as e:
        jobs[job_id]["status"] = "error"
        jobs[job_id]["error"] = str(e)
        emit(type="error", message=str(e))
    finally:
        cancel_events.pop(job_id, None)
        finish()
