"""
Central settings loader.
Reads all configuration from .env and exposes typed constants.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root
BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


# ── Telegram ──────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_USER_ID: int = int(os.getenv("TELEGRAM_USER_ID", "0"))


# ── Gemini AI ─────────────────────────────────────────────────────
GEMINI_API_KEY: str = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")


# ── YouTube ───────────────────────────────────────────────────────
YOUTUBE_API_KEY: str = os.getenv("YOUTUBE_API_KEY", "")
YOUTUBE_CLIENT_SECRETS_FILE: str = os.getenv(
    "YOUTUBE_CLIENT_SECRETS_FILE", "config/client_secrets.json"
)


# ── Instagram ─────────────────────────────────────────────────────
INSTAGRAM_ACCESS_TOKEN: str = os.getenv("INSTAGRAM_ACCESS_TOKEN", "")
INSTAGRAM_ACCOUNT_ID: str = os.getenv("INSTAGRAM_ACCOUNT_ID", "")


# ── Whisper ───────────────────────────────────────────────────────
WHISPER_MODEL: str = os.getenv("WHISPER_MODEL", "small")
WHISPER_DEVICE: str = os.getenv("WHISPER_DEVICE", "cpu")


# ── Clip Settings ─────────────────────────────────────────────────
CLIP_MIN_SECONDS: int = int(os.getenv("CLIP_MIN_SECONDS", "30"))
CLIP_MAX_SECONDS: int = int(os.getenv("CLIP_MAX_SECONDS", "90"))
CLIPS_PER_VIDEO: int = int(os.getenv("CLIPS_PER_VIDEO", "5"))

# Supported formats
VALID_FORMATS = {"vertical", "horizontal"}
_raw_formats = os.getenv("DEFAULT_FORMATS", "vertical,horizontal")
DEFAULT_FORMATS: list[str] = [
    f.strip() for f in _raw_formats.split(",") if f.strip() in VALID_FORMATS
]

# Video dimensions per format
FORMAT_DIMENSIONS = {
    "vertical":   {"width": 1080, "height": 1920},  # 9:16  — Reels / Shorts
    "horizontal": {"width": 1920, "height": 1080},  # 16:9  — Cinematic Shorts
}


# ── Storage ───────────────────────────────────────────────────────
DOWNLOADS_DIR: Path = BASE_DIR / os.getenv("DOWNLOADS_DIR", "output/downloads")
CLIPS_DIR:     Path = BASE_DIR / os.getenv("CLIPS_DIR",     "output/clips")
LOGS_DIR:      Path = BASE_DIR / os.getenv("LOGS_DIR",      "output/logs")
DB_PATH:       Path = BASE_DIR / "data.db"

# Ensure directories exist at import time
for _dir in (DOWNLOADS_DIR, CLIPS_DIR, LOGS_DIR):
    _dir.mkdir(parents=True, exist_ok=True)


# ── Channel Monitor ───────────────────────────────────────────────
MONITOR_INTERVAL_MINUTES: int = int(os.getenv("MONITOR_INTERVAL_MINUTES", "15"))


# ── Validation helper ─────────────────────────────────────────────
def validate_settings() -> list[str]:
    """
    Returns a list of missing/invalid setting names.
    Call this at startup to catch config errors early.
    """
    missing = []
    if not TELEGRAM_BOT_TOKEN:
        missing.append("TELEGRAM_BOT_TOKEN")
    if not TELEGRAM_USER_ID:
        missing.append("TELEGRAM_USER_ID")
    if not GEMINI_API_KEY:
        missing.append("GEMINI_API_KEY")
    if not YOUTUBE_API_KEY:
        missing.append("YOUTUBE_API_KEY")
    return missing
