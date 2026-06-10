# Codex — Book Translator & Audiobook Engine

A self-hosted web app that translates whole books (EPUB / PDF / TXT / DOCX) with a
local LLM served by llama.cpp, then optionally renders the translated EPUB into a
per-chapter MP3 audiobook with Microsoft Edge neural TTS.

Built for long, terminology-heavy fiction (e.g. Warhammer 40K omnibuses,
English → Chinese), where name consistency and zero untranslated leftovers matter
more than raw speed.

## How translation works

Each chapter goes through three passes against an OpenAI-compatible endpoint
(thinking disabled on every pass):

1. **Scout** — reads the chapter, finds proper nouns not yet in the glossary, and
   registers them via an `update_memory` tool call. New terms are merged into a
   persistent per-book translation memory (case-insensitive, plural-aware).
2. **Translate** — translates the chapter with the glossary in the system prompt.
   Output must match the source paragraph count (±1) or it retries.
3. **Fix** — scans the output for leftover source-language words. Each flagged
   line is re-sent to the LLM (up to 3 tries, but it stops early as soon as a try
   makes no progress); whatever still survives is patched word-by-word through
   Google Translate (`deep-translator`), with caching and batching to stay under
   the free endpoint's rate limits.

Other behaviors worth knowing:

- **Glossary filtering** — only terms that actually appear in the current chunk
  (word-boundary match, tolerant of plurals/possessives) are shipped in the
  prompt, so a 700-term memory costs ~40 lines per request instead of 700.
- **Keep-words** — memory entries whose translation equals the original
  (e.g. `WAAAGH!`) are deliberately left in English by the fix pass.
- **Resume** — finished chapters are saved as `workspace/<book>/translation_chapters/NNN.txt`
  and skipped on re-run; resumed chapters are re-checked by the fix pass, so a
  restart mid-correction can't leak untranslated lines into the final book.
- **Output** — translated EPUBs are rebuilt from the original zip (same structure,
  same images), so they round-trip cleanly into the audiobook side. Non-EPUB
  inputs can be bound as EPUB / HTML / TXT / PDF (pandoc + WeasyPrint).

## How the audiobook works

- The translated EPUB is split exactly like the translator splits it: one content
  document = one chapter, in spine order. Chapter titles come from the EPUB's own
  table of contents (no language-specific heuristics); documents without a TOC
  entry keep their filename stem as a label but don't have it read aloud.
- Each chapter is sent to `edge-tts` as a single request (it chunks internally)
  and comes back as one MP3 in `workspace/<book>/tts/chapters/`.
- Generation resumes per chapter; changing the voice or the EPUB starts fresh.
- The web player lists chapters as they finish, supports auto-advance, voice
  previews, and remembers the last played chapter & position per book
  (localStorage, keyed by the EPUB filename).

## Running

Requirements: Python 3.12, a running [llama.cpp](https://github.com/ggml-org/llama.cpp)
server with an OpenAI-compatible API (default `http://127.0.0.1:8080/v1`,
model name `qwen3`), and network access for Google fallback + edge-tts.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn codex.main:app --port 8000
```

Open `http://localhost:8000`, drop a book on the left page, tune the endpoint /
model / chunking under **Apparatus**, optionally upload a base glossary
(`{"Term": {"translation": "...", "type": "...", "source": "official"}}`), and
start. The right page takes any EPUB (typically the translated output) and turns
it into an audiobook.

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
  worker.py             per-book translation job (scout → translate → fix)
  config.py             defaults (model, retries, workspace path)
  pipeline/
    extract.py          file → pages/chapters (EPUB structure analysis)
    translate.py        LLM passes, glossary filtering, Google fallback
    tts.py              EPUB → per-chapter MP3 via edge-tts
    output.py           bind results as EPUB / HTML / TXT / PDF
static/                 web UI (vanilla JS, no build step)
workspace/<book>/       translation_memory.json, translation_chapters/, tts/, jobs/
streaming.log           raw LLM streams for the chapter in flight (debugging)
```
