"""OCR Tesseract en sous-processus (sortie TSV) : mots, boîtes normalisées, confiance, langues, cache.

Tesseract n'est jamais importé (pas de pytesseract) : le binaire désigné par `config.TESSERACT_CMD` est
appelé sur un PNG temporaire, avec un délai (`config.OCR_TIMEOUT_S`). Toute panne devient une
`OcrUnavailableError` (binaire ou langue absents) ou une `OcrFailedError` (plantage, délai), avec un
message français affichable ; la sortie d'erreur de Tesseract est journalisée.

Le moteur est enfichable (`OcrEngine`) : un moteur PaddleOCR pourra remplacer `TesseractEngine`.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import cv2
import numpy as np
from PIL import Image
from pydantic import ValidationError

import config
from models.errors import OcrFailedError, OcrUnavailableError
from models.geometry import Box
from models.ocr import OcrResult, OcrWord
from services.image_service import binarize_sauvola, encode_png

logger = logging.getLogger("bookocr.ocr")

DEFAULT_DPI = 300
MIN_DPI, MAX_DPI = 100, 600
CACHE_FORMAT = 1               # incrémenté si le format du cache change


# --------------------------------------------------------------------------- binaire, version, langues


def _binary() -> str | None:
    """Chemin du binaire Tesseract (lu à chaque appel : les tests peuvent changer config.TESSERACT_CMD)."""
    command = str(config.TESSERACT_CMD or "")
    if not command:
        return None
    found = shutil.which(command)
    if found:
        return found
    path = Path(command)
    return str(path) if path.is_file() and os.access(path, os.X_OK) else None


def _install_help(languages: list[str] | None = None) -> str:
    names = languages or ["fra", "eng"]
    apt = " ".join(f"tesseract-ocr-{name.lower().replace('_', '-')}" for name in names)
    pacman = " ".join(f"tesseract-data-{name.lower()}" for name in names)
    return (f"Arch, Manjaro : sudo pacman -S {pacman} ; Debian, Ubuntu : sudo apt install {apt}")


def _missing_binary_error() -> OcrUnavailableError:
    return OcrUnavailableError(
        f"Tesseract est introuvable (commande « {config.TESSERACT_CMD} »). Installez-le "
        "(Arch : sudo pacman -S tesseract tesseract-data-fra ; Debian : sudo apt install tesseract-ocr "
        "tesseract-ocr-fra) ou indiquez son chemin dans BOOKOCR_TESSERACT.")


@functools.lru_cache(maxsize=8)
def _version_of(binary: str) -> str | None:
    try:
        done = subprocess.run([binary, "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.warning("Version de Tesseract illisible : %s", error)
        return None
    match = re.search(r"tesseract\s+v?(\S+)", done.stdout + done.stderr, re.IGNORECASE)
    return match.group(1) if match else None


@functools.lru_cache(maxsize=8)
def _languages_of(binary: str) -> tuple[str, ...]:
    try:
        done = subprocess.run([binary, "--list-langs"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as error:
        logger.warning("Liste des langues Tesseract illisible : %s", error)
        return ()
    lines = (done.stdout or done.stderr).splitlines()
    return tuple(sorted({line.strip() for line in lines[1:] if line.strip() and " " not in line.strip()}))


def tesseract_version() -> str | None:
    """« 5.5.3 », ou None si Tesseract est absent ou ne répond pas."""
    binary = _binary()
    return _version_of(binary) if binary else None


def available_languages() -> list[str]:
    """Langues de reconnaissance installées (sans « osd ») ; liste vide si Tesseract est absent."""
    binary = _binary()
    return [lang for lang in _languages_of(binary) if lang != "osd"] if binary else []


def has_osd() -> bool:
    binary = _binary()
    return bool(binary) and "osd" in _languages_of(binary)


def check_languages(languages: list[str]) -> None:
    """Lève OcrUnavailableError si Tesseract ou une des langues manque (avec la commande d'installation)."""
    if not _binary():
        raise _missing_binary_error()
    if not languages:
        raise OcrUnavailableError("Aucune langue d'OCR choisie : sélectionnez au moins une langue (par exemple fra).")
    installed = available_languages()
    missing = [lang for lang in languages if lang not in installed]
    if missing:
        raise OcrUnavailableError(
            f"Langue d'OCR non installée : {', '.join(missing)}. Langues disponibles : "
            f"{', '.join(installed) or 'aucune'}. Pour l'installer : {_install_help(missing)}.",
            detail=f"manquantes={missing}")


# --------------------------------------------------------------------------- appel de Tesseract


def _as_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 3:
        image = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY)
    if image.dtype != np.uint8:
        image = np.clip(image, 0, 255).astype(np.uint8)
    return np.ascontiguousarray(image)


