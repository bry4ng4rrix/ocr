"""Tesseract OCR run as a subprocess (TSV output): words with boxes and confidence, orientation (OSD).

Tesseract is never imported (no pytesseract): the binary named by `config.TESSERACT_CMD` is called
with a temporary PNG, inside a temporary folder removed afterwards. Every failure becomes an
`OCRUnavailableError` (binary or language missing) or an `OCRFailedError` (crash, timeout), with a
French message shown as is to the user.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

import config

from .coordinates import norm_to_px
from .document_models import Box
from .errors import OCRFailedError, OCRUnavailableError

OCR_DPI = 300                 # resolution declared to Tesseract (the frame's real dpi is unknown)
OSD_SAMPLE_SIDE = 1500        # long side of the copy sent to the orientation detection
OSD_TRUSTED = 10.0            # OSD confidence accepted without a reading check
OSD_MINIMUM = 1.0             # below this the OSD answer is not even checked
READING_MIN_SCORE = 150.0     # reading check: score an upright page must reach

INSTALL_HELP = ("Installez-le puis relancez l'application :\n"
                "- Debian, Ubuntu : sudo apt install tesseract-ocr tesseract-ocr-fra tesseract-ocr-eng\n"
                "- Arch, Manjaro : sudo pacman -S tesseract tesseract-data-fra tesseract-data-eng\n"
                "Si Tesseract est installé ailleurs, indiquez son chemin dans la variable LIVREOCR_TESSERACT.")


@dataclass
class OCRWordPx:
    """One word read by Tesseract, in pixels of the image given to it."""
    text: str
    left: int
    top: int
    width: int
    height: int
    conf: float = -1.0        # 0..100, -1 unknown
    block: int = 0
    par: int = 0
    line: int = 0
    word: int = 0

    @property
    def right(self) -> int:
        return self.left + self.width

    @property
    def bottom(self) -> int:
        return self.top + self.height


@dataclass
class OCRResult:
    words: list[OCRWordPx] = field(default_factory=list)
    width: int = 0
    height: int = 0
    languages: list[str] = field(default_factory=list)
    preparation: str = "gris"   # "gris" or "binarisé"

    @property
    def mean_confidence(self) -> float:
        confs = [w.conf for w in self.words if w.conf >= 0]
        return float(np.mean(confs)) if confs else -1.0

    @property
    def score(self) -> float:
        """Amount of confidently read text, used to compare two readings of the same page."""
        return float(sum(max(w.conf, 0.0) * len(w.text) for w in self.words))


# --------------------------------------------------------------------------- binary and languages

def _binary() -> str | None:
    cmd = str(config.TESSERACT_CMD or "")
    if not cmd:
        return None
    found = shutil.which(cmd)
    if found:
        return found
    path = Path(cmd)
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def _missing_binary_error() -> OCRUnavailableError:
    return OCRUnavailableError(f"Tesseract est introuvable (commande « {config.TESSERACT_CMD} »). {INSTALL_HELP}")


def tesseract_version() -> str | None:
    """'5.5.3' or None when the binary is missing or does not answer."""
    binary = _binary()
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return None
    match = re.search(r"tesseract\s+v?(\S+)", out.stdout + out.stderr, re.IGNORECASE)
    return match.group(1) if match else None


def _all_languages(binary: str) -> list[str]:
    try:
        out = subprocess.run([binary, "--list-langs"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return []
    lines = (out.stdout or out.stderr).splitlines()
    return sorted({line.strip() for line in lines[1:] if line.strip() and " " not in line.strip()})


def available_languages() -> list[str]:
    """Installed recognition languages (without 'osd'); empty when Tesseract is missing."""
    binary = _binary()
    return [lang for lang in _all_languages(binary) if lang != "osd"] if binary else []


def check_languages(langs: list[str]) -> None:
    """Raise OCRUnavailableError when Tesseract or one of the languages is missing."""
    binary = _binary()
    if not binary:
        raise _missing_binary_error()
    if not langs:
        raise OCRUnavailableError("Aucune langue d'OCR n'est choisie : sélectionnez au moins une langue (par exemple fra).")
    installed = set(available_languages())
    missing = [lang for lang in langs if lang not in installed]
    if missing:
        names = ", ".join(missing)
        apt = " ".join(f"tesseract-ocr-{m.lower().replace('_', '-')}" for m in missing)
        pac = " ".join(f"tesseract-data-{m.lower()}" for m in missing)
        raise OCRUnavailableError(
            f"Langue d'OCR non installée : {names}. Langues disponibles : {', '.join(sorted(installed)) or 'aucune'}.\n"
            f"- Debian, Ubuntu : sudo apt install {apt}\n- Arch, Manjaro : sudo pacman -S {pac}")


# --------------------------------------------------------------------------- running Tesseract

def _as_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    if image.dtype != np.uint8:
        image = np.clip(image, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(image)


def _call(image: np.ndarray, args: list[str], timeout: int | None) -> str:
    """Write `image` to a temporary PNG, run Tesseract on it and return its standard output."""
    binary = _binary()
    if not binary:
        raise _missing_binary_error()
    timeout = timeout or config.OCR_TIMEOUT_S
    with tempfile.TemporaryDirectory(prefix="livreocr_ocr_") as tmp:
        path = Path(tmp) / "page.png"
        if not cv2.imwrite(str(path), image):
            raise OCRFailedError("Impossible de préparer l'image pour l'OCR (écriture du fichier temporaire).")
        try:
            result = subprocess.run([binary, str(path), "stdout", *args], capture_output=True, text=True,
                                    timeout=timeout, env={**os.environ, "OMP_THREAD_LIMIT": "1"})
        except subprocess.TimeoutExpired:
            raise OCRFailedError(f"L'OCR a dépassé le délai de {timeout} s. Réduisez la taille de l'image "
                                 "ou augmentez LIVREOCR_OCR_TIMEOUT.") from None
        except OSError as exc:
            raise OCRFailedError(f"Tesseract n'a pas pu être lancé : {exc}") from None
    if result.returncode != 0:
        detail = (result.stderr or "").strip()[-600:]
        raise OCRFailedError(f"Tesseract a échoué (code {result.returncode}). {detail}".strip())
    return result.stdout


def _parse_tsv(tsv: str) -> list[OCRWordPx]:
    words = []
    for row in tsv.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or cols[0] != "5":
            continue
        text = cols[11].strip()
        if not text:
            continue
        try:
            block, par, line, word, left, top, width, height = (int(cols[i]) for i in range(2, 10))
            conf = float(cols[10])
        except ValueError:
            continue
        words.append(OCRWordPx(text, left, top, width, height, max(-1.0, min(conf, 100.0)), block, par, line, word))
    return words


def run_ocr(gray: np.ndarray, languages: list[str], psm: int = 3, timeout: int | None = None) -> OCRResult:
    """One Tesseract reading of a gray (or RGB) image. Raises OCRUnavailableError / OCRFailedError."""
    check_languages(languages)
    image = _as_gray(gray)
    h, w = image.shape[:2]
    if h < 2 or w < 2:
        return OCRResult([], w, h, list(languages))
    tsv = _call(image, ["-l", "+".join(languages), "--psm", str(psm), "--dpi", str(OCR_DPI), "tsv"], timeout)
    return OCRResult(_parse_tsv(tsv), w, h, list(languages), "gris")


def _binarize(gray: np.ndarray) -> np.ndarray:
    """Sauvola threshold (window ~ 1/40 of the long side, k = 0.2) on a lightly smoothed copy."""
    g = cv2.GaussianBlur(gray, (3, 3), 0).astype(np.float64)
    win = max(15, (max(g.shape) // 40) | 1)
    mean = cv2.boxFilter(g, -1, (win, win), borderType=cv2.BORDER_REPLICATE)
    sq = cv2.boxFilter(g * g, -1, (win, win), borderType=cv2.BORDER_REPLICATE)
    std = np.sqrt(np.maximum(sq - mean * mean, 0.0))
    threshold = mean * (1.0 + 0.2 * (std / 128.0 - 1.0))
    return np.where(g > threshold, 255, 0).astype(np.uint8)


def read_page(gray: np.ndarray, languages: list[str], retry_binarized: bool = True,
              low_confidence: float = 60) -> OCRResult:
    """Read a page; retry with a binarized copy when no text or low-confidence text was found."""
    image = _as_gray(gray)
    first = run_ocr(image, languages)
    mean = first.mean_confidence
    if not retry_binarized or (first.words and mean >= low_confidence):
        return first
    second = run_ocr(_binarize(image), languages)
    if second.score > first.score:
        second.preparation = "binarisé"
        return second
    return first


def ocr_region(gray: np.ndarray, box: Box, languages: list[str]) -> tuple[str, float]:
    """Text of one region (normalized box) read as a single block (psm 6): (text, mean confidence).

    Lines are joined with "\\n"; confidence is -1 when nothing was read.
    """
    image = _as_gray(gray)
    h, w = image.shape[:2]
    left, top, right, bottom = norm_to_px(box, w, h)
    pad = max(2, int(0.004 * max(w, h)))
    crop = image[max(0, top - pad):min(h, bottom + pad), max(0, left - pad):min(w, right + pad)]
    if crop.shape[0] < 4 or crop.shape[1] < 4:
        return "", -1.0
    paper = int(np.percentile(crop, 95))
    crop = cv2.copyMakeBorder(crop, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=paper)
    result = run_ocr(crop, languages, psm=6, timeout=min(config.OCR_TIMEOUT_S, 120))
    lines: dict[tuple[int, int, int], list[OCRWordPx]] = {}
    for word in result.words:
        lines.setdefault((word.block, word.par, word.line), []).append(word)
    text = "\n".join(" ".join(w.text for w in sorted(ws, key=lambda w: w.word)) for _, ws in sorted(lines.items()))
    return text, round(result.mean_confidence, 1)


# --------------------------------------------------------------------------- orientation

def _turn_clockwise(image: np.ndarray, degrees: int) -> np.ndarray:
    return np.ascontiguousarray(np.rot90(image, k=-(degrees // 90) % 4))


def _osd(gray: np.ndarray) -> tuple[int, float] | None:
    try:
        out = _call(gray, ["--psm", "0", "--dpi", str(OCR_DPI)], timeout=min(config.OCR_TIMEOUT_S, 90))
    except OCRFailedError:
        return None    # "Too few characters" and the like: no answer
    rotate = re.search(r"Rotate:\s*(\d+)", out)
    confidence = re.search(r"Orientation confidence:\s*([\d.]+)", out)
    if not rotate or not confidence:
        return None
    return int(rotate.group(1)) % 360, float(confidence.group(1))


def _reading_score(gray: np.ndarray, languages: list[str]) -> float:
    """Sum of confidences of real words (3 letters or more) read with psm 6."""
    try:
        words = run_ocr(gray, languages, psm=6, timeout=min(config.OCR_TIMEOUT_S, 90)).words
    except OCRFailedError:
        return 0.0
    return float(sum(w.conf for w in words if w.conf >= 50 and len(re.findall(r"[^\W\d_]", w.text)) >= 3))


def detect_orientation(rgb: np.ndarray, languages: list[str] | None = None) -> tuple[int, float] | None:
    """(clockwise quarter turn to apply so the text reads upright, confidence) or None.

    Tesseract OSD (psm 0) on a reduced gray copy. A confident answer is returned as is; a weak
    one is checked by reading the page turned both ways along the detected axis. None when no
    answer is reliable (too little text, OSD model missing, Tesseract missing).
    """
    binary = _binary()
    if not binary or "osd" not in _all_languages(binary):
        return None
    gray = _as_gray(rgb)
    h, w = gray.shape[:2]
    scale = min(1.0, OSD_SAMPLE_SIDE / max(h, w))
    if scale < 1.0:
        gray = cv2.resize(gray, (max(1, round(w * scale)), max(1, round(h * scale))), interpolation=cv2.INTER_AREA)
    osd = _osd(gray)
    if osd is None or osd[1] < OSD_MINIMUM:
        return None
    angle, confidence = osd
    if confidence >= OSD_TRUSTED:
        return angle, confidence
    langs = [lang for lang in (languages or config.DEFAULT_LANGUAGES) if lang in available_languages()]
    if not langs:
        return None
    guess = _reading_score(_turn_clockwise(gray, angle), langs)
    opposite = _reading_score(_turn_clockwise(gray, (angle + 180) % 360), langs)
    if guess >= READING_MIN_SCORE and guess >= 1.5 * opposite:
        return angle, confidence
    if opposite >= READING_MIN_SCORE and opposite >= 1.5 * guess:
        return (angle + 180) % 360, confidence
    return None
