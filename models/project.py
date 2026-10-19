"""Projet : pages ordonnées et réglages globaux."""
from __future__ import annotations

from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

from models.blocks import Alignment, BlockKind, TextStyle, new_id
from models.errors import PageNotFoundError
from models.geometry import PageFormat
from models.page import Page

SCHEMA_VERSION = 1


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ReconstructionMode(StrEnum):
    FIDELITY = "fidelity"      # positions absolues, fonds, apparence
    EDITABLE = "editable"      # flux de paragraphes stylés, facilité d'édition
    HYBRID = "hybrid"          # fonds et images positionnés, texte courant en flux


MODE_LABELS = {
    ReconstructionMode.FIDELITY: "Fidélité à l'original",
    ReconstructionMode.EDITABLE: "Document modifiable",
    ReconstructionMode.HYBRID: "Mode hybride",
}


class TypographySettings(BaseModel):
    """Typographie du livre. Ordre de résolution d'un style de bloc (du plus faible au plus fort) :
    valeurs par défaut -> estimated_style du bloc -> kind_styles (livre) -> Page.style_rules -> style_override du bloc.
    """

    document_font: str = "Liberation Serif"
    kind_styles: dict[BlockKind, TextStyle] = Field(default_factory=dict)
    default_line_spacing: float = 1.15
    default_alignment: Alignment = Alignment.LEFT
    default_color: str = "#000000"


class ExportSettings(BaseModel):
    mode: ReconstructionMode = ReconstructionMode.EDITABLE
    page_format: Literal["detected", "A4", "A5", "custom"] = "detected"
    custom_width_mm: float = Field(default=148.0, gt=30, le=1000)
    custom_height_mm: float = Field(default=210.0, gt=30, le=1000)
    scale_fonts_to_format: bool = True        # tailles de police proportionnelles si le format change
    keep_backgrounds: bool = True             # fonds graphiques (cas B) conservés dans tous les modes
    include_headers_footers: bool = True
    include_page_numbers: bool = True
    overflow_policy: Literal["shrink", "report"] = "shrink"   # PDF : réduire la police (≥ 70 %) ou seulement signaler
    margins_override_mm: tuple[float, float, float, float] | None = None   # haut, droite, bas, gauche


class Project(BaseModel):
    schema_version: int = SCHEMA_VERSION
    id: str = Field(default_factory=new_id)
    name: str
    author: str | None = None
    expected_page_count: int | None = Field(default=None, ge=1, le=100_000)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    languages: list[str] = Field(default_factory=lambda: ["fra", "eng"])
    ocr_psm: int = 3
    background_method: Literal["inpaint", "fill"] = "inpaint"
    autosave_seconds: int = Field(default=30, ge=0, le=3600)   # 0 = désactivée
    cloud_ai_allowed: bool = False          # Gemini : interdit sauf consentement explicite
    ollama_allowed: bool = False            # analyse locale optionnelle, jamais obligatoire
    pages: list[Page] = Field(default_factory=list)
    typography: TypographySettings = Field(default_factory=TypographySettings)
    export: ExportSettings = Field(default_factory=ExportSettings)

    def page(self, page_id: str) -> Page:
        for page in self.pages:
            if page.id == page_id:
                return page
        raise PageNotFoundError("Page introuvable dans le projet.", detail=page_id)

    def page_number(self, page_id: str) -> int:
        """Numéro 1.. de la page dans l'ordre du livre."""
        for index, page in enumerate(self.pages):
            if page.id == page_id:
                return index + 1
        raise PageNotFoundError("Page introuvable dans le projet.", detail=page_id)

    def target_format(self, page: Page) -> PageFormat:
        """Format de sortie de `page` selon les réglages d'export."""
        from models.geometry import STANDARD_FORMATS

        choice = self.export.page_format
        if choice in ("A4", "A5"):
            return STANDARD_FORMATS[choice]
        if choice == "custom":
            return PageFormat(name="Personnalisé", width_mm=self.export.custom_width_mm,
                              height_mm=self.export.custom_height_mm)
        return page.format_override or page.detected_format or STANDARD_FORMATS["A5"]


class ProjectSummary(BaseModel):
    id: str
    name: str
    updated_at: datetime
    page_count: int
    analysed_count: int
    error_count: int
