import json
import re
import threading
import time
from functools import lru_cache
from pathlib import Path

from deep_translator import GoogleTranslator
from openai import OpenAI

from codex.config import MODEL, MAX_RETRIES, RETRY_DELAY, LANGUAGE_NAMES

# Map app lang codes → Google Translate codes
_GOOGLE_LANG = {"zh": "zh-CN", "zh-tw": "zh-TW"}
# Free endpoint allows ~100 requests/hour — cache results and batch words per request
_google_cache: dict[str, str] = {}

STREAMING_LOG = Path("streaming.log")

# Thinking is permanently off for every pass (llama.cpp/Qwen chat-template switch)
_NO_THINK = {"chat_template_kwargs": {"enable_thinking": False}}

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)
_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")
_ENG_WORD_RE = re.compile(r"\b[a-zA-Z]{4,}\b", re.ASCII)

TOOL_UPDATE_MEMORY = {
    "type": "function",
    "function": {
        "name": "update_memory",
        "description": "Record newly discovered proper nouns and terms encountered during translation",
        "parameters": {
            "type": "object",
            "properties": {
                "terms": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "original": {"type": "string"},
                            "translation": {"type": "string"},
                            "type": {"type": "string"},
                        },
                        "required": ["original", "translation", "type"],
                    },
                }
            },
            "required": ["terms"],
        },
    },
}


@lru_cache(maxsize=4096)
def _term_pattern(key: str) -> re.Pattern:
    """Word-boundary pattern tolerant of simple English morphology (plural/possessive)."""
    stems = [key]
    if len(key) > 3 and key[-1] in "sS" and key[-2].isalpha() and key[-2] not in "sS":
        stems.append(key[:-1])  # plural-looking key also matches its singular form
    pat = "(?:" + "|".join(re.escape(s) for s in sorted(stems, key=len, reverse=True)) + ")"
    if key[0].isalnum():
        pat = r"\b" + pat
    if key[-1].isalnum():
        pat += r"(?:'s|s'|es|s)?\b"
    return re.compile(pat, re.IGNORECASE | re.ASCII)


def filter_glossary(glossary: dict, text: str) -> dict:
    """Subset of glossary whose terms actually appear in text."""
    return {k: v for k, v in glossary.items() if k and _term_pattern(k).search(text)}


def build_system_prompt(src_lang: str, tgt_lang: str, glossary: dict | None = None) -> str:
    src = LANGUAGE_NAMES.get(src_lang, src_lang)
    tgt = LANGUAGE_NAMES.get(tgt_lang, tgt_lang)
    prompt = f"你是专业{src}→{tgt}文学翻译家。"
    if glossary:
        lines = [f"{orig} → {entry['translation']}" for orig, entry in sorted(glossary.items())]
        prompt += "\n\n【术语表】\n" + "\n".join(lines)
    return prompt


def is_valid_translation(text: str, tgt_lang: str) -> bool:
    if not text or not text.strip():
        return False
    if tgt_lang in ("zh", "zh-tw"):
        cjk = sum(1 for c in text if "一" <= c <= "鿿" or "㐀" <= c <= "䶿")
        return cjk / len(text) >= 0.15
    return len(text.strip()) > 20


def _extract_translation(raw: str) -> str:
    text = _THINK_RE.sub("", raw).strip()
    # Strip repeated-paragraph artifact (model echoing its own output)
    paragraphs = [p for p in text.split("\n\n") if len(p.strip()) > 20]
    if len(paragraphs) >= 2:
        probe = paragraphs[0].strip()[:60]
        for i in range(1, len(paragraphs)):
            if probe in paragraphs[i]:
                text = "\n\n".join(paragraphs[:i]).rstrip()
                break
    return text.strip()


def _has_untranslated(line: str, keep: frozenset) -> bool:
    words = _ENG_WORD_RE.findall(line)
    return bool(words) and any(w.lower() not in keep for w in words)


