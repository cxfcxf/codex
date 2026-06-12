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

# After this many paragraph-count mismatches, keep the closest attempt and move on
MAX_MISMATCH_TRIES = 5

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


class ScoutFormatError(ValueError):
    """update_memory tool args didn't match the declared schema."""


def _run_scout(
    client: OpenAI,
    scout_kwargs: dict,
    system: str,
    user_msg: str,
    log_label: str,
    on_new_terms,
    cancel: threading.Event | None,
    strict: bool = False,
) -> None:
    """Single-turn scout: stream tool calls, process update_memory, log everything.

    With strict=True, malformed tool args raise ScoutFormatError so the caller
    can retry with corrective feedback instead of guessing what the model meant.
    """
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
                            log_f.write(f"[tool_call: {tc.function.name}] ")
                        if tc.function.arguments:
                            tc_by_idx[idx]["args"].append(tc.function.arguments)
                            # Tool-arg deltas are the scout's real output — log them,
                            # or a runaway tool call looks like a hung stream
                            log_f.write(tc.function.arguments)
                            log_f.flush()
                    if tc.id:
                        tc_by_idx[idx]["id"] = tc.id

    all_terms: list = []
    for info in tc_by_idx.values():
        if info["name"] != "update_memory":
            continue
        try:
            args = json.loads("".join(info["args"]))
        except Exception:
            if strict:
                raise ScoutFormatError("arguments were not valid JSON")
            args = {}
        raw_terms = args.get("terms", []) if isinstance(args, dict) else []
        if not isinstance(raw_terms, list):
            if strict:
                raise ScoutFormatError("terms is not an array")
            raw_terms = []
        all_terms.extend(raw_terms)

    if strict:
        for t in all_terms:
            if (not isinstance(t, dict)
                    or not isinstance(t.get("original"), str)
                    or not isinstance(t.get("translation"), str)):
                raise ScoutFormatError(f"malformed term entry: {json.dumps(t, ensure_ascii=False)[:120]}")

    terms = [t for t in all_terms if isinstance(t, dict)]
    if terms and on_new_terms:
        on_new_terms(terms)
    if tc_by_idx:
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
) -> tuple[str, str | None]:
    """Single translate turn: stream, log, return (raw text, finish_reason).

    finish_reason "length" means the output cap cut the generation — the
    caller must treat that attempt as failed no matter what the text says."""
    messages: list = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_msg},
    ]
    stream = client.chat.completions.create(**kwargs, messages=messages)
    chunks: list[str] = []
    finish_reason: str | None = None

    with STREAMING_LOG.open("a", encoding="utf-8") as log_f:
        log_f.write(f"\n{log_label}\n--- system ---\n{system}\n--- user ---\n{user_msg}\n--- stream ---\n")
        for s_chunk in stream:
            if cancel and cancel.is_set():
                stream.close()
                raise InterruptedError("cancelled")
            if not s_chunk.choices:
                continue
            if s_chunk.choices[0].finish_reason:
                finish_reason = s_chunk.choices[0].finish_reason
            d = s_chunk.choices[0].delta
            token = getattr(d, "reasoning_content", None) or d.content
            if token:
                log_f.write(token)
                log_f.flush()
            if d.content:
                chunks.append(d.content)

    return "".join(chunks), finish_reason


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
                raw, _ = _stream_translate(client, base_kwargs, system, fix_translate_msg, "=== FIX TRANSLATE ===", cancel)
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


