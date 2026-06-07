import json
import re
import threading
import time
from pathlib import Path

from openai import OpenAI

from codex.config import MODEL, MAX_RETRIES, RETRY_DELAY, RESOLVE_BATCH_SIZE, LANGUAGE_NAMES
from codex.pipeline.utils import is_looping

STREAMING_LOG = Path("streaming.log")

# ── Capitalized term extraction ───────────────────────────────────────────────

_CAP_RUN_RE = re.compile(r"\b[A-Z][a-z][a-zA-Z'\-]*(?:\s+[A-Z][a-z][a-zA-Z'\-]*)*\b")
_SENT_END_RE = re.compile(r'[.!?\n]["\' \t]*$')
_SKIP_WORDS = frozenset({
    # articles, pronouns, conjunctions, prepositions
    "The", "A", "An", "He", "She", "It", "They", "We", "You", "I",
    "His", "Her", "Its", "Their", "Our", "Your", "My", "Me", "Him",
    "This", "That", "These", "Those", "But", "And", "Or", "Not",
    "So", "Yet", "For", "Nor", "As", "At", "By", "In", "Of", "On",
    "To", "Up", "No", "If", "Do", "Be", "Was", "Is", "Are", "Had",
    "Has", "Have", "Did", "Does", "Will", "Would", "Could", "Should",
    "May", "Might", "Must", "Shall", "Can", "Then", "Than", "When",
    "Where", "Which", "Who", "What", "How", "Why", "All", "Some",
    "Any", "Each", "Every", "Both", "Few", "More", "Most", "Other",
    "Such", "Same", "Only", "Also", "Still", "Now", "Here", "There",
    "Just", "Even", "Never", "Always", "Again", "Once", "Back",
    # common verbs
    "Said", "Says", "Say", "Looked", "Look", "Looks", "Came", "Come",
    "Comes", "Went", "Go", "Goes", "Got", "Get", "Gets", "Felt", "Feel",
    "Feels", "Knew", "Know", "Knows", "Saw", "See", "Sees", "Heard",
    "Hear", "Took", "Take", "Takes", "Made", "Make", "Makes", "Gave",
    "Give", "Gives", "Kept", "Keep", "Keeps", "Turned", "Turn", "Turns",
    "Moved", "Move", "Moves", "Found", "Find", "Finds", "Told", "Tell",
    "Tells", "Called", "Call", "Calls", "Seemed", "Seem", "Seems",
    "Left", "Stood", "Stand", "Stands", "Brought", "Bring", "Brings",
    "Thought", "Think", "Thinks", "Used", "Use", "Uses", "Pulled",
    "Pushed", "Reached", "Reached", "Stopped", "Stopped", "Started",
    # common adjectives / adverbs / exclamations
    "Good", "Bad", "Old", "New", "Big", "Small", "Long", "Short",
    "High", "Low", "Right", "Wrong", "Sure", "True", "Dead", "Dark",
    "Hard", "Deep", "Real", "Close", "Open", "Clear", "Free", "Fast",
    "Slow", "Late", "Early", "Cold", "Hot", "Alive", "Alone",
    "Damn", "Hell", "Yes", "No", "Ah", "Oh", "Wait", "Stop", "Run",
    "Concentrate", "Actually", "About", "After", "Before", "During",
    "Against", "Between", "Through", "Without", "Within", "Across",
    "Apart", "Away", "Ahead", "Around", "Along", "Among", "Upon",
    "Beneath", "Behind", "Beyond", "Beside", "Below", "Above",
    "Busy", "Coming", "Going", "Done", "Next", "Last", "First",
    "Another", "Certain", "Enough", "Almost", "Already", "Anything",
    "Something", "Nothing", "Everything", "Someone", "Everyone",
    "Anyone", "Somewhere", "Nowhere", "Everywhere", "Somehow",
    "Data", "Company", "Department", "Acts", "Boost", "Boss",
})


def build_extract_prompt(src_lang: str, tgt_lang: str) -> str:
    src = LANGUAGE_NAMES.get(src_lang, src_lang)
    tgt = LANGUAGE_NAMES.get(tgt_lang, tgt_lang)
    return f"""你是一位专业文学翻译专家。从给定{src}文本中提取所有专有名词，并提供标准{tgt}译名。

优先级：
1. 已有公认的官方{tgt}译名（最高优先）
2. 约定俗成的{tgt}译名
3. 若无已知译名，进行音译或意译
4. 若该词应保留原文（如外星语言、特殊术语），translation字段填入原文，source填"keep"

【重要】translation字段只填一个译名，若有多个候选，选最佳的一个。

类型：character（人名）、place（地名）、faction（组织）、item（物品）、concept（概念/头衔）

仅输出JSON，不要任何额外文字或代码块标记：
{{"terms": [{{"original": "原文", "translation": "{tgt}译名", "type": "character|place|faction|item|concept", "source": "official|convention|keep"}}]}}"""


