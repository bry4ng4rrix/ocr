"""Réglages de l'application. Chaque valeur peut être surchargée par une variable d'environnement BOOKOCR_*.

Les chemins sont lus à chaque appel (fonctions) afin que les tests puissent rediriger les données.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent


def _load_env_file(path: Path) -> None:
    """Lit un fichier .env simple (CLE=valeur) sans écraser les variables déjà définies.

    Ignoré si BOOKOCR_SKIP_DOTENV est défini (tests : réglages déterministes).
    """
    if os.environ.get("BOOKOCR_SKIP_DOTENV") or not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_env_file(BASE_DIR / ".env")


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def data_dir() -> Path:
    """Racine des données de travail (projets, journaux)."""
    return Path(os.environ.get("BOOKOCR_DATA_DIR", BASE_DIR / "data")).resolve()


def projects_dir() -> Path:
    return data_dir() / "projects"


def log_dir() -> Path:
    return data_dir() / "logs"


def db_path() -> Path:
    """Emplacement de la base SQLite locale."""
    return data_dir() / "bookocr.db"


# Import
MAX_FILE_MB = _int("BOOKOCR_MAX_FILE_MB", 80)
MAX_PAGES_PER_PROJECT = _int("BOOKOCR_MAX_PAGES", 10_000)
MAX_IMAGE_PIXELS = _int("BOOKOCR_MAX_IMAGE_PIXELS", 200_000_000)
MAX_PDF_PAGES_PER_FILE = _int("BOOKOCR_MAX_PDF_PAGES", 800)
PDF_IMPORT_DPI = _int("BOOKOCR_PDF_IMPORT_DPI", 300)
ALLOWED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff", ".pdf"}

# Traitement
TESSERACT_CMD = os.environ.get("BOOKOCR_TESSERACT", "tesseract")
OCR_TIMEOUT_S = _int("BOOKOCR_OCR_TIMEOUT", 300)
DEFAULT_LANGUAGES = ["fra", "eng"]
LOW_CONFIDENCE = _int("BOOKOCR_LOW_CONFIDENCE", 60)   # confiance (0-100) sous laquelle un mot ou un bloc est à vérifier
ANALYSIS_MAX_SIDE = 2400      # côté maximal des copies réduites utilisées pour les détections
PREVIEW_MAX_SIDE = 1600
THUMBNAIL_MAX_SIDE = 320
HISTORY_DEPTH = _int("BOOKOCR_HISTORY_DEPTH", 30)     # versions conservées par page
DEFAULT_AUTOSAVE_SECONDS = 30

# Export
SOFFICE_CMD = os.environ.get("BOOKOCR_SOFFICE", "soffice")
SOFFICE_TIMEOUT_S = _int("BOOKOCR_SOFFICE_TIMEOUT", 240)
FONT_DIRS = [p for p in os.environ.get(
    "BOOKOCR_FONT_DIRS",
    f"{BASE_DIR / 'assets' / 'fonts'}:/usr/share/fonts:/usr/local/share/fonts:{Path.home() / '.local/share/fonts'}",
).split(":") if p]
DEFAULT_FONT_FAMILY = "Liberation Serif"
DEFAULT_SANS_FAMILY = "Liberation Sans"

# Routage qualité (seuils mesurables, calibrables)
ROUTE_CONFIDENCE_RELIABLE = _int("BOOKOCR_ROUTE_RELIABLE", 80)
ROUTE_CONFIDENCE_DOUBTFUL = _int("BOOKOCR_ROUTE_DOUBTFUL", 60)

# IA locale / cloud : désactivées par défaut, aucun appel si false
OLLAMA_ENABLED = _bool("OLLAMA_ENABLED", False)
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
OLLAMA_VISION_MODEL = os.environ.get("OLLAMA_VISION_MODEL", "gemma3:4b")
OLLAMA_TIMEOUT = _int("OLLAMA_TIMEOUT", 120)
OLLAMA_MAX_RETRIES = max(0, _int("OLLAMA_MAX_RETRIES", 2))
OLLAMA_ANALYSIS_ENABLED = _bool("OLLAMA_ANALYSIS_ENABLED", True)     # analyse visuelle Gemma (si OLLAMA_ENABLED)
OLLAMA_AUTO_APPLY = _bool("OLLAMA_AUTO_APPLY", False)                # false : Gemma propose, l'utilisateur valide
OLLAMA_MAX_PARALLEL_REQUESTS = max(1, _int("OLLAMA_MAX_PARALLEL_REQUESTS", 1))
OLLAMA_ANALYSIS_MAX_SIDE = _int("OLLAMA_ANALYSIS_MAX_SIDE", 896)     # Gemma 3 voit les images en 896 × 896

# Numérisation fidèle (PDF image + couche OCR invisible)
SCAN_PDF_DPI = _int("BOOKOCR_SCAN_PDF_DPI", 300)          # taille de page PDF = pixels × 72 / dpi
SCAN_JPEG_QUALITY = _int("BOOKOCR_SCAN_JPEG_QUALITY", 92)  # unique encodage avec perte de l'image finale
SCAN_BATCH_SIZE = _int("BOOKOCR_SCAN_BATCH_SIZE", 10)      # pages traitées entre deux relectures du projet
SCAN_MAX_ATTEMPTS = _int("BOOKOCR_SCAN_MAX_ATTEMPTS", 3)   # tentatives par page avant FAILED définitif
GEMINI_ENABLED = _bool("GEMINI_ENABLED", False)
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.0-flash")
GEMINI_TIMEOUT = _int("GEMINI_TIMEOUT", 60)
GEMINI_MAX_CALLS = _int("GEMINI_MAX_CALLS", 50)


def gemini_api_key() -> str:
    """Clé lue à l'appel : jamais journalisée, jamais inscrite dans le code."""
    return os.environ.get("GEMINI_API_KEY", "").strip()


def setup_logging() -> logging.Logger:
    """Journal console + fichier, configuré une seule fois par processus."""
    logger = logging.getLogger("bookocr")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logger.addHandler(console)
    try:
        log_dir().mkdir(parents=True, exist_ok=True)
        handler = logging.FileHandler(log_dir() / "bookocr.log", encoding="utf-8")
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    except OSError:
        logger.warning("Journal fichier indisponible dans %s", log_dir())
    return logger
