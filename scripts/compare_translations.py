"""Compare two translated EPUBs against their common source.

Objective metrics per book:
  - structural fidelity (chapter/paragraph counts vs source)
  - untranslated residue (paragraphs with no CJK, latin word density)
  - parenthetical-gloss artifacts (parens added beyond the source rate)
  - repetition artifacts (duplicated paragraphs)

Optional --judge N: blind A/B comparison of N sampled passages using the
local OpenAI-compatible server (labels randomized per sample).

Usage:
  python scripts/compare_translations.py SOURCE.epub A.epub B.epub
      [--label-a qwen] [--label-b gemma]
      [--judge 12] [--base-url http://127.0.0.1:8080/v1]
"""

import argparse
import json
import random
import re
import zipfile
from pathlib import Path

from bs4 import BeautifulSoup

CJK_RE = re.compile(r"[一-鿿㐀-䶿]")
LATIN_WORD_RE = re.compile(r"[A-Za-z]{2,}")
PAREN_GLOSS_RE = re.compile(r"[（(][^（）()]{0,40}[A-Za-z]{2,}[^（）()]{0,40}[）)]")


def read_paras(path: Path) -> dict[str, list[str]]:
    """{zip filename: [paragraph texts]} for every content document with <p> tags."""
    out: dict[str, list[str]] = {}
    with zipfile.ZipFile(str(path)) as z:
        for name in z.namelist():
            if not name.lower().endswith((".xhtml", ".html", ".htm")):
                continue
            soup = BeautifulSoup(z.read(name), "html.parser")
            paras = [p.get_text(separator=" ", strip=True) for p in soup.find_all("p")]
            paras = [p for p in paras if p]
            if paras:
                out[name] = paras
    return out


def cjk_count(text: str) -> int:
    return len(CJK_RE.findall(text))


def analyze(src: dict[str, list[str]], trans: dict[str, list[str]]) -> dict:
    m = {
        "chapters_missing": 0, "para_drift_total": 0, "chapters_with_drift": 0,
        "paras_total": 0, "paras_no_cjk": 0, "latin_words": 0, "chars_total": 0,
        "cjk_chars": 0, "gloss_parens": 0, "paren_chars": 0, "dup_consecutive": 0,
        "worst_drift": [], "untranslated_samples": [], "gloss_samples": [],
    }
    seen_gloss: set[str] = set()
    for name, s_paras in src.items():
        t_paras = trans.get(name)
        if t_paras is None:
            m["chapters_missing"] += 1
            continue
        drift = abs(len(t_paras) - len(s_paras))
        m["para_drift_total"] += drift
        if drift:
            m["chapters_with_drift"] += 1
            m["worst_drift"].append((drift, name, len(s_paras), len(t_paras)))
        prev = None
        for p in t_paras:
            m["paras_total"] += 1
            m["chars_total"] += len(p)
            m["cjk_chars"] += cjk_count(p)
            words = LATIN_WORD_RE.findall(p)
            m["latin_words"] += len(words)
            if cjk_count(p) == 0 and len(words) >= 3:
                m["paras_no_cjk"] += 1
                if len(m["untranslated_samples"]) < 5:
                    m["untranslated_samples"].append(f"{name}: {p[:90]}")
            for g in PAREN_GLOSS_RE.findall(p):
                m["gloss_parens"] += 1
                if g not in seen_gloss and len(m["gloss_samples"]) < 8:
                    seen_gloss.add(g)
                    m["gloss_samples"].append(g)
            m["paren_chars"] += p.count("（") + p.count("(")
            if prev is not None and len(p) > 15 and p == prev:
                m["dup_consecutive"] += 1
            prev = p
    m["worst_drift"].sort(reverse=True)
    return m


def fmt_metrics(label: str, m: dict, src_paras: int, src_words: int) -> list[str]:
    lines = [f"── {label} ──"]
    lines.append(f"  chapters missing:        {m['chapters_missing']}")
    lines.append(f"  paragraphs (src {src_paras}): {m['paras_total']}  "
                 f"(drift {m['para_drift_total']} across {m['chapters_with_drift']} chapters)")
    pct = 100 * m["paras_no_cjk"] / max(1, m["paras_total"])
    lines.append(f"  untranslated paragraphs: {m['paras_no_cjk']} ({pct:.2f}%)")
    lines.append(f"  latin words / 1k CJK:    {1000 * m['latin_words'] / max(1, m['cjk_chars']):.1f}")
    lines.append(f"  gloss-parens (en inside):{m['gloss_parens']}")
    lines.append(f"  parens / 10k chars:      {10000 * m['paren_chars'] / max(1, m['chars_total']):.1f}")
    lines.append(f"  CJK chars / src word:    {m['cjk_chars'] / max(1, src_words):.2f}")
    lines.append(f"  consecutive dup paras:   {m['dup_consecutive']}")
    if m["worst_drift"]:
        worst = ", ".join(f"{n} ({s}→{t})" for d, n, s, t in m["worst_drift"][:4])
        lines.append(f"  worst drift:             {worst}")
    return lines