def _call(image: np.ndarray, args: list[str], timeout: float | None = None, dpi: int = DEFAULT_DPI,
          failure_level: int = logging.ERROR) -> str:
    """Écrit `image` dans un PNG temporaire, lance Tesseract et renvoie sa sortie standard."""
    binary = _binary()
    if not binary:
        raise _missing_binary_error()
    timeout = timeout or config.OCR_TIMEOUT_S
    environment = {**os.environ}
    environment.setdefault("OMP_THREAD_LIMIT", "1")
    with tempfile.TemporaryDirectory(prefix="bookocr_ocr_") as folder:
        path = Path(folder) / "page.png"
        Image.fromarray(image).save(path, format="PNG", dpi=(dpi, dpi), compress_level=1)
        command = [binary, str(path), "stdout", "--dpi", str(dpi), *args]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=timeout, env=environment)
        except subprocess.TimeoutExpired:
            logger.error("Tesseract : délai de %s s dépassé (%s)", timeout, " ".join(args))
            raise OcrFailedError(f"La reconnaissance du texte a dépassé le délai de {timeout:.0f} s. "
                                 "Réduisez l'image ou augmentez BOOKOCR_OCR_TIMEOUT.") from None
        except OSError as error:
            logger.error("Tesseract n'a pas pu être lancé : %s", error)
            raise OcrFailedError("Tesseract n'a pas pu être lancé.", detail=str(error)) from error
    stderr = (done.stderr or "").strip()
    if done.returncode != 0:
        logger.log(failure_level, "Tesseract a échoué (code %s) : %s", done.returncode, stderr[-2000:])
        raise OcrFailedError(f"La reconnaissance du texte a échoué (Tesseract, code {done.returncode}).",
                             detail=stderr[-600:])
    if stderr:
        logger.debug("Tesseract (stderr) : %s", stderr[-2000:])
    return done.stdout


def parse_tsv(tsv: str, width: int, height: int) -> list[OcrWord]:
    """Mots (niveau 5) d'une sortie TSV de Tesseract, boîtes normalisées sur l'image lue."""
    words: list[OcrWord] = []
    for row in tsv.splitlines()[1:]:
        columns = row.split("\t")
        if len(columns) < 12 or columns[0] != "5":
            continue
        text = columns[11].strip()
        if not text:
            continue
        try:
            block, paragraph, line, word, left, top, box_w, box_h = (int(columns[i]) for i in range(2, 10))
            confidence = float(columns[10])
        except ValueError:
            continue
        if box_w <= 0 or box_h <= 0:
            continue
        box = Box.from_pixels(left, top, left + box_w, top + box_h, width, height)
        words.append(OcrWord(text=text, box=box, confidence=max(-1.0, min(confidence, 100.0)),
                             block_num=block, par_num=paragraph, line_num=line, word_num=word))
    return words


