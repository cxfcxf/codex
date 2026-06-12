# Codex — Book Translator & Audiobook Engine

A self-hosted web app that translates whole books (EPUB in, EPUB out) with a
local LLM served by llama.cpp, then optionally renders the translated EPUB into a
per-chapter MP3 audiobook with Microsoft Edge neural TTS.

Built for long, terminology-heavy fiction (e.g. Warhammer 40K omnibuses,
English → Chinese), where name consistency and structural fidelity matter more
than raw speed. Input is EPUB only — convert anything else upstream, where you
can see the file and make book-specific decisions.

## How translation works

Each chapter goes through up to three passes against an OpenAI-compatible
endpoint (thinking disabled on every pass):

1. **Scout** — reads the chapter, finds proper nouns not yet in the glossary, and
   registers them via an `update_memory` tool call. New terms are merged into a
   persistent per-book translation memory (case-insensitive, plural-aware).
   The call runs with `tool_choice: "required"` so llama.cpp grammar-enforces the
   args schema at decode time; output is still strictly validated, malformed
   output is fed back to the model for a retry, and a persistently failing scout
   degrades to "no term discovery" instead of failing the chapter.
2. **Translate** — translates the chapter with the glossary in the system prompt.
   The prompt states the exact source paragraph count; the output must come back
   within the **¶ Tolerance** setting (default 2% of the count, floor ±1).
   A miss feeds the actual wrong count back to the model. If a block keeps
   failing, it is **recursively bisected**: whole chapter → halves → quarters …
   down to a 50-paragraph floor. Blocks too small to split get 5 corrective
   retries, then the closest attempt is kept (clearly flagged in the console).
   Every retry, split, and kept-best is logged live to the job console.
3. **Fix** (optional, default off — toggle in Apparatus) — scans the output for
   leftover source-language words. Each flagged line is re-sent to the LLM
   (up to 3 tries); whatever survives is patched word-by-word through Google
   Translate (`deep-translator`), cached and batched to stay under the free
   endpoint's rate limits. Turn it on for models that leave English behind;
   leave it off for models that translate cleanly in one go.

Other behaviors worth knowing:

- **Glossary filtering** — only terms that actually appear in the current block
  (word-boundary match, tolerant of plurals/possessives) are shipped in the
  prompt, so a 1600-term memory costs ~40 lines per request instead of 1600.
- **Keep-words** — memory entries whose translation equals the original
  (e.g. `WAAAGH!`) are deliberately left in English by the fix pass.
- **Resume** — finished chapters are saved as `workspace/<book>/translation_chapters/NNN.txt`
  and skipped on re-run; bisected blocks append to a `.txt.part` sidecar as they
  finish and the chapter file is only written whole, so a restart can't bind a
  half-translated chapter. A crashed llama.cpp just stalls the job — it retries
  indefinitely and continues when the server comes back.
- **Output** — the translated EPUB is rebuilt from the original zip (same
  structure, same images), so it round-trips cleanly into the audiobook side.

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
server with an OpenAI-compatible API (default `http://127.0.0.1:8080/v1`; the
model name field is cosmetic for llama.cpp), and network access for the optional
Google fallback + edge-tts.

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn codex.main:app --port 8000
```

Open `http://localhost:8000`, drop an EPUB on the left page, tune the endpoint /
model / ¶ tolerance / correction pass under **Apparatus**, optionally upload a
base glossary (`{"Term": {"translation": "...", "type": "...", "source": "official"}}`),
and start. The right page takes any EPUB (typically the translated output) and
turns it into an audiobook.

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
  worker.py             per-book translation job (scout → translate → bisect → fix)
  config.py             defaults (model, retries, workspace path)
  pipeline/
    extract.py          EPUB → chapters (structure analysis)
    translate.py        LLM passes, glossary filtering, shape retries, Google fallback
    tts.py              EPUB → per-chapter MP3 via edge-tts
    output.py           rebuild the translated EPUB from the original zip
scripts/
  compare_translations.py   metrics + blind LLM judge for two translations
static/                 web UI (vanilla JS, no build step)
workspace/<book>/       translation_memory.json, translation_chapters/, tts/, jobs/
streaming.log           raw LLM streams for the block in flight (debugging)
```