def judge(src, ta, tb, label_a, label_b, n, base_url, seed=42):
    from openai import OpenAI
    client = OpenAI(api_key="dummy", base_url=base_url)
    common = [k for k in src if k in ta and k in tb
              and len(src[k]) >= 8 and len(ta[k]) >= 8 and len(tb[k]) >= 8]
    common.sort()
    if not common:
        print("\nNo common chapters for judging.")
        return
    rng = random.Random(seed)
    picks = sorted(rng.sample(common, n) if n <= len(common)
                   else [common[int(i * len(common) / n) % len(common)] for i in range(n)])
    votes = {label_a: 0, label_b: 0, "tie": 0, "invalid": 0}
    print(f"\n══ Blind LLM judge ({n} passages, randomized labels) ══")
    for i, name in enumerate(picks):
        idx = rng.randrange(2, min(len(src[name]), len(ta[name]), len(tb[name])) - 3)
        s_x = "\n".join(src[name][idx:idx + 3])
        a_x = "\n".join(ta[name][idx:idx + 3])
        b_x = "\n".join(tb[name][idx:idx + 3])
        flip = rng.random() < 0.5
        first, second = (b_x, a_x) if flip else (a_x, b_x)
        prompt = (
            "你是资深文学翻译评审。比较同一英文原文的两个中文译文，从准确性、完整性、"
            "流畅度、文学性四方面评判。不要偏向更长或更短的译文。\n\n"
            f"【原文】\n{s_x}\n\n【译文甲】\n{first}\n\n【译文乙】\n{second}\n\n"
            '只输出 JSON：{"better": "甲"或"乙"或"平", "reason": "一句话理由"}'
        )
        try:
            resp = client.chat.completions.create(
                model="llama.cpp", temperature=0.0, max_tokens=2000,
                messages=[{"role": "user", "content": prompt}],
                response_format={"type": "json_object"},
                extra_body={"chat_template_kwargs": {"enable_thinking": False}},
            )
            verdict = json.loads(resp.choices[0].message.content or "{}")
            better = str(verdict.get("better", ""))
        except Exception as exc:
            print(f"  [{i + 1:2}] {name}: judge call failed: {exc}")
            votes["invalid"] += 1
            continue
        if "甲" in better:
            winner = (label_b if flip else label_a)
        elif "乙" in better:
            winner = (label_a if flip else label_b)
        elif "平" in better:
            winner = "tie"
        else:
            winner = "invalid"
        votes[winner] += 1
        reason = str(verdict.get("reason", ""))[:80]
        print(f"  [{i + 1:2}] {Path(name).stem}: {winner}  — {reason}")
    print(f"\n  VOTES: {label_a}={votes[label_a]}  {label_b}={votes[label_b]}  "
          f"tie={votes['tie']}  invalid={votes['invalid']}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("source"); ap.add_argument("trans_a"); ap.add_argument("trans_b")
    ap.add_argument("--label-a", default="A"); ap.add_argument("--label-b", default="B")
    ap.add_argument("--judge", type=int, default=0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--base-url", default="http://127.0.0.1:8080/v1")
    args = ap.parse_args()

    src = read_paras(Path(args.source))
    # Mirror the translator's extraction rule (paragraphs >10 chars survive), so
    # source indexes align with translated <p> indexes
    src = {k: [p for p in v if len(p.replace(" ", "")) > 10] for k, v in src.items()}
    src = {k: v for k, v in src.items() if v}
    ta = read_paras(Path(args.trans_a))
    tb = read_paras(Path(args.trans_b))
    src_paras = sum(len(v) for v in src.values())
    src_words = sum(len(p.split()) for v in src.values() for p in v)

    print(f"Source: {len(src)} chapters, {src_paras} paragraphs, {src_words} words\n")
    ma = analyze(src, ta)
    mb = analyze(src, tb)
    for line in fmt_metrics(args.label_a, ma, src_paras, src_words):
        print(line)
    print()
    for line in fmt_metrics(args.label_b, mb, src_paras, src_words):
        print(line)

    for label, m in ((args.label_a, ma), (args.label_b, mb)):
        if m["untranslated_samples"]:
            print(f"\n  {label} untranslated samples:")
            for s in m["untranslated_samples"]:
                print(f"    · {s}")
        if m["gloss_samples"]:
            print(f"\n  {label} gloss samples:")
            for s in m["gloss_samples"]:
                print(f"    · {s}")

    if args.judge:
        judge(src, ta, tb, args.label_a, args.label_b, args.judge, args.base_url,
              seed=args.seed)


if __name__ == "__main__":
    main()
