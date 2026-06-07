# Codex — Book Translator

A web app that translates books (PDF, EPUB, TXT, DOCX) using any OpenAI-compatible API (DeepSeek, local Ollama/LM Studio, etc.), with optional audiobook generation via edge-tts.

---

## Quick Start

### Docker (recommended)

```bash
docker build -t codex-translator .
docker run -d --name codex -p 7860:8000 \
  -v $(pwd)/jobs:/app/jobs \
  -v $(pwd)/translation_chapters:/app/translation_chapters \
  codex-translator
```

Open http://localhost:7860

Mount the volumes if you want translated files and chapter caches to persist across container restarts.

### Local (dev)

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
.venv/bin/python -m uvicorn codex.main:app --host 0.0.0.0 --port 7860
```

---

## Features

- **Formats**: PDF, EPUB, TXT, DOCX → EPUB, HTML, PDF, TXT
- **EPUB-preserving translation**: For EPUB input, the original CSS, cover, navigation, and structure are kept intact. Only the paragraph text is replaced.
- **Chapter-level resumability**: EPUB translations are cached per chapter in `translation_chapters/`. If the job is interrupted, resubmit the same file and already-translated chapters are skipped.
- **Glossary pipeline**: For non-EPUB formats, proper nouns are extracted, conflicts resolved, then used as a translation glossary.
- **Glossary upload**: Upload a `glossary.json` in Advanced Settings to skip the extraction phase and use your own terminology.
- **Auto settings**: After file upload, the app analyzes the file and suggests optimal workers, chunk sizes, and overlap.
- **Parallel workers**: Configurable concurrency for translation requests.
- **Any OpenAI-compatible API**: Set Base URL, Model, and API Key in Advanced Settings.

---

## Advanced Settings

| Setting | Default | Description |
|---|---|---|
| Base URL | `https://api.deepseek.com` | OpenAI-compatible API endpoint |
| Model | `deepseek-chat` | Model name |
| API Key | *(blank)* | Leave blank for local models |
| Parallel Workers | auto | Concurrent translation requests |
| Translation Chunk | auto | Words per translation request |
| Extract Chunk | auto | Words per glossary extraction request |
| Overlap Context | auto | Words of context carried between chunks |
| Temperature | 0.3 | Sampling temperature |
| Max Tokens | 393216 | Max output tokens per request |
| Glossary | *(none)* | Upload `glossary.json` to skip extraction |

---

## Audiobook Generation

Convert a translated EPUB to MP3 chapters using Microsoft Neural TTS (edge-tts, free, no API key needed).

```bash
pip install edge-tts
python audiobook_edge_tts.py \
  --epub /path/to/translated.epub \
  --voice zh-CN-YunjianNeural \
  --delay 0.5
```

- Resumes automatically if interrupted — run again to continue
- Outputs one MP3 per chapter to `tts_chunks_edge/chapters/`
- `--delay 0.5` adds a 0.5s pause between requests to avoid rate limiting
- `--stitch-only` re-merges existing chunks without re-generating

### Available Chinese voices

| Voice | Character |
|---|---|
| `zh-CN-YunjianNeural` | Male, deep, narrator-style (recommended) |
| `zh-CN-YunxiNeural` | Male, younger |
| `zh-CN-YunyangNeural` | Male, news-reader style |
| `zh-CN-XiaoxiaoNeural` | Female, warm |

---

## PDF to EPUB Conversion

For clean (non-OCR) PDF novels, `pdf_to_epub.py` converts to structured EPUB with proper chapter detection.

```bash
pip install pdfplumber
python pdf_to_epub.py
```

Edit `PDF_PATH` and `OUT_EPUB` at the top of the file. The script uses word-level bounding box extraction to detect paragraph boundaries and chapter headings.

---

## Project Structure

```
codex/               # Main application package
  main.py            # FastAPI routes
  worker.py          # Background translation worker
  config.py          # Constants and defaults
  pipeline/
    extract.py       # Text extraction (PDF, EPUB, TXT, DOCX)
    translate.py     # Translation via OpenAI-compatible API
    glossary.py      # Term extraction and conflict resolution
    output.py        # Output generation (EPUB, HTML, PDF, TXT)

static/              # Frontend
  index.html
  css/style.css
  js/app.js

jobs/                # Per-job working directories (ephemeral in Docker)
translation_chapters/ # Per-chapter translation cache for EPUB resumability

audiobook_edge_tts.py  # Audiobook generator (EPUB → MP3)
pdf_to_epub.py         # PDF → EPUB converter
Dockerfile
requirements.txt
```

---

## Persistence in Docker

| Directory | Contents | Mount to persist |
|---|---|---|
| `jobs/` | Input files, output EPUBs | `-v $(pwd)/jobs:/app/jobs` |
| `translation_chapters/` | Per-chapter translation cache | `-v $(pwd)/translation_chapters:/app/translation_chapters` |

Without mounts, all translated files are lost when the container stops.