def build_resolve_prompt(src_lang: str, tgt_lang: str) -> str:
    tgt = LANGUAGE_NAMES.get(tgt_lang, tgt_lang)
    return f"""你是一位专业文学翻译专家，对各类文学作品的{tgt}译名有深入了解。

以下专有名词存在翻译分歧，请从每个词条的候选译名中选出最准确的版本。

选择标准（按优先级）：
1. 已有公认的官方{tgt}译名
2. 约定俗成的{tgt}译名
3. 若所有候选均不理想，可提供更好的译名
4. 若该词应保留原文，则chosen填入原文

仅输出JSON，不要任何额外文字：
{{"resolutions": [{{"original": "原文", "chosen": "选定译名"}}]}}"""


def extract_capitalized_terms(full_text: str) -> list[str]:
    """Return unique mid-sentence Title Case word sequences from text."""
    candidates: set[str] = set()
    for m in _CAP_RUN_RE.finditer(full_text):
        term = m.group()
        first_word = term.split()[0]
        if first_word in _SKIP_WORDS:
            continue
        preceding = full_text[max(0, m.start() - 8): m.start()]
        if not preceding.strip() or _SENT_END_RE.search(preceding):
            continue
        candidates.add(term)
    # single words must appear 3+ times to filter incidental capitalization
    terms = {
        t for t in candidates
        if " " in t or full_text.count(t) >= 3
    }
    return sorted(terms)


def build_cap_extract_prompt(src_lang: str, tgt_lang: str) -> str:
    src = LANGUAGE_NAMES.get(src_lang, src_lang)
    tgt = LANGUAGE_NAMES.get(tgt_lang, tgt_lang)
    return f"""你是一位专业文学翻译专家，熟悉各类奇幻与科幻作品的{tgt}译名。
以下是从{src}小说中提取的大写词汇列表，其中可能包含人名、地名、组织名、物品名及专有概念。

请对每个词条：
1. 判断是否为真正的专有名词（普通单词请跳过，不要输出）
2. 若词汇已出现在【已知术语】列表中，直接沿用该译名（source填"official"），不要另立新译
3. 提供准确的{tgt}译名（优先使用官方/约定俗成译名，如无则音译或意译）
4. 仅当词汇是真正无法翻译的虚构外星语或发明词时，translation填原文，source填"keep"
5. translation字段只填一个译名，若有多个候选，选最佳的一个

仅输出JSON，不要任何额外文字或代码块标记：
{{"terms": [{{"original": "原文", "translation": "{tgt}译名", "type": "character|place|faction|item|concept", "source": "official|convention|keep"}}]}}"""


def translate_cap_terms(
    client: OpenAI, prompt: str, terms: list[str],
    model: str = MODEL, temperature: float = 0.3,
    cancel: threading.Event | None = None,
    on_batch_done: callable = None,
    known_glossary: list[dict] | None = None,
) -> list[dict]:
    if not terms:
        return []
    known_ctx = ""
    if known_glossary:
        lines = [f"{t['original']} → {t['translation']}"
                 for t in known_glossary if t.get("original") and t.get("translation")]
        if lines:
            known_ctx = "\n\n【已知术语，请保持译名一致】\n" + "\n".join(lines)
    for attempt in range(MAX_RETRIES):
        if cancel and cancel.is_set():
            raise InterruptedError("cancelled")
        try:
            raw = _stream_content(
                client, cancel=cancel, model=model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": "\n".join(terms) + known_ctx},
                ],
                temperature=temperature,
            )
            raw = raw.strip()
            if raw.startswith("```"):
                raw = "\n".join(raw.split("\n")[1:])
            if raw.endswith("```"):
                raw = "\n".join(raw.split("\n")[:-1])
            result = json.loads(raw).get("terms", [])
            if on_batch_done:
                on_batch_done(1)
            return result
        except InterruptedError:
            raise
        except Exception:
            if attempt < MAX_RETRIES - 1:
                time.sleep(RETRY_DELAY)
    return []



def _stream_content(client: OpenAI, cancel: threading.Event | None = None, **kwargs) -> str:
    stream = client.chat.completions.create(stream=True, **kwargs)
    parts = []
    recent: list[str] = []
    token_count = 0
    with STREAMING_LOG.open("w", encoding="utf-8") as log_f:
        for chunk in stream:
            if cancel and cancel.is_set():
                stream.close()
                raise InterruptedError("cancelled")
            if not chunk.choices:
                continue
            d = chunk.choices[0].delta
            token = getattr(d, "reasoning_content", None) or d.content
            if token:
                log_f.write(token)
                log_f.flush()
                recent.append(token)
                token_count += 1
                if token_count % 50 == 0:
                    if is_looping("".join(recent)):
                        stream.close()
                        raise RuntimeError("Loop detected in generation, aborting")
                    recent = []
            if d.content:
                parts.append(d.content)
    return "".join(parts).strip()


