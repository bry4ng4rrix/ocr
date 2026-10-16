"""PDF exports (mode A facsimile, mode B reconstruction), their validation and their report."""
from __future__ import annotations

import io
import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Literal

from PIL import Image

from core import pdf_generator, pdf_validator
from core.document_models import Page, PageStatus, Project
from core.errors import PDFGenerationError

from . import processing_service
from .storage_service import ProjectStorage, save_project

logger = logging.getLogger("livreocr.export")
Mode = Literal["reconstruction", "facsimile"]
MODE_LABELS = {"reconstruction": "PDF reconstruit (texte numérique et illustrations séparées)",
               "facsimile": "PDF fac-similé (images des pages originales)"}


@dataclass
class ExportResult:
    path: Path
    relative: str
    mode: Mode
    warnings: list[str] = field(default_factory=list)
    not_reconstructed: list[int] = field(default_factory=list)   # 1-based page numbers
    validation: dict = field(default_factory=dict)

    @property
    def data(self) -> bytes:
        return self.path.read_bytes()


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]+", "_", text).strip("_")[:60] or "livre"


def pages_needing_ocr(project: Project, page_ids: list[str] | None = None) -> list[int]:
    return [i + 1 for i, p in enumerate(project.pages)
            if (page_ids is None or p.id in page_ids) and (p.status != PageStatus.PROCESSED or p.ocr_outdated)]


def export_pdf(project: Project, mode: Mode, page_ids: list[str] | None = None, progress=None) -> ExportResult:
    """Write the PDF in the project's exports folder, read it back and store a JSON report."""
    if not project.pages:
        raise PDFGenerationError("Le projet ne contient aucune page.")
    storage = ProjectStorage(project.id).ensure()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    suffix = "reconstruit" if mode == "reconstruction" else "facsimile"
    relative = f"exports/{_slug(project.name)}_{suffix}_{stamp}.pdf"
    target = storage.path(relative)
    if progress:
        progress(0.1, "Génération du PDF")
    report = pdf_generator.generate_pdf(
        project, target, mode,
        image_provider=processing_service.illustration_provider(project),
        page_image_provider=processing_service.page_image_provider(project),
        page_ids=page_ids)
    if progress:
        progress(0.8, "Vérification du PDF")
    try:
        validation = pdf_validator.validate_pdf(target, project, mode, page_ids=page_ids)
    except Exception as exc:          # a failed check must not lose the PDF
        logger.exception("validation du PDF en échec")
        validation = {"errors": [f"Vérification impossible : {exc}"]}
    warnings = list(report.warnings)
    not_rebuilt = [p.page_index + 1 for p in report.pages if not p.reconstructed] if mode == "reconstruction" else []
    missing = pages_needing_ocr(project, page_ids)
    if mode == "reconstruction" and missing:
        warnings.insert(0, f"{len(missing)} page(s) sans OCR à jour : {', '.join(map(str, missing[:20]))}"
                           + (" …" if len(missing) > 20 else "") + ".")
    result = ExportResult(target, relative, mode, warnings, not_rebuilt, validation)
    storage.write_bytes(relative.replace(".pdf", ".json"), json.dumps({
        "mode": mode, "created": stamp, "warnings": warnings, "not_reconstructed": not_rebuilt,
        "validation": validation}, ensure_ascii=False, indent=2, default=str).encode("utf-8"))
    save_project(project)
    if progress:
        progress(1.0, "PDF prêt")
    return result


def list_exports(project: Project) -> list[dict]:
    storage = ProjectStorage(project.id)
    folder = storage.root / "exports"
    out = []
    for pdf in sorted(folder.glob("*.pdf"), reverse=True) if folder.exists() else []:
        info = {"relative": storage.relative(pdf), "name": pdf.name, "size": pdf.stat().st_size}
        meta = pdf.with_suffix(".json")
        if meta.exists():
            try:
                info.update(json.loads(meta.read_text("utf-8")))
            except ValueError:
                pass
        out.append(info)
    return out


def preview_png(project: Project, page: Page, mode: Mode = "reconstruction", width_px: int = 900):
    """(PIL image, PageReport): the page rasterised from the very PDF code used by the export."""
    return pdf_generator.render_page_png(project, page, processing_service.illustration_provider(project),
                                         mode=mode, width_px=width_px,
                                         page_image_provider=processing_service.page_image_provider(project))
