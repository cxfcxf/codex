# Codex — Book Translator & Audiobook Engine

A self-hosted web app that translates whole books (EPUB in, EPUB out) with a
local LLM served by llama.cpp, then optionally renders the translated EPUB into a
per-chapter MP3 audiobook with Microsoft Edge neural TTS.

Built for long, terminology-heavy fiction (e.g. Warhammer 40K omnibuses,
English → Chinese), where name consistency and structural fidelity matter more
than raw speed. Input is EPUB only — convert anything else upstream, where you
can see the file and make book-specific decisions.

## How translation works

A job runs four phases against an OpenAI-compatible endpoint (thinking disabled
on every call): **Prepare → Scout → Translate → Bind**.

1. **Prepare** — loads the uploaded base glossary (entries tagged `official`,
   never overwritten) and the book's saved `glossary.json`, then analyzes the
   EPUB structure.
2. **Scout** — walks the whole book chapter by chapter *before anything is
   translated*, finding proper nouns not yet in the glossary and registering
   them via an `update_memory` tool call. Sequential by design: each chapter's
   prompt carries the terms discovered so far (filtered to what appears in that
   chapter), so naming is deterministic and dedup is cheap. Tool calling is
   confined to this phase — if the model misbehaves, nothing has been translated
   yet. The call uses `tool_choice: "auto"` (forcing it via grammar masks EOS
   and invites runaway output); args are strictly validated, malformed output is
   quoted back to the model for a retry, the last attempt accepts leniently
   (coercions are flagged in the console), and a persistently failing scout
   degrades to "no term discovery" instead of failing the job. Output is capped
   by chapter size (4k–8k tokens) so a runaway dies in seconds. Resumable per
   chapter via `scouted.json`.
3. **Translate** — chapters translate with the *complete* book glossary, so a
   name's translation is consistent everywhere regardless of where it first
   appears. With **Parallel Scribes** > 1, chapters fan out across llama.cpp
   slots (largest first, so the run ends on quick small chapters); the frozen
   glossary is what makes this safe. Structure is enforced per block:
   - **Block Size** (default 250 ¶) pre-splits oversized chapters into balanced
     blocks under the ceiling (818 ¶ → 205+205+204+204) — a monster chapter
     never wastes a doomed whole-chapter attempt.
   - The prompt states the block's exact paragraph count; output must land
     within **¶ Tolerance** (default 1%, floor ±1). A miss quotes the actual
     wrong count back to the model for one retry, then the block is
     **bisected** — halves, quarters, down to a 50-¶ floor. Floor blocks get 5
     corrective retries, then the closest attempt is kept (flagged ⚠).
   - Every call's output is capped at 3× its source size, with truncation
     detected explicitly (`finish_reason`) — a capped attempt always counts as
     failed and can never be kept as best, so runaway loops cost minutes, not
     context windows, and truncated text can't reach the book.
   - Every dispatch, retry, split, and kept-best is logged live to the console.
4. **Bind** — the translated EPUB is rebuilt from the original zip (same
   structure, same images), so it round-trips cleanly into the audiobook side.

An optional **Correction Pass** (default off) scans the output for leftover
source-language words; each flagged line is re-sent to the LLM (up to 3 tries),
and whatever survives is patched word-by-word through Google Translate
(`deep-translator`), cached and batched. Turn it on for models that leave
English behind; leave it off for models that translate cleanly in one go.

Other behaviors worth knowing:

- **Glossary filtering** — only terms that actually appear in the current block
  (word-boundary match, tolerant of plurals/possessives) are shipped in the
  prompt, so a 1600-term glossary costs ~40 lines per request instead of 1600.
- **Keep-words** — glossary entries whose translation equals the original
  (e.g. `WAAAGH!`) are deliberately left in English by the correction pass.