def estimate_dpi(gray: np.ndarray) -> int:
    """Résolution probable d'après la hauteur médiane des lettres (corps ≈ 10,5 pt) ; 300 si incertaine.

    Hauteur médiane mesurée d'une lettre ≈ 0,47 cadratin, soit ≈ 0,068 pouce pour un corps de 10,5 points.
    """
    small_side = 1600
    height, width = gray.shape[:2]
    scale = min(1.0, small_side / max(height, width))
    work = gray if scale >= 1.0 else cv2.resize(gray, (int(width * scale), int(height * scale)),
                                                interpolation=cv2.INTER_AREA)
    ink = cv2.threshold(work, 0, 1, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)[1]
    if not 0.002 < float(ink.mean()) < 0.4:
        return DEFAULT_DPI
    count, _, stats, _ = cv2.connectedComponentsWithStats(ink, connectivity=8)
    heights = stats[1:, 3]
    letters = heights[(heights >= 4) & (heights <= 0.05 * work.shape[0]) & (stats[1:, 2] <= 0.05 * work.shape[1])]
    if len(letters) < 80:
        return DEFAULT_DPI
    dpi = float(np.median(letters)) / scale / 0.068
    return int(round(min(max(dpi, MIN_DPI), MAX_DPI) / 10) * 10)


def _score(result: OcrResult) -> float:
    """Quantité de texte lu avec confiance (comparaison de deux lectures d'une même image)."""
    return float(sum(max(word.confidence, 0.0) * len(word.text) for word in result.words))


# --------------------------------------------------------------------------- moteurs


class OcrEngine(Protocol):
    """Moteur OCR enfichable. `recognize` fait UNE lecture (pas de cache ni de seconde lecture)."""

    name: str

    def version(self) -> str: ...

    def recognize(self, gray: np.ndarray, languages: list[str], psm: int = 3) -> OcrResult: ...


class TesseractEngine:
    name = "tesseract"

    def __init__(self, timeout: float | None = None, dpi: int | None = None) -> None:
        self.timeout = timeout
        self.dpi = dpi

    def version(self) -> str:
        return tesseract_version() or ""

    def recognize(self, gray: np.ndarray, languages: list[str], psm: int = 3) -> OcrResult:
        check_languages(languages)
        image = _as_gray(gray)
        height, width = image.shape[:2]
        result = OcrResult(width=width, height=height, languages=list(languages), engine=self.name,
                           engine_version=self.version(), psm=psm)
        if height < 4 or width < 4:
            return result
        dpi = self.dpi or estimate_dpi(image)
        tsv = _call(image, ["-l", "+".join(languages), "--psm", str(psm), "tsv"], self.timeout, dpi)
        result.words = parse_tsv(tsv, width, height)
        return result


# --------------------------------------------------------------------------- lecture d'une page


