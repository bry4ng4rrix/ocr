"""Analyse visuelle d'une page par Gemma 3 (Ollama local) : diagnostic structuré, jamais une transformation.

Le modèle reçoit une copie JPEG réduite de la page redressée et renvoie un JSON contraint par le schéma de
`models.scanning.VisualAnalysis` (catégories bornées, liste blanche d'actions). La réponse est validée par
Pydantic ; tout écart donne un `AnalysisRecord` au statut explicite et le pipeline continue sans IA.
Résultats mis en cache par empreinte de l'image envoyée, modèle (nom + digest) et version de la consigne.
"""
from __future__ import annotations

import copy
import hashlib
import io
import json
import logging
import time
from pathlib import Path

import numpy as np
from PIL import Image
from pydantic import ValidationError

import config
from models.errors import AiResponseError, AiTimeoutError, AiUnavailableError
from models.scanning import AnalysisRecord, ScanAction, VisualAnalysis
from services import ollama_service

logger = logging.getLogger("bookocr.analysis")

PROMPT_VERSION = "scan-v1"
ANALYSIS_JPEG_QUALITY = 85
_DIGEST_TTL_S = 60.0
_digest_memo: dict[str, tuple[float, str]] = {}

PROMPT = f"""You are a quality inspector for the digitisation of printed books.
The image is a photograph of ONE printed book page, already cropped by software.
Judge only what is clearly visible. Do not transcribe, translate, correct or rewrite the text.
Do not estimate sizes, resolution or coordinates.

Fill every field:
- page_type: "text" (mostly text), "illustrated" (drawings, charts or tables with text), "photographic" (mostly photographs), "mixed", "blank", "unknown".
- orientation_issue: "none" if the printed text is upright and reads normally; "upside_down" if the text is upside down; "rotated_90_cw" or "rotated_90_ccw" if the text lines run vertically; "uncertain" if you cannot tell.
- perspective_issue: the page looks trapezoidal or keystoned.
- crop_issue: background, scanner parts, fingers or another page visible around the page, or content cut off at an edge.
- shadow_level: dark shadows on the paper.
- lighting_issue: "uneven", "underexposed" (too dark), "overexposed" (washed out) or "none".
- blur_level: text looks soft or fuzzy. noise_level: visible grain.
- contrast_level: "low" if the text is faint or washed out, otherwise "normal" or "high".
- color_content: "grayscale", "limited_color" (a few coloured elements) or "full_color" (colour photographs or large coloured areas).
- reflection_issue: glare or bright light reflections on the paper.
- curvature_issue: text lines bend near the book spine.
- recommended_actions: only actions that fix defects you reported, chosen from {", ".join(a.value for a in ScanAction)}. Use ROTATE only if orientation_issue is not "none".
- manual_review_required: true only if a person must check this page.
- reason: one short sentence in French naming the main visible defects.
Use "none" when a defect is absent and "uncertain" only when you really cannot judge."""


def ollama_schema() -> dict:
    """Schéma JSON envoyé à Ollama (`format`) : tous les champs obligatoires, aucun champ supplémentaire."""
    schema = copy.deepcopy(VisualAnalysis.model_json_schema())
    schema["required"] = list(schema["properties"].keys())
    schema["additionalProperties"] = False
    return schema


def analysis_enabled() -> bool:
    return bool(config.OLLAMA_ENABLED and config.OLLAMA_ANALYSIS_ENABLED)


def analysis_image(rgb: np.ndarray, max_side: int | None = None) -> bytes:
    """Copie JPEG réduite envoyée au modèle (l'image de travail n'est jamais remplacée)."""
    limit = max_side or config.OLLAMA_ANALYSIS_MAX_SIDE
    image = Image.fromarray(np.ascontiguousarray(rgb))
    image.thumbnail((limit, limit), Image.Resampling.LANCZOS)
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=ANALYSIS_JPEG_QUALITY)
    return buffer.getvalue()


