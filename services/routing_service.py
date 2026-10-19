"""Routage A–E à partir des mesures de qualité, pas d'une décision arbitraire d'un modèle."""
from __future__ import annotations

from pathlib import Path

import config
from models import Page, Project
from models.quality import PageQuality, RouteBand
from services import quality_service


def apply_route(page: Page, quality: PageQuality) -> None:
    """Écrit la bande et le drapeau de relecture sur la page (persistance via save_project)."""
    page.route_band = quality.band.value
    page.review_required = quality.band == RouteBand.REVIEW_REQUIRED


def route_page(project: Project, page: Page, project_dir: Path | None = None) -> PageQuality:
    """Décide A/B/C/D/E. N'appelle jamais Ollama ni Gemini : le pipeline local reste autonome."""
    quality = quality_service.evaluate_page(page, project_dir=project_dir)
    if quality.band != RouteBand.COMPLEX:
        apply_route(page, quality)
        return quality

    ollama_ok = config.OLLAMA_ENABLED and project.ollama_allowed
    gemini_ok = config.GEMINI_ENABLED and project.cloud_ai_allowed
    reasons = list(quality.reasons)
    band = RouteBand.COMPLEX
    if not ollama_ok and gemini_ok:
        band = RouteBand.CLOUD_CANDIDATE
        reasons.append("Ollama non autorisé : page candidate pour Gemini (non appelée automatiquement).")
    elif not ollama_ok and not gemini_ok:
        band = RouteBand.REVIEW_REQUIRED
        reasons.append("Aucun secours IA n'est autorisé : relecture humaine.")
    else:
        reasons.append("Candidate pour une analyse Ollama (non appelée automatiquement).")
    quality = quality.model_copy(update={"band": band, "reasons": reasons})
    apply_route(page, quality)
    return quality
