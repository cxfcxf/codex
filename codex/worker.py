import asyncio
import json
import threading
from pathlib import Path

from openai import OpenAI

from codex.config import MODEL, DEFAULT_TEMPERATURE, LANG_NAMES_EN
from codex.pipeline.extract import analyze_epub_structure
from codex.pipeline.translate import build_system_prompt, translate_chunk, correct_translation
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
    tolerance_percent = max(0, min(int(config.get("tolerance_percent", 2)), 10))

    book_dir = Path(config["book_dir"])
    chapters_dir = book_dir / "translation_chapters"
    memory_path = book_dir / "translation_memory.json"
    jobs_dir = book_dir / "jobs"
    chapters_dir.mkdir(parents=True, exist_ok=True)
    jobs_dir.mkdir(parents=True, exist_ok=True)

    try:
        client = OpenAI(api_key=config["api_key"] or "dummy", base_url=config["base_url"])

        # ── Build initial memory ──────────────────────────────────────────────
        # Priority: uploaded glossary > existing book glossary > base glossary > empty
        memory: dict = {}

        if config.get("base_glossary_content"):
            try:
                base_dict = json.loads(config["base_glossary_content"])
                for k, v in base_dict.items():
                    if v.get("translation"):
                        memory[k] = {
                            "translation": v["translation"],
                            "type": v.get("type", "concept"),
                            "source": v.get("source", "official"),
                        }
                emit(type="status", message=f"Loaded base glossary ({len(memory)} terms).", phase="prepare")
            except Exception as exc:
                emit(type="status", message=f"Base glossary load failed: {exc}", phase="prepare")

        if memory_path.exists():
            saved = json.loads(memory_path.read_text(encoding="utf-8"))
            # Merge saved memory but never overwrite official base glossary entries
            for k, v in saved.items():
                if k not in memory or memory[k].get("source") != "official":
                    memory[k] = v
            emit(type="status", message=f"Loaded translation memory ({len(memory)} terms).", phase="prepare")

        def _keep_set() -> frozenset:
            return frozenset(k.lower() for k, v in memory.items() if v.get("translation", k) == k)

        keep_english = _keep_set()

        def on_new_terms(terms: list[dict]) -> int:
            added = 0
            memory_lower = {k.lower() for k in memory}
            for t in [x for x in terms if isinstance(x, dict)]:
                orig = t.get("original", "")
                trans = t.get("translation", "")
                # Models sometimes emit lists or other junk in tool args — coerce, don't crash
                if isinstance(orig, list):
                    orig = orig[0] if orig else ""
                if isinstance(trans, list):
                    trans = trans[0] if trans else ""
                if not isinstance(orig, str) or not isinstance(trans, str):
                    continue
                orig, trans = orig.strip(), trans.strip()
                if not orig or not trans:
                    continue
                lo = orig.lower()
                # Skip if already in memory (case-insensitive)
                if lo in memory_lower:
                    continue
                # Skip if plural of an existing term
                if (lo.endswith("s") and lo[:-1] in memory_lower) or \
                   (lo.endswith("es") and lo[:-2] in memory_lower):
                    continue
                ttype = t.get("type")
                memory[orig] = {
                    "translation": trans,
                    "type": ttype if isinstance(ttype, str) and ttype else "concept",
                    "source": "tm",
                }
                memory_lower.add(lo)
                added += 1
            return added

        # ── EPUB ──────────────────────────────────────────────────────────────
        emit(type="status", message="Analyzing EPUB structure…", phase="prepare")
        chapters = analyze_epub_structure(file_path)
        total = len(chapters)
        emit(type="status", message=f"{total} chapters found.", phase="prepare")

        translations: dict[str, str] = {}
        done = 0
        pending = []

        emit(type="total", total=total, phase="translate")
        emit(type="status", message="Checking resumed chapters…", phase="translate")
        for ch in chapters:
            if cancel.is_set():
                jobs[job_id]["status"] = "cancelled"
                emit(type="cancelled", message="Stopped.")
                return
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
                        glossary=memory, cancel=cancel,
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
                 message=f"Translating {len(pending)} chapters (memory: {len(memory)} terms)…",
                 phase="translate")

            for item in pending:
                if cancel.is_set():
                    jobs[job_id]["status"] = "cancelled"
                    emit(type="cancelled", message="Stopped.")
                    return

                ch = item["ch"]
                system_prompt = build_system_prompt(src_lang, tgt_lang, memory or None)

                def _on_terms(terms):
                    added = on_new_terms(terms)
                    if added:
                        nonlocal keep_english
                        keep_english = _keep_set()
                        memory_path.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")
                        emit(type="status",
                             message=f"+{added} new terms added to memory ({len(memory)} total).",
                             phase="translate")

                unit_label = [""]

                def _on_para_count(src, tgt):
                    emit(type="para_count", src=src, tgt=tgt,
                         chapter=ch["title"] + unit_label[0], phase="translate")

                part_path = item["txt_path"].with_name(item["txt_path"].name + ".part")
                part_path.unlink(missing_ok=True)
                ch_paras = [p for p in ch["text"].split("\n\n") if p.strip()]

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
                        on_new_terms=_on_terms,
                        on_para_count=_on_para_count,
                        on_mismatch=_on_mismatch,
                        glossary=memory,
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

                result = translate_unit(ch_paras, 0)
                item["txt_path"].write_text(result, encoding="utf-8")
                part_path.unlink(missing_ok=True)

                translations[ch["filename"]] = result
                done += 1
                emit(type="progress", done=done, total=total,
                     chapter=ch["title"], phase="translate")

                # Correction phase: fix any untranslated English lines
                if fix_pass:
                    corrected = correct_translation(
                        client, model, result, tgt_lang, src_lang, keep_english,
                        glossary=memory, cancel=cancel,
                    )
                    if corrected != result:
                        item["txt_path"].write_text(corrected, encoding="utf-8")
                        translations[ch["filename"]] = corrected
                        emit(type="correction", chapter=ch["title"], phase="translate")

        memory_path.write_text(json.dumps(memory, ensure_ascii=False, indent=2), encoding="utf-8")
        emit(type="status", message=f"Memory saved ({len(memory)} terms).", phase="translate")

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