def _run_scout(
    client: OpenAI,
    scout_kwargs: dict,
    system: str,
    user_msg: str,
    log_label: str,
    on_new_terms,
    cancel: threading.Event | None,
) -> None:
    """Single-turn scout: stream tool calls, process update_memory, log everything."""
    if cancel and cancel.is_set():
        raise InterruptedError("cancelled")

    messages: list = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    stream = client.chat.completions.create(**scout_kwargs, messages=messages)
    tc_by_idx: dict[int, dict] = {}

    with STREAMING_LOG.open("a", encoding="utf-8") as log_f:
        log_f.write(f"\n{log_label}\n--- system ---\n{system}\n--- user ---\n{user_msg}\n--- stream ---\n")
        for s_chunk in stream:
            if cancel and cancel.is_set():
                stream.close()
                raise InterruptedError("cancelled")
            if not s_chunk.choices:
                continue
            d = s_chunk.choices[0].delta
            token = getattr(d, "reasoning_content", None) or d.content
            if token:
                log_f.write(token)
                log_f.flush()
            if hasattr(d, "tool_calls") and d.tool_calls:
                for tc in d.tool_calls:
                    idx = getattr(tc, "index", 0) or 0
                    if idx not in tc_by_idx:
                        tc_by_idx[idx] = {"name": None, "args": [], "id": None}
                    if hasattr(tc, "function") and tc.function:
                        if tc.function.name:
                            tc_by_idx[idx]["name"] = tc.function.name
                        if tc.function.arguments:
                            tc_by_idx[idx]["args"].append(tc.function.arguments)
                    if tc.id:
                        tc_by_idx[idx]["id"] = tc.id

    for info in tc_by_idx.values():
        if info["name"] != "update_memory":
            continue
        try:
            args = json.loads("".join(info["args"]))
        except Exception:
            args = {}
        raw_terms = args.get("terms", [])
        terms = [t for t in raw_terms if isinstance(t, dict)] if isinstance(raw_terms, list) else []
        if terms and on_new_terms:
            on_new_terms(terms)
        with STREAMING_LOG.open("a", encoding="utf-8") as log_f:
            log_f.write(f"\n[update_memory: {len(terms)} terms]\n")
            log_f.write(json.dumps(terms, ensure_ascii=False, indent=2) + "\n")


def _stream_translate(
    client: OpenAI,
    kwargs: dict,
    system: str,
    user_msg: str,
    log_label: str,
    cancel: threading.Event | None,
) -> str:
    """Single translate turn: stream, log, return raw collected text."""
    messages: list = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    stream = client.chat.completions.create(**kwargs, messages=messages)
    chunks: list[str] = []

    with STREAMING_LOG.open("a", encoding="utf-8") as log_f:
        log_f.write(f"\n{log_label}\n--- system ---\n{system}\n--- user ---\n{user_msg}\n--- stream ---\n")
        for s_chunk in stream:
            if cancel and cancel.is_set():
                stream.close()
                raise InterruptedError("cancelled")
            if not s_chunk.choices:
                continue
            d = s_chunk.choices[0].delta
            token = getattr(d, "reasoning_content", None) or d.content
            if token:
                log_f.write(token)
                log_f.flush()
            if d.content:
                chunks.append(d.content)

    return "".join(chunks)


