import re
import threading
import time
from pathlib import Path

from openai import OpenAI

from codex.config import MODEL, MAX_RETRIES, RETRY_DELAY, LANGUAGE_NAMES
from codex.pipeline.utils import is_looping

STREAMING_LOG = Path("streaming.log")

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)


_CJK_RE = re.compile(r"[一-鿿㐀-䶿]")
_ENG_LOW_RE = re.compile(r"\b[a-z][a-zA-Z]{3,}\b")


def build_system_prompt(src_lang: str, tgt_lang: str, glossary: dict | None = None) -> str:
    src = LANGUAGE_NAMES.get(src_lang, src_lang)
    tgt = LANGUAGE_NAMES.get(tgt_lang, tgt_lang)

    prompt = f"""你是一位专业文学翻译家。将以下{src}内容翻译成流畅、文学化的{tgt}。

翻译规则：
1. 保持原文的叙事风格、节奏和语气
2. 严格使用【术语表】中的译名，标注"保留原文"的词汇直接原样保留
3. 保留段落结构，每段对应翻译
4. 对话须自然，符合目标语言阅读习惯
5. 只输出译文，不加注释、解释或原文

【严禁以下行为】
- 输出原文或原文与译文混排（术语表中标注"保留原文"的专有名词除外）
- 添加"以下是翻译："等引导语
- 跳过任何段落不翻译
- 改变段落数量"""

    if glossary:
        lines = []
        for original, entry in sorted(glossary.items()):
            t = entry["translation"]
            lines.append(f"{original} → {t}（保留原文）" if t == original else f"{original} → {t}")
        prompt += "\n\n【术语表】\n" + "\n".join(lines)

    return prompt


def is_valid_translation(text: str, tgt_lang: str) -> bool:
    if not text or not text.strip():
        return False
    if tgt_lang in ("zh", "zh-tw"):
        cjk = sum(1 for c in text if "一" <= c <= "鿿" or "㐀" <= c <= "䶿")
        return cjk / len(text) >= 0.15
    return len(text.strip()) > 20


def _sanitize_result(text: str) -> str:
    paragraphs = [p for p in text.split("\n\n") if len(p.strip()) > 20]
    if len(paragraphs) >= 2:
        probe = paragraphs[0].strip()[:60]
        for i in range(1, len(paragraphs)):
            if probe in paragraphs[i]:
                text = "\n\n".join(paragraphs[:i]).rstrip()
                break
    return text.strip()


def _has_untranslated(line: str, keep: frozenset) -> bool:
    if not _CJK_RE.search(line):
        return False
    return any(w not in keep for w in _ENG_LOW_RE.findall(line))


def _fix_untranslated_lines(
    client: OpenAI,
    model: str,
    text: str,
    tgt_lang: str,
    keep: frozenset,
    cancel: threading.Event | None = None,
) -> str:
    lang = LANGUAGE_NAMES.get(tgt_lang, tgt_lang)
    lines = text.splitlines()
    changed = False
    for i, line in enumerate(lines):
        if cancel and cancel.is_set():
            break
        if not _has_untranslated(line, keep):
            continue
        prompt = (
            f"以下这行中文译文中夹杂了未翻译的英文词汇。"
            f"请将其中的普通英文词汇翻译为{lang}。"
            f"保持所有中文内容原封不动；保留专有名词、人名及虚构发明词汇的英文原形。"
            f"只输出修正后的单行译文，不加任何说明。\n\n{line}"
        )
        for attempt in range(MAX_RETRIES):
            try:
                stream = client.chat.completions.create(
                    model=model,
                    messages=[{"role": "user", "content": prompt}],
                    temperature=0.2,
                    stream=True,
                )
                parts: list[str] = []
                recent: list[str] = []
                token_count = 0
                with STREAMING_LOG.open("a", encoding="utf-8") as log_f:
                    for s_chunk in stream:
                        if cancel and cancel.is_set():
                            stream.close()
                            break
                        if not s_chunk.choices:
                            continue
                        d = s_chunk.choices[0].delta
                        thinking = getattr(d, "reasoning_content", None)
                        content = d.content
                        token = thinking or content
                        if token:
                            log_f.write(token)
                            log_f.flush()
                            recent.append(token)
                            token_count += 1
                            if token_count % 50 == 0:
                                if is_looping("".join(recent)):
                                    stream.close()
                                    raise RuntimeError("loop detected in fix pass")
                                recent = []
                        if content:
                            parts.append(content)
                fixed = "".join(parts).strip()
                if fixed and _CJK_RE.search(fixed) and not _has_untranslated(fixed, keep):
                    lines[i] = fixed
                    changed = True
                break
            except RuntimeError:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)
            except Exception:
                break
    return "\n".join(lines) if changed else text


def translate_chunk(
    client: OpenAI,
    system_prompt: str,
    chunk: dict,
    tgt_lang: str,
    model: str = MODEL,
    temperature: float = 0.3,
    keep: frozenset = frozenset(),
    cancel: threading.Event | None = None,
) -> str:
    effective_system = system_prompt
    if chunk.get("context"):
        effective_system += f"\n\n【上文结尾，仅供衔接参考，不翻译】\n{chunk['context']}"

    user_msg = f"请翻译以下内容：\n\n{chunk['text']}"
    result = ""

    kwargs: dict = dict(
        model=model,
        messages=[
            {"role": "system", "content": effective_system},
            {"role": "user", "content": user_msg},
        ],
        temperature=temperature,
        stream=True,
    )

    for attempt in range(MAX_RETRIES):
        if cancel and cancel.is_set():
            raise InterruptedError("cancelled")
        try:
            stream = client.chat.completions.create(**kwargs)
            chunks: list[str] = []
            recent: list[str] = []
            token_count = 0
            with STREAMING_LOG.open("w", encoding="utf-8") as log_f:
                for s_chunk in stream:
                    if cancel and cancel.is_set():
                        stream.close()
                        raise InterruptedError("cancelled")
                    if not s_chunk.choices:
                        continue
                    d = s_chunk.choices[0].delta
                    thinking = getattr(d, "reasoning_content", None)
                    content = d.content
                    token = thinking or content
                    if token:
                        log_f.write(token)
                        log_f.flush()
                        recent.append(token)
                        token_count += 1
                        if token_count % 50 == 0:
                            if is_looping("".join(recent)):
                                stream.close()
                                raise RuntimeError("loop detected")
                            recent = []
                    if content:
                        chunks.append(content)

            raw = "".join(chunks)
            result = _THINK_RE.sub("", raw).strip()
            result = _sanitize_result(result)

            if is_valid_translation(result, tgt_lang):
                result = _fix_untranslated_lines(client, model, result, tgt_lang, keep, cancel)
                return result

            user_msg = f"【重要：只输出译文，不输出原文】\n\n{chunk['text']}"
        except InterruptedError:
            raise
        except Exception:
            if attempt == MAX_RETRIES - 1:
                raise
            time.sleep(RETRY_DELAY)

    return result
