import os
from pathlib import Path

# Load .env if present
_env_file = Path(__file__).parent.parent / ".env"
if _env_file.exists():
    for _line in _env_file.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

DEFAULT_BASE_URL = "https://api.deepseek.com"
MODEL = "deepseek-chat"
DEFAULT_TEMPERATURE = 0.3
TRANSLATE_WORKERS = 10
CHUNK_SIZE_WORDS = 1500
EXTRACT_CHUNK_WORDS = 3000
OVERLAP_WORDS = 150
MAX_RETRIES = 3
RETRY_DELAY = 5
RESOLVE_BATCH_SIZE = 30

WORKSPACE_DIR = Path("workspace")
WORKSPACE_DIR.mkdir(exist_ok=True)

LANGUAGE_NAMES = {
    "en": "英文", "zh": "简体中文", "zh-tw": "繁体中文",
    "ja": "日文", "ko": "韩文", "fr": "法文", "de": "德文",
    "es": "西班牙文", "it": "意大利文", "pt": "葡萄牙文",
    "ru": "俄文", "ar": "阿拉伯文",
}

LANG_NAMES_EN = {
    "en": "english", "zh": "chinese", "zh-tw": "chinese_traditional",
    "ja": "japanese", "ko": "korean", "fr": "french", "de": "german",
    "es": "spanish", "it": "italian", "ru": "russian",
}