def correct_translation(
    client: OpenAI,
    model: str,
    text: str,
    tgt_lang: str,
    src_lang: str,
    keep: frozenset,
    glossary: dict | None = None,
    cancel: threading.Event | None = None,
) -> str:
    lang = LANGUAGE_NAMES.get(tgt_lang, tgt_lang)
    lines = text.splitlines()
    changed = False

    base_kwargs: dict = dict(model=model, temperature=0.2, stream=True, extra_body=_NO_THINK)

    for i, line in enumerate(lines):
        if cancel and cancel.is_set():
            break
        if not _has_untranslated(line, keep):
            continue

        system = build_system_prompt(src_lang, tgt_lang, filter_glossary(glossary, line) if glossary else None)
        src_name = LANGUAGE_NAMES.get(src_lang, src_lang)
        current_line = line  # best partial result so far
        fully_fixed = False
        prev_left = {w.lower() for w in _ENG_WORD_RE.findall(line) if w.lower() not in keep}

        for _ in range(3):
            if cancel and cancel.is_set():
                break
            fix_translate_msg = (
                f"将以下译文行中残留的{src_name}词汇翻译为{lang}，保持语义连贯。\n"
                f"专有名词优先参照【术语表】，普通词汇用自然{lang}表达。\n"
                f"仅输出修正后的单行。\n\n{current_line}"
            )
            try:
                raw = _stream_translate(client, base_kwargs, system, fix_translate_msg, "=== FIX TRANSLATE ===", cancel)
                fixed = _extract_translation(raw)
                if fixed and _CJK_RE.search(fixed):
                    current_line = fixed  # keep partial progress even if not fully clean
                    left = {w.lower() for w in _ENG_WORD_RE.findall(fixed) if w.lower() not in keep}
                    if not left:
                        lines[i] = fixed
                        changed = True
                        fully_fixed = True
                        break
                    if left >= prev_left:
                        break  # no progress over last attempt — go straight to Google
                    prev_left = left
                    continue
            except InterruptedError:
                raise
            except Exception:
                pass
            time.sleep(RETRY_DELAY)

        if not fully_fixed and not (cancel and cancel.is_set()):
            # LLM couldn't fully fix it — Google fallback on best partial result
            result_line = current_line
            words = [w for w in _ENG_WORD_RE.findall(result_line) if w.lower() not in keep]
            if words:
                gl_src = _GOOGLE_LANG.get(src_lang, src_lang)
                gl_tgt = _GOOGLE_LANG.get(tgt_lang, tgt_lang)
                errors: list[str] = []
                unique = list(dict.fromkeys(w.lower() for w in words))
                missing = [w for w in unique if f"{gl_src}:{gl_tgt}:{w}" not in _google_cache]
                if missing:
                    # One request for all uncached words; newlines survive round-trip
                    for _ in range(3):
                        try:
                            raw = GoogleTranslator(source=gl_src, target=gl_tgt).translate("\n".join(missing))
                            parts = [p.strip() for p in (raw or "").split("\n")]
                            if len(parts) == len(missing):
                                for w, p in zip(missing, parts):
                                    _google_cache[f"{gl_src}:{gl_tgt}:{w}"] = p
                                break
                            errors.append(f"batch split mismatch: {raw!r}")
                        except Exception as exc:
                            errors.append(str(exc))
                        time.sleep(RETRY_DELAY)
                for word in unique:
                    translation = _google_cache.get(f"{gl_src}:{gl_tgt}:{word}")
                    if translation and translation.lower() != word:
                        result_line = re.sub(rf"\b{re.escape(word)}\b", translation, result_line,
                                             flags=re.IGNORECASE | re.ASCII)
                leftover = [w for w in _ENG_WORD_RE.findall(result_line) if w.lower() not in keep]
                with STREAMING_LOG.open("a", encoding="utf-8") as log_f:
                    if leftover:
                        log_f.write(f"\n[fallback translate FAILED for {leftover}; errors: {errors}]\n")
                    else:
                        log_f.write(f"\n[fallback translate: {words} → used Google]\n")
            # Keep whatever improvement we got (LLM partial fix and/or Google)
            if result_line != line and _CJK_RE.search(result_line):
                lines[i] = result_line
                changed = True

    return "\n".join(lines) if changed else text