- **Resume** — both phases checkpoint per chapter: scouted chapters are listed
  in `scouted.json` (glossary saved first, marker second, so a crash can lose
  at most one chapter's worth of re-scoutable work); finished translations are
  saved as `workspace/<book>/translation_chapters/NNN.txt` and skipped on
  re-run. In-progress blocks append to a `.txt.part` sidecar, and the chapter
  file is only written whole, so a restart can't bind a half-translated
  chapter. A crashed llama.cpp just stalls the job — it retries indefinitely
  and continues when the server comes back.
- **Curation window** — after the scout phase the glossary is complete and
  nothing is translated yet: halt the job, edit `glossary.json` by hand, and
  re-submit; translation will use your corrections everywhere.

## How the audiobook works

- The translated EPUB is split exactly like the translator splits it: one content
  document = one chapter, in spine order. Chapter titles come from the EPUB's own
  table of contents (no language-specific heuristics); documents without a TOC
  entry keep their filename stem as a label but don't have it read aloud.
- Each chapter is sent to `edge-tts` as a single request (it chunks internally)
  and comes back as one MP3 in `workspace/<book>/tts/chapters/`. The console
  shows each chapter as it's dispatched (`⟶ voicing …`), and transient endpoint
  failures (the occasional 503) are retried up to 5 times with exponential
  backoff (5s → 60s) before a chapter is given up on.
- Generation resumes per chapter; changing the voice or the EPUB starts fresh.
- The web player lists chapters as they finish, supports auto-advance, voice
  previews, and remembers the last played chapter & position per book
  (localStorage, keyed by the EPUB filename).
- Past the Lectorium, the **Archivum** (page V) shelves every book in the
  workspace with at least one voiced chapter — finished or mid-run — and the
  **Auditorium** (page VI) opens the chosen volume's chapters in its own
  player, resuming from your bookmark. One lectern speaks at a time.

## Running

Requirements: Python 3.12, a running [llama.cpp](https://github.com/ggml-org/llama.cpp)
server with an OpenAI-compatible API (default `http://127.0.0.1:8080/v1`; the
model name field is cosmetic for llama.cpp), and network access for the optional
Google fallback + edge-tts.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn codex.main:app --port 8000
```

Open `http://localhost:8000`, drop an EPUB on the left page, tune the settings
under **Apparatus** — endpoint, model, parallel scribes, block size, ¶
tolerance, correction pass — optionally upload a base glossary
(`{"Term": {"translation": "...", "type": "...", "source": "official"}}`),
and start. The right page takes any EPUB (typically the translated output) and
turns it into an audiobook.

Parallel Scribes needs llama.cpp running with multiple slots (`-np 4`; note
`-c` is the *total* context, shared across slots, and speculative decoding
forces single-slot). Per-stream speed drops as slots share memory bandwidth,
but aggregate throughput — what sets the book's wall-clock — goes up ~1.7× at
4 slots. The scout phase stays sequential regardless: short requests cycling
several slots thrash llama.cpp's prompt cache for a net loss.

### Comparing translations

`scripts/compare_translations.py` scores two translated EPUBs against their
common source: structural drift, untranslated residue, parenthetical-gloss
artifacts, duplicated paragraphs — plus an optional blind A/B judge
(`--judge N`) that samples aligned passages and asks the local LLM which
translation is better, with labels randomized per sample:

```bash
.venv/bin/python scripts/compare_translations.py source.epub a.epub b.epub \
    --label-a qwen --label-b gemma --judge 16
```

### Docker

```bash
docker build -t codex-translator .
docker run -p 8000:8000 -v "$PWD/workspace:/app/workspace" codex-translator
```

Mount `workspace/` to keep translation memory, finished chapters, and audio
across container restarts. The container reaches the llama.cpp server over the
network, so point Base URL at a host the container can resolve.

## Layout

```
codex/
  main.py               FastAPI app: upload, SSE progress, TTS endpoints
  worker.py             per-book job (prepare → scout → translate → bind)
  config.py             defaults (model, retries, workspace path)
  pipeline/
    extract.py          EPUB → chapters (structure analysis)
    translate.py        scout & translate calls, glossary filtering, shape
                        retries, output caps, Google fallback
    tts.py              EPUB → per-chapter MP3 via edge-tts
    output.py           rebuild the translated EPUB from the original zip
scripts/
  compare_translations.py   metrics + blind LLM judge for two translations
static/                 web UI (vanilla JS, no build step)
workspace/<book>/       glossary.json, scouted.json, translation_chapters/, tts/, jobs/
streaming.log           raw LLM stream for the call in flight (single-scribe debugging;
                        interleaved and truncated per-call when scribes > 1)
```