def _model_digest(model: str) -> str:
    now = time.monotonic()
    cached = _digest_memo.get(model)
    if cached and now - cached[0] < _DIGEST_TTL_S:
        return cached[1]
    digest = ollama_service.model_details(model).digest
    _digest_memo[model] = (now, digest)
    return digest


def cache_key(image_sha256: str, model: str, digest: str) -> str:
    raw = f"{image_sha256}|{model}|{digest}|{PROMPT_VERSION}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def _read_cached(path: Path) -> AnalysisRecord | None:
    if not path.is_file():
        return None
    try:
        return AnalysisRecord.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        logger.warning("Cache d'analyse illisible, ignoré : %s (%s)", path.name, type(error).__name__)
        return None


def _write_cached(path: Path, record: AnalysisRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(record.model_dump_json(indent=1), encoding="utf-8")
    temporary.replace(path)


def parse_response(content: str) -> VisualAnalysis:
    """Valide strictement le JSON du modèle. Lève AiResponseError (message utilisateur) sinon."""
    try:
        return VisualAnalysis.model_validate_json(content)
    except ValidationError as error:
        fields = sorted({".".join(str(part) for part in item["loc"]) or "racine" for item in error.errors()})
        raise AiResponseError("La réponse de Gemma ne respecte pas le schéma attendu.",
                              detail=", ".join(fields[:6])) from error


def analyse_page(rgb: np.ndarray, cache_dir: Path, *, force: bool = False) -> AnalysisRecord:
    """Analyse une page (copie réduite). Ne lève jamais : le statut dit ce qui s'est passé."""
    model = ollama_service.configured_model()
    if not analysis_enabled():
        return AnalysisRecord(status="disabled", model=model, prompt_version=PROMPT_VERSION,
                              error="Analyse IA désactivée (OLLAMA_ENABLED / OLLAMA_ANALYSIS_ENABLED).")
    jpeg = analysis_image(rgb)
    width, height = Image.open(io.BytesIO(jpeg)).size
    image_sha = hashlib.sha256(jpeg).hexdigest()
    base = {"model": model, "prompt_version": PROMPT_VERSION, "image_sha256": image_sha,
            "image_width": width, "image_height": height}
    try:
        digest = _model_digest(model)
    except AiUnavailableError as error:
        return AnalysisRecord(status="unavailable", error=error.user_message, **base)

    path = Path(cache_dir) / f"{cache_key(image_sha, model, digest)}.json"
    if not force:
        cached = _read_cached(path)
        if cached is not None:
            return cached

    started = time.monotonic()
    try:
        result = ollama_service.chat_json(PROMPT, jpeg, ollama_schema(), model=model)
        analysis = parse_response(result.content)
    except AiTimeoutError as error:
        return AnalysisRecord(status="timeout", model_digest=digest, error=error.user_message,
                              duration_s=round(time.monotonic() - started, 2), **base)
    except AiUnavailableError as error:
        return AnalysisRecord(status="unavailable", model_digest=digest, error=error.user_message,
                              duration_s=round(time.monotonic() - started, 2), **base)
    except AiResponseError as error:
        logger.warning("Réponse Gemma invalide (%s)", error.detail or "format")
        record = AnalysisRecord(status="invalid_response", model_digest=digest, error=error.user_message,
                                duration_s=round(time.monotonic() - started, 2), **base)
        _write_cached(path, record)    # même entrée, température 0 : inutile de refaire 1 min de calcul
        return record
    record = AnalysisRecord(status="ok", analysis=analysis, model_digest=digest,
                            duration_s=result.duration_s, attempts=result.attempts, **base)
    _write_cached(path, record)
    logger.info("Analyse Gemma en %.1f s (%d tentative(s))", result.duration_s, result.attempts)
    return record


def summary(record: AnalysisRecord) -> dict[str, str]:
    """Résumé lisible (interface, rapports) ; aucun contenu du livre."""
    if not record.usable:
        return {"statut": record.status, "message": record.error}
    analysis = record.analysis
    assert analysis is not None
    return json.loads(analysis.model_dump_json(exclude={"recommended_actions"})) | {
        "actions": ", ".join(action.value for action in analysis.recommended_actions) or "aucune"}