def translate_chunk(
    client: OpenAI,
    system_prompt: str,
    chunk: dict,
    tgt_lang: str,
    src_lang: str = "en",
    model: str = MODEL,
    temperature: float = 0.3,
    cancel: threading.Event | None = None,
    on_new_terms=None,
    on_para_count=None,
    glossary: dict | None = None,
) -> str:
    chapter_text = chunk["text"]

    # Ship only glossary terms that occur in this chunk — full glossary dilutes the prompt
    if glossary is not None:
        effective_system = build_system_prompt(src_lang, tgt_lang, filter_glossary(glossary, chapter_text))
    else:
        effective_system = system_prompt
    if chunk.get("context"):
        effective_system += f"\n\n【上文结尾，仅供衔接参考，不翻译】\n{chunk['context']}"
    tgt_name = LANGUAGE_NAMES.get(tgt_lang, tgt_lang)

    src_name = LANGUAGE_NAMES.get(src_lang, src_lang)

    scout_msg = (
        f"阅读以下章节，找出所有不在【术语表】中的专有名词（人名、地名、派系名、虚构物品等）。\n"
        f"为每个新词提供{tgt_name}译名，然后调用 update_memory()。无新词时传 terms: []。\n\n"
        f"完成后仅输出【准备完成】，不输出其他内容。\n\n"
        f"{chapter_text}"
    )
    translate_msg = (
        f"将以下{src_name}文本翻译为{tgt_name}。\n\n"
        f"规则：\n"
        f"1. 保持原文叙事风格、节奏与语气。\n"
        f"2. 严格使用【术语表】译名，译名后不加原文括注。\n"
        f"3. 逐段对应翻译，段落数量必须一致。\n"
        f"4. 对话自然，符合{tgt_name}表达习惯。\n"
        f"5. 严禁添加译注、解释、括号说明或任何原文没有的内容。\n\n"
        f"直接输出译文，不输出任何非译文内容。"
    )

    base_kwargs: dict = dict(model=model, temperature=temperature, stream=True, extra_body=_NO_THINK)

    scout_kwargs: dict = dict(model=model, temperature=temperature, stream=True,
                              tools=[TOOL_UPDATE_MEMORY], tool_choice="auto",
                              extra_body=_NO_THINK)

    STREAMING_LOG.write_text("", encoding="utf-8")  # truncate for new chapter

    # Scout runs once to discover new terms
    for attempt in range(MAX_RETRIES):
        try:
            _run_scout(client, scout_kwargs, effective_system, scout_msg, "=== SCOUT TURN ===", on_new_terms, cancel)
            break
        except InterruptedError:
            raise
        except Exception:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(RETRY_DELAY)

    # Rebuild system with glossary updated by scout
    if glossary is not None:
        translate_system = build_system_prompt(src_lang, tgt_lang, filter_glossary(glossary, chapter_text))
        if chunk.get("context"):
            translate_system += f"\n\n【上文结尾，仅供衔接参考，不翻译】\n{chunk['context']}"
    else:
        translate_system = effective_system

    src_paras = len([p for p in chapter_text.split("\n\n") if p.strip()])

    result = ""
    attempt = 0
    while True:
        if cancel and cancel.is_set():
            raise InterruptedError("cancelled")
        try:
            raw = _stream_translate(
                client, base_kwargs, translate_system,
                f"{translate_msg}\n\n{chapter_text}",
                "=== TRANSLATE TURN ===", cancel,
            )
            result = _extract_translation(raw)
            tgt_paras = len([p for p in result.split("\n\n") if p.strip()])
            if abs(tgt_paras - src_paras) <= 1:
                break
            with STREAMING_LOG.open("a", encoding="utf-8") as log_f:
                log_f.write(f"\n[para mismatch attempt {attempt + 1}: src={src_paras} tgt={tgt_paras}, retrying]\n")
        except InterruptedError:
            raise
        except Exception:
            pass
        attempt += 1
        time.sleep(RETRY_DELAY)

    tgt_paras = len([p for p in result.split("\n\n") if p.strip()])
    if on_para_count:
        on_para_count(src_paras, tgt_paras)
    return result
