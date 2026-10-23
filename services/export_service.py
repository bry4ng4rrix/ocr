"""Exports du livre : composition -> DOCX / PDF -> relecture et validation.

Façade utilisée par l'interface : aucun export n'est déclaré réussi sans relecture du fichier produit.
Un échec d'export ne touche jamais au projet (les fichiers sont écrits dans exports/, de façon atomique).
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from PIL import Image

from models import (ComposedPage, ConversionResult, ExportReport, Issue, Page, Project, ReconstructionMode,
                    Severity, ValidationReport)
from models.errors import ExportError
from services import composition_service, document_service, pdf_service, project_service

logger = logging.getLogger("bookocr.export")


@dataclass
class ExportOutcome:
    path: Path | None
    report: ExportReport | None
    validation: ValidationReport | None = None
    docx_inspection: object | None = None          # document_service.DocxInspection
    conversion: ConversionResult | None = None
    issues: list[Issue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        reports_ok = (self.report is None or self.report.ok) and (self.validation is None or self.validation.ok)
        return self.path is not None and reports_ok and not any(i.severity == Severity.ERROR for i in self.issues)


def exports_dir(project: Project) -> Path:
    folder = project_service.project_dir(project) / "exports"
    folder.mkdir(parents=True, exist_ok=True)
    return folder


def export_path(project: Project, suffix: str, label: str) -> Path:
    """Chemin horodaté d'un nouvel export dans exports/ (nom du livre assaini)."""
    return _filename(project, suffix, label)


def _filename(project: Project, suffix: str, label: str) -> Path:
    safe = re.sub(r"[^\w\-]+", "_", project.name, flags=re.UNICODE).strip("_") or "livre"
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    folder = exports_dir(project)
    path = folder / f"{safe}_{label}_{stamp}{suffix}"
    counter = 2
    while path.exists():                       # deux exports dans la même seconde ne s'écrasent pas
        path = folder / f"{safe}_{label}_{stamp}-{counter}{suffix}"
        counter += 1
    return path


def compose(project: Project, mode: ReconstructionMode | None = None,
            page_ids: list[str] | None = None) -> list[ComposedPage]:
    pages = composition_service.compose_book(project, project_service.project_dir(project), mode=mode,
                                             page_ids=page_ids)
    if not pages:
        raise ExportError("Aucune page à exporter.")
    return pages


def preview_page(project: Project, page: Page, mode: ReconstructionMode | None = None,
                 width_px: int = 900) -> tuple[Image.Image, ComposedPage, list[Issue]]:
    """Aperçu = rendu PDF rasterisé de la composition (ce qui est vu est ce qui est exporté en PDF)."""
    composed = composition_service.compose_page(project, page, project_service.project_dir(project), mode=mode)
    image, issues = pdf_service.render_preview(composed, width_px=width_px)
    return image, composed, [*composed.issues, *issues]


def export_docx(project: Project, mode: ReconstructionMode | None = None,
                page_ids: list[str] | None = None) -> ExportOutcome:
    mode = mode or project.export.mode
    pages = compose(project, mode, page_ids)
    output = _filename(project, ".docx", mode.value)
    report = document_service.build_docx(pages, project, output)
    inspection = document_service.inspect_docx(output)
    issues = _check_docx(inspection, pages)
    logger.info("DOCX exporté : %s (%d pages)", output, len(pages))
    return ExportOutcome(output, report, docx_inspection=inspection, issues=issues)


def _check_docx(inspection, pages: list[ComposedPage]) -> list[Issue]:
    issues = []
    # les sections continues de colonnes ne comptent pas : une section « de page » par page source
    if inspection.page_sections != len(pages):
        issues.append(Issue(code="docx_section_count", severity=Severity.ERROR,
                            message=f"Le DOCX contient {inspection.page_sections} pages pour {len(pages)} attendues."))
    expected_text = sum(len(page.text_elements) for page in pages)
    if expected_text and inspection.non_empty_paragraphs == 0 and inspection.frames == 0:
        issues.append(Issue(code="docx_no_text", severity=Severity.ERROR,
                            message="Le DOCX ne contient aucun texte modifiable."))
    expected_images = sum(len(page.image_elements) + len(page.backgrounds) for page in pages)
    found_images = inspection.inline_images + inspection.anchored_images
    if found_images < expected_images:
        issues.append(Issue(code="docx_missing_images", severity=Severity.ERROR,
                            message=f"{expected_images - found_images} image(s) manquante(s) dans le DOCX."))
    return issues


def export_pdf(project: Project, mode: ReconstructionMode | None = None, page_ids: list[str] | None = None,
               searchable_facsimile: bool = False) -> ExportOutcome:
    mode = mode or project.export.mode
    pages = compose(project, mode, page_ids)
    label = "facsimile" if searchable_facsimile else mode.value
    output = _filename(project, ".pdf", label)
    report = pdf_service.render_pdf(pages, output, searchable_facsimile=searchable_facsimile,
                                    overflow_policy=project.export.overflow_policy)
    validation = pdf_service.validate_pdf(output, None if searchable_facsimile else pages)
    return ExportOutcome(output, report, validation=validation)


def export_pdf_via_docx(project: Project, mode: ReconstructionMode | None = None,
                        page_ids: list[str] | None = None) -> ExportOutcome:
    """DOCX puis conversion LibreOffice ; le PDF obtenu est relu et comparé aux pages attendues."""
    docx = export_docx(project, mode, page_ids)
    conversion = pdf_service.convert_docx_to_pdf(docx.path, exports_dir(project))
    if not conversion.available or not conversion.output:
        return ExportOutcome(None, docx.report, conversion=conversion, issues=[Issue(
            code="libreoffice_unavailable", severity=Severity.ERROR,
            message=conversion.message or "LibreOffice est absent : conversion DOCX -> PDF impossible.")])
    pages = compose(project, mode or project.export.mode, page_ids)
    validation = pdf_service.validate_pdf(Path(conversion.output), pages)
    return ExportOutcome(Path(conversion.output), docx.report, validation=validation, conversion=conversion,
                         docx_inspection=docx.docx_inspection, issues=docx.issues)


def list_exports(project: Project) -> list[Path]:
    return sorted((p for p in exports_dir(project).iterdir() if p.suffix in {".pdf", ".docx"}),
                  key=lambda p: p.stat().st_mtime, reverse=True)