def scout_chapter(
    client: OpenAI,
    model: str,
    text: str,
    tgt_lang: str,
    src_lang: str = "en",
    temperature: float = 0.3,
    glossary: dict | None = None,
    on_new_terms=None,
    cancel: threading.Event | None = None,
) -> None:
    """Scout phase: discover proper nouns in one chapter and feed them to
    on_new_terms via the update_memory tool call.

    tool_choice stays "auto": forcing the call via grammar ("required") masks EOS
    until the JSON closes, and a looping model then fills the entire context with
    legal array items (seen: 253k tokens / 39 min). With auto the model ends
    naturally; malformed args are caught by strict validation + reprompt below,
    and only the last attempt accepts them leniently. A persistently failing
    scout is logged and skipped — it must never block the pipeline.
    """
    tgt_name = LANGUAGE_NAMES.get(tgt_lang, tgt_lang)
    system = build_system_prompt(src_lang, tgt_lang, filter_glossary(glossary, text) if glossary else None)

    scout_instr = (
        f"阅读以下章节，找出所有不在【术语表】中的专有名词（人名、地名、派系名、虚构物品等）。\n"
        f"为每个新词提供{tgt_name}译名，然后调用 update_memory()。无新词时传 terms: []。\n\n"
        f"完成后仅输出【准备完成】，不输出其他内容。"
    )
    # Dynamic cap from the chapter's size: a term entry is ~40 tokens and even
    # term-dense chapters yield well under 1 token of JSON per 2 source words.
    # When the model fumbles the tool-call closing syntax (seen: `"人名}]}` +
    # `<tool_call|>` + endless filler), the end-trigger never matches and it
    # otherwise spews until the server context fills. The cap turns that into a
    # quick failed attempt; truncated args fail JSON validation and the retry
    # below re-runs the scout.
    src_size = max(len(text.split()), len(_CJK_RE.findall(text)))
    scout_cap = max(4096, min(8192, src_size // 2))
    scout_kwargs: dict = dict(model=model, temperature=temperature, stream=True,
                              tools=[TOOL_UPDATE_MEMORY], tool_choice="auto",
                              max_tokens=scout_cap, extra_body=_NO_THINK)

    STREAMING_LOG.write_text("", encoding="utf-8")  # truncate for new chapter

    scout_user = f"{scout_instr}\n\n{text}"
    for attempt in range(MAX_RETRIES):
        try:
            _run_scout(client, scout_kwargs, system, scout_user, "=== SCOUT TURN ===",
                       on_new_terms, cancel, strict=attempt < MAX_RETRIES - 1)
            return
        except InterruptedError:
            raise
        except ScoutFormatError as exc:
            with STREAMING_LOG.open("a", encoding="utf-8") as log_f:
                log_f.write(f"\n[scout output malformed (attempt {attempt + 1}): {exc}, retrying]\n")
            scout_user = (
                f"{scout_instr}\n\n"
                f"【上次调用不合格】update_memory 的参数格式错误：{exc}。"
                f"terms 数组中每项的 original、translation、type 必须各是一个字符串，"
                f"不得使用数组、嵌套对象或其他类型。重新调用 update_memory()。\n\n"
                f"{text}"
            )
        except Exception as exc:
            if attempt == MAX_RETRIES - 1:
                with STREAMING_LOG.open("a", encoding="utf-8") as log_f:
                    log_f.write(f"\n[scout failed, continuing without term discovery: {exc}]\n")
                return
            time.sleep(RETRY_DELAY)


def translate_chunk(
    client: OpenAI,
    system_prompt: str,
    chunk: dict,
    tgt_lang: str,
    src_lang: str = "en",
    model: str = MODEL,
    temperature: float = 0.3,
    cancel: threading.Event | None = None,
    on_para_count=None,
    on_mismatch=None,
    glossary: dict | None = None,
    tolerance_percent: int = 2,
    mismatch_tries: int = MAX_MISMATCH_TRIES,
) -> tuple[str, bool]:
    """Returns (translation, ok). ok=False means the paragraph count never came
    within tolerance and the closest attempt was returned instead."""
    chapter_text = chunk["text"]

    # Ship only glossary terms that occur in this chunk — full glossary dilutes the prompt
    if glossary is not None:
        translate_system = build_system_prompt(src_lang, tgt_lang, filter_glossary(glossary, chapter_text))
    else:
        translate_system = system_prompt
    tgt_name = LANGUAGE_NAMES.get(tgt_lang, tgt_lang)

    src_name = LANGUAGE_NAMES.get(src_lang, src_lang)
    src_paras = len([p for p in chapter_text.split("\n\n") if p.strip()])

    translate_msg = (
        f"将以下{src_name}文本翻译为{tgt_name}。\n\n"
        f"规则：\n"
        f"1. 保持原文叙事风格、节奏与语气。\n"
        f"2. 严格使用【术语表】译名，译名后不加原文括注。\n"
        f"3. 原文以空行分隔，共 {src_paras} 段。逐段对应翻译：每段原文译为恰好一段译文，"
        f"段与段之间用一个空行分隔。禁止拆分长段、合并短段或增删段落，译文必须正好 {src_paras} 段。\n"
        f"4. 对话自然，符合{tgt_name}表达习惯。\n"
        f"5. 严禁添加译注、解释、括号说明或任何原文没有的内容。\n\n"
        f"直接输出译文，不输出任何非译文内容。"
    )

    # Dynamic output budget derived from the source block: honest CJK output
    # runs ~1.6–1.9 tokens per source word, so 3× is generous for any real
    # translation but stops a repetition loop in minutes instead of letting it
    # fill the server's context pool. Truncation is detected explicitly via
    # finish_reason and always counts as a failed attempt — capped text can
    # never pass the shape check by accident or be kept as "best".
    src_size = max(len(chapter_text.split()), len(_CJK_RE.findall(chapter_text)))
    output_cap = max(2048, src_size * 3)
    base_kwargs: dict = dict(model=model, temperature=temperature, stream=True,
                             max_tokens=output_cap, extra_body=_NO_THINK)

    STREAMING_LOG.write_text("", encoding="utf-8")  # truncate for new block

    result = ""
    ok = False
    misses = 0
    best: tuple[int, str] | None = None  # (diff, text) closest to source so far
    tolerance = max(1, round(src_paras * tolerance_percent / 100))
    user_msg = f"{translate_msg}\n\n{chapter_text}"
    while True:
        if cancel and cancel.is_set():
            raise InterruptedError("cancelled")
        try:
            raw, finish = _stream_translate(
                client, base_kwargs, translate_system,
                user_msg,
                "=== TRANSLATE TURN ===", cancel,
            )
            truncated = finish == "length"
            result = _extract_translation(raw)
            tgt_paras = len([p for p in result.split("\n\n") if p.strip()])
            diff = abs(tgt_paras - src_paras)
            if diff <= tolerance and not truncated:
                ok = True
                break
            misses += 1
            if not truncated and (best is None or diff < best[0]):
                best = (diff, result)
            with STREAMING_LOG.open("a", encoding="utf-8") as log_f:
                note = " TRUNCATED at cap" if truncated else ""
                log_f.write(f"\n[para mismatch attempt {misses}/{mismatch_tries}: "
                            f"src={src_paras} tgt={tgt_paras} tol=±{tolerance}{note}]\n")
            if on_mismatch:
                on_mismatch(misses, mismatch_tries, src_paras, tgt_paras)
            if misses >= mismatch_tries:
                if best is not None:
                    result = best[1]
                with STREAMING_LOG.open("a", encoding="utf-8") as log_f:
                    log_f.write(f"\n[shape check failed after {misses} tries: src={src_paras} "
                                f"best diff={best[0] if best else 'none — all attempts truncated'}]\n")
                break
            # Tell the model what went wrong instead of resending the same prompt
            if truncated:
                complaint = (f"【上次译文不合格】输出过长被截断（原文仅 {src_paras} 段）。"
                             f"不要重复内容，严格逐段翻译，确保译文正好 {src_paras} 段。")
            else:
                hint = "不要把一段原文拆成多段译文" if tgt_paras > src_paras else "不要合并或遗漏段落"
                complaint = (f"【上次译文不合格】输出了 {tgt_paras} 段，但原文是 {src_paras} 段。"
                             f"{hint}，重新逐段翻译，确保译文正好 {src_paras} 段。")
            user_msg = f"{translate_msg}\n\n{complaint}\n\n{chapter_text}"
        except InterruptedError:
            raise
        except Exception:
            pass
        time.sleep(RETRY_DELAY)

    tgt_paras = len([p for p in result.split("\n\n") if p.strip()])
    if on_para_count:
        on_para_count(src_paras, tgt_paras)
    return result, ok