def extract_terms_one(client: OpenAI, prompt: str, chunk: dict, model: str = MODEL, temperature: float = 0.3, cancel: threading.Event | None = None, known_glossary: list[dict] | None = None) -> tuple[dict, list[dict]]:
    known_ctx = ""
    if known_glossary:
        lines = [f"{t['original']} → {t['translation']}"
                 for t in known_glossary if t.get("original") and t.get("translation")]
        if lines:
            known_ctx = "\n\n【已知术语，请保持译名一致，勿重复提取】\n" + "\n".join(lines)
    for attempt in range(MAX_RETRIES):
        if cancel and cancel.is_set():
            raise InterruptedError("cancelled")
        try:
            raw = _stream_content(
                client,
                cancel=cancel,
                model=model,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": f"提取以下文本中的所有专有名词：\n\n{chunk['text']}{known_ctx}"},
                ],
                temperature=temperature,
            )
            raw = raw.strip()
            if raw.startswith("```"):
                raw = "\n".join(raw.split("\n")[1:])
            if raw.endswith("```"):
                raw = "\n".join(raw.split("\n")[:-1])
            terms = json.loads(raw).get("terms", [])
            return chunk, terms
        except InterruptedError:
            raise
        except Exception:
            if attempt == MAX_RETRIES - 1:
                return chunk, []
            time.sleep(RETRY_DELAY)
    return chunk, []


def merge_terms(raw_terms: list[dict]) -> dict:
    source_rank = {"official": 0, "convention": 1, "keep": 2}
    merged = {}
    for term in raw_terms:
        original = term.get("original", "")
        translation = term.get("translation", "")
        if not original or not translation:
            continue
        key = original.lower()
        if key not in merged:
            merged[key] = {
                "original": original,
                "translation": translation,
                "type": term.get("type", "concept"),
                "source": term.get("source", "convention"),
                "alternatives": [],
            }
        else:
            existing = merged[key]
            new_rank = source_rank.get(term.get("source", "convention"), 99)
            cur_rank = source_rank.get(existing["source"], 99)
            if new_rank < cur_rank:
                existing["alternatives"].append(existing["translation"])
                existing["translation"] = translation
                existing["source"] = term.get("source", "convention")
                existing["original"] = original
            elif translation != existing["translation"] and translation not in existing["alternatives"]:
                existing["alternatives"].append(translation)

    result = {}
    for entry in merged.values():
        out = {"translation": entry["translation"], "type": entry["type"], "source": entry["source"]}
        if entry["alternatives"]:
            out["conflict"] = True
            out["alternatives"] = entry["alternatives"]
        result[entry["original"]] = out
    return result


def auto_resolve(client: OpenAI, prompt: str, glossary: dict, model: str = MODEL, temperature: float = 0.3, cancel: threading.Event | None = None) -> dict:
    conflicts = {k: v for k, v in glossary.items() if v.get("conflict")}
    if not conflicts:
        return glossary

    for batch_start in range(0, len(conflicts), RESOLVE_BATCH_SIZE):
        batch = list(conflicts.items())[batch_start:batch_start + RESOLVE_BATCH_SIZE]
        payload = [
            {
                "original": term,
                "candidates": [entry["translation"]] + entry.get("alternatives", []),
                "type": entry["type"],
            }
            for term, entry in batch
        ]
        for attempt in range(MAX_RETRIES):
            try:
                raw = _stream_content(
                    client,
                    cancel=cancel,
                    model=model,
                    messages=[
                        {"role": "system", "content": prompt},
                        {"role": "user", "content": f"请从以下每个词条的候选译名中选出最佳版本：\n\n{json.dumps(payload, ensure_ascii=False, indent=2)}"},
                    ],
                    temperature=temperature,
                )
                raw = raw.strip()
                if raw.startswith("```"):
                    raw = "\n".join(raw.split("\n")[1:])
                if raw.endswith("```"):
                    raw = "\n".join(raw.split("\n")[:-1])
                for r in json.loads(raw).get("resolutions", []):
                    orig, chosen = r.get("original", ""), r.get("chosen", "")
                    if orig in glossary and chosen:
                        glossary[orig]["translation"] = chosen
                        glossary[orig].pop("conflict", None)
                        glossary[orig].pop("alternatives", None)
                break
            except InterruptedError:
                raise
            except Exception:
                if attempt < MAX_RETRIES - 1:
                    time.sleep(RETRY_DELAY)

    for entry in glossary.values():
        entry.pop("conflict", None)
        entry.pop("alternatives", None)

    return glossary
