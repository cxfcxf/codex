def is_looping(window: str, threshold: int = 5) -> bool:
    """Detect repetitive loops in streamed token output.

    Requires both an absolute count above threshold AND the word dominating
    >25% of the window — prevents false positives from common stopwords.
    """
    words = [w for w in window.split() if w != "->"]
    if not words:
        return False
    counts: dict[str, int] = {}
    for w in words:
        counts[w] = counts.get(w, 0) + 1
    max_count = max(counts.values())
    return max_count > threshold and max_count / len(words) > 0.25
