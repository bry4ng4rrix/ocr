"""Application settings. Every value can be overridden by an environment variable (LIVREOCR_*)."""
from __future__ import annotations

import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


WORKSPACE_DIR = Path(os.environ.get("LIVREOCR_WORKSPACE", BASE_DIR / "workspace")).resolve()
PROJECTS_DIR = WORKSPACE_DIR / "projects"
LOG_DIR = WORKSPACE_DIR / "logs"

# Import limits.
MAX_FILE_MB = _int("LIVREOCR_MAX_FILE_MB", 60)          # per uploaded file
MAX_PAGES_PER_PROJECT = _int("LIVREOCR_MAX_PAGES", 800)
MAX_IMAGE_PIXELS = _int("LIVREOCR_MAX_IMAGE_PIXELS", 150_000_000)
MAX_PDF_PAGES_PER_FILE = _int("LIVREOCR_MAX_PDF_PAGES", 600)
PDF_IMPORT_DPI = _int("LIVREOCR_PDF_IMPORT_DPI", 300)
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf"}

# Processing.
TESSERACT_CMD = os.environ.get("LIVREOCR_TESSERACT", "tesseract")
OCR_TIMEOUT_S = _int("LIVREOCR_OCR_TIMEOUT", 300)
DEFAULT_LANGUAGES = ["fra", "eng"]
LOW_CONFIDENCE = 60           # word / block confidence (0-100) under which a result is flagged for review
PREVIEW_MAX_SIDE = 1600       # previews are computed on a copy at most this long
THUMBNAIL_MAX_SIDE = 320
UNDO_DEPTH = 50

# Fonts: bundled fonts first, then the system folders.
FONT_DIRS = [p for p in os.environ.get(
    "LIVREOCR_FONT_DIRS",
    f"{BASE_DIR / 'assets' / 'fonts'}:/usr/share/fonts:/usr/local/share/fonts:{Path.home() / '.local/share/fonts'}",
).split(":") if p]


def setup_logging() -> logging.Logger:
    """File + console logging, configured once per process."""
    logger = logging.getLogger("livreocr")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(LOG_DIR / "livreocr.log", encoding="utf-8")
        handler.setFormatter(fmt)
        logger.addHandler(handler)
    except OSError:
        logger.warning("Journal fichier indisponible dans %s", LOG_DIR)
    return logger