def cache_key(gray: np.ndarray, languages: list[str], psm: int, version: str) -> str:
    """SHA-256 de l'image (PNG) + langues + psm + version du moteur."""
    digest = hashlib.sha256(encode_png(_as_gray(gray))).hexdigest()
    payload = json.dumps({"image": digest, "languages": list(languages), "psm": psm, "version": version,
                          "format": CACHE_FORMAT, "low": config.LOW_CONFIDENCE}, sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _read_cache(path: Path) -> OcrResult | None:
    try:
        return OcrResult.model_validate_json(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValidationError, ValueError) as error:
        logger.warning("Cache OCR illisible, il sera recalculé : %s (%s)", path, error)
        return None


def _write_cache(path: Path, result: OcrResult) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(result.model_dump_json(), encoding="utf-8")
        temporary.replace(path)
    except OSError as error:
        logger.warning("Cache OCR non écrit (%s) : %s", path, error)


def run_ocr(gray: np.ndarray, languages: list[str], psm: int = 3, cache_dir: Path | None = None) -> OcrResult:
    """Lit une image grise. Seconde lecture binarisée (Sauvola) si la confiance moyenne est sous
    `config.LOW_CONFIDENCE` ; la meilleure est gardée. Résultat mis en cache si `cache_dir` est donné.
    Lève OcrUnavailableError (Tesseract ou langue absents) ou OcrFailedError.
    """
    check_languages(languages)
    image = _as_gray(gray)
    engine = TesseractEngine()
    cache_path = None
    if cache_dir is not None:
        cache_path = Path(cache_dir) / f"{cache_key(image, languages, psm, engine.version())}.json"
        cached = _read_cache(cache_path)
        if cached is not None:
            logger.info("OCR servi par le cache : %s", cache_path.name)
            return cached
    best = engine.recognize(image, languages, psm)
    if best.mean_confidence < config.LOW_CONFIDENCE and min(image.shape[:2]) >= 4:
        second = engine.recognize(binarize_sauvola(image), languages, psm)
        second.preparation = "binarized"
        logger.info("OCR : confiance %.1f en gris, %.1f binarisé", best.mean_confidence, second.mean_confidence)
        if _score(second) > _score(best):
            best = second
    if cache_path is not None:
        _write_cache(cache_path, best)
    return best


def ocr_region(gray: np.ndarray, box: Box, languages: list[str]) -> tuple[str, float]:
    """Relit une région comme un bloc (psm 6) : (texte, confiance moyenne). Lignes séparées par « \\n »."""
    image = _as_gray(gray)
    height, width = image.shape[:2]
    left, top, right, bottom = box.to_pixels(width, height)
    pad = max(2, int(0.004 * max(width, height)))
    crop = image[max(0, top - pad):min(height, bottom + pad), max(0, left - pad):min(width, right + pad)]
    if crop.shape[0] < 4 or crop.shape[1] < 4:
        return "", 0.0
    paper = int(np.percentile(crop, 95))
    crop = cv2.copyMakeBorder(crop, 10, 10, 10, 10, cv2.BORDER_CONSTANT, value=paper)
    result = TesseractEngine(timeout=min(config.OCR_TIMEOUT_S, 120)).recognize(crop, languages, psm=6)
    lines: dict[tuple[int, int, int], list[OcrWord]] = {}
    for word in result.words:
        lines.setdefault((word.block_num, word.par_num, word.line_num), []).append(word)
    text = "\n".join(" ".join(word.text for word in sorted(words, key=lambda item: item.word_num))
                     for _, words in sorted(lines.items()))
    return text, round(result.mean_confidence, 1)


# --------------------------------------------------------------------------- orientation (OSD)


@dataclass(frozen=True)
class OsdResult:
    """Réponse de l'OSD Tesseract. `rotate` = rotation HORAIRE (0, 90, 180, 270) à appliquer pour redresser."""

    rotate: int
    orientation_confidence: float
    script: str = ""
    script_confidence: float = 0.0


def run_osd(gray: np.ndarray, timeout: float | None = None) -> OsdResult | None:
    """OSD Tesseract (`--psm 0`). None si le modèle osd manque ou si Tesseract ne conclut pas
    (« Too few characters »…) : à traiter comme « incertain », jamais comme 0° sûr."""
    if not has_osd():
        return None
    image = _as_gray(gray)
    try:
        out = _call(image, ["--psm", "0"], timeout or min(config.OCR_TIMEOUT_S, 90), estimate_dpi(image),
                    failure_level=logging.INFO)     # « Too few characters » : réponse attendue, pas une panne
    except OcrFailedError as error:
        logger.info("OSD sans réponse : %s", error)
        return None
    rotate = re.search(r"Rotate:\s*(\d+)", out)
    confidence = re.search(r"Orientation confidence:\s*([\d.]+)", out)
    if not rotate or not confidence:
        return None
    script = re.search(r"Script:\s*(\S+)", out)
    script_confidence = re.search(r"Script confidence:\s*([\d.]+)", out)
    return OsdResult(int(rotate.group(1)) % 360, float(confidence.group(1)),
                     script.group(1) if script else "",
                     float(script_confidence.group(1)) if script_confidence else 0.0)
