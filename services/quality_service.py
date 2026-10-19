"""Contrôles de qualité mesurables sur une page ou un livre (sans appel IA, sans une seule métrique globale)."""
from __future__ import annotations

from pathlib import Path

import config
from models import BookQuality, Issue, OcrResult, Page, PageStatus, Project, Severity
from models.quality import PageQuality, RouteBand


def _missing_illustrations(page: Page, project_dir: Path | None) -> int:
    if project_dir is None:
        return 0
    missing = 0
    for block in page.image_blocks:
        if block.hidden or not block.file:
            continue
        if not (project_dir / block.file).is_file():
            missing += 1
    return missing


def evaluate_page(page: Page, *, project_dir: Path | None = None, ocr: OcrResult | None = None) -> PageQuality:
    """Évalue une page déjà analysée (ou en erreur) et choisit une bande A–E."""
    mean = page.mean_confidence if page.mean_confidence is not None else (
        ocr.mean_confidence if ocr is not None else None)
    empty = bool(ocr is not None and not ocr.words) or (
        page.status in {PageStatus.OCR_DONE, PageStatus.ANALYSED} and not any(
            not block.hidden and (block.text or "").strip() for block in page.text_blocks))
    low_blocks = sum(1 for block in page.text_blocks if not block.hidden and block.needs_review)
    missing = _missing_illustrations(page, project_dir)
    error_issues = sum(1 for issue in page.issues if issue.severity == Severity.ERROR)
    reasons: list[str] = []
    issues: list[Issue] = []

    if page.status == PageStatus.ERROR:
        reasons.append(page.error or "La page est en erreur.")
        band = RouteBand.REVIEW_REQUIRED
    elif page.status in {PageStatus.IMPORTED, PageStatus.CORRECTED}:
        reasons.append("La page n'a pas encore été reconnue.")
        band = RouteBand.DOUBTFUL
    elif empty:
        reasons.append("Aucun texte exploitable n'a été reconnu.")
        band = RouteBand.REVIEW_REQUIRED
        issues.append(Issue(code="quality_empty_text", message=reasons[-1], page_id=page.id,
                            severity=Severity.ERROR))
    elif missing:
        reasons.append(f"{missing} illustration(s) extraite(s) introuvable(s) sur le disque.")
        band = RouteBand.REVIEW_REQUIRED
        issues.append(Issue(code="quality_missing_image", message=reasons[-1], page_id=page.id,
                            severity=Severity.ERROR))
    elif page.ocr_outdated:
        reasons.append("La géométrie a changé après l'OCR : relance nécessaire.")
        band = RouteBand.DOUBTFUL
    elif mean is not None and mean < config.ROUTE_CONFIDENCE_DOUBTFUL:
        reasons.append(f"Confiance OCR basse ({mean:.0f} %).")
        band = RouteBand.COMPLEX
    elif low_blocks or (mean is not None and mean < config.ROUTE_CONFIDENCE_RELIABLE):
        if low_blocks:
            reasons.append(f"{low_blocks} bloc(s) à vérifier.")
        if mean is not None and mean < config.ROUTE_CONFIDENCE_RELIABLE:
            reasons.append(f"Confiance OCR intermédiaire ({mean:.0f} %).")
        band = RouteBand.DOUBTFUL
    elif error_issues:
        reasons.append("Des erreurs sont encore signalées sur la page.")
        band = RouteBand.DOUBTFUL
    else:
        reasons.append("Contrôles locaux acceptables.")
        band = RouteBand.RELIABLE

    return PageQuality(
        page_id=page.id, band=band, reasons=reasons, mean_confidence=mean, empty_text=empty,
        low_confidence_blocks=low_blocks, missing_illustration_files=missing, error_issues=error_issues,
        ocr_outdated=page.ocr_outdated, issues=issues,
    )


def evaluate_book(project: Project, *, project_dir: Path | None = None) -> BookQuality:
    pages = [evaluate_page(page, project_dir=project_dir) for page in project.pages]
    issues: list[Issue] = []
    if project.expected_page_count is not None and len(project.pages) != project.expected_page_count:
        issues.append(Issue(
            code="quality_page_count",
            severity=Severity.WARNING,
            message=(f"Nombre de pages importées ({len(project.pages)}) différent du nombre attendu "
                     f"({project.expected_page_count})."),
        ))
    return BookQuality(
        page_count=len(project.pages),
        expected_page_count=project.expected_page_count,
        analysed_count=sum(page.status == PageStatus.ANALYSED for page in project.pages),
        error_count=sum(page.status == PageStatus.ERROR for page in project.pages),
        review_count=sum(item.band == RouteBand.REVIEW_REQUIRED for item in pages),
        pages=pages,
        issues=issues,
    )
