"""Page : source, corrections, résultats d'analyse et blocs."""
from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from models.blocks import BlockKind, ImageBlock, TextBlock, TextStyle, new_id
from models.errors import BlockNotFoundError
from models.geometry import Box, PageFormat, Point
from models.reports import Issue


class PageCategory(StrEnum):
    TEXT = "text"                              # cas A
    GRAPHIC_BACKGROUND = "graphic_background"  # cas B
    PHOTO_TEXT = "photo_text"                  # cas C
    TEXT_ON_IMAGE = "text_on_image"
    MIXED = "mixed"
    UNKNOWN = "unknown"


class PageStatus(StrEnum):
    IMPORTED = "imported"
    CORRECTED = "corrected"        # géométrie (et amélioration) calculées
    OCR_DONE = "ocr_done"
    ANALYSED = "analysed"          # mise en page et images extraites
    ERROR = "error"


class GeometryCorrection(BaseModel):
    """Réglages (choix de l'utilisateur) et valeurs détectées de la correction géométrique.

    Ordre d'application : EXIF -> feuille/perspective -> quart de tour auto -> quart de tour manuel
    -> inclinaison -> courbure. Les coins sont normalisés sur l'original après EXIF.
    """

    auto_crop: bool = True                     # contour de feuille + perspective
    auto_orientation: bool = True
    auto_deskew: bool = True
    auto_dewarp: bool = True                   # appliqué seulement si la mesure est fiable
    manual_quarter_turns: int = Field(default=0, ge=0, le=3)       # horaire, après l'auto
    manual_quad: list[Point] | None = None     # remplace le contour détecté (tl, tr, br, bl)
    manual_skew_deg: float | None = Field(default=None, ge=-45, le=45)   # remplace l'angle détecté

    detected_quad: list[Point] | None = None
    quad_uncertain: bool = False
    detected_quarter_turns: int | None = Field(default=None, ge=0, le=3)
    orientation_confidence: float | None = None
    orientation_method: str | None = None
    orientation_uncertain: bool = False
    detected_skew_deg: float | None = None
    skew_uncertain: bool = False
    dewarp_applied: bool = False
    detection_done: bool = False               # True quand les champs detected_* ont été calculés

    @field_validator("manual_quad", "detected_quad")
    @classmethod
    def _four_corners(cls, value: list[Point] | None) -> list[Point] | None:
        if value is not None and len(value) != 4:
            raise ValueError("un contour de page a exactement 4 coins")
        return value

    def effective_quad(self) -> list[Point] | None:
        if self.manual_quad is not None:
            return self.manual_quad
        return self.detected_quad if self.auto_crop else None

    def effective_quarter_turns(self) -> int:
        auto = (self.detected_quarter_turns or 0) if self.auto_orientation else 0
        return (auto + self.manual_quarter_turns) % 4

    def effective_skew(self) -> float:
        if self.manual_skew_deg is not None:
            return self.manual_skew_deg
        return (self.detected_skew_deg or 0.0) if self.auto_deskew else 0.0

    def signature(self) -> str:
        """Empreinte des valeurs effectivement appliquées : change => coordonnées OCR obsolètes."""
        quad = self.effective_quad()
        payload = {
            "quad": [(round(p.x, 4), round(p.y, 4)) for p in quad] if quad else None,
            "turns": self.effective_quarter_turns(),
            "skew": round(self.effective_skew(), 3),
            "dewarp": self.dewarp_applied,
        }
        return hashlib.sha1(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]


class ImageEnhancement(BaseModel):
    """Réglages photographiques non destructifs. Valeurs neutres = image inchangée."""

    enabled: bool = True
    white_balance: Literal["none", "auto"] = "none"
    brightness: float = Field(default=0.0, ge=-100, le=100)
    contrast: float = Field(default=0.0, ge=-100, le=100)
    saturation: float = Field(default=0.0, ge=-100, le=100)
    sharpness: float = Field(default=0.0, ge=0, le=100)
    denoise: float = Field(default=0.0, ge=0, le=100)
    shadow_correction: float = Field(default=0.0, ge=0, le=100)
    readability: float = Field(default=0.0, ge=0, le=100)    # contraste local (CLAHE) pour la lisibilité

    def is_neutral(self) -> bool:
        if not self.enabled:
            return True
        return self == ImageEnhancement(enabled=True)

    def signature(self) -> str:
        return hashlib.sha1(self.model_dump_json().encode()).hexdigest()[:16]


class Page(BaseModel):
    id: str = Field(default_factory=new_id)
    source_file: str                           # relatif au projet : originals/<id>.<ext>
    source_index: int = 0                      # page dans un PDF ou un TIFF multi-pages
    source_sha256: str
    original_name: str
    source_width: int = 0
    source_height: int = 0

    geometry: GeometryCorrection = Field(default_factory=GeometryCorrection)
    enhancement: ImageEnhancement = Field(default_factory=ImageEnhancement)
    corrected_width: int | None = None
    corrected_height: int | None = None

    status: PageStatus = PageStatus.IMPORTED
    error: str | None = None
    issues: list[Issue] = Field(default_factory=list)

    category: PageCategory = PageCategory.UNKNOWN
    category_override: PageCategory | None = None
    detected_format: PageFormat | None = None
    format_override: PageFormat | None = None
    body_font_size_pt: float | None = None
    columns: int = 1
    content_box: Box | None = None             # zone utile (marges = complément)
    printed_page_number: str | None = None

    text_blocks: list[TextBlock] = Field(default_factory=list)
    image_blocks: list[ImageBlock] = Field(default_factory=list)
    background_regions: list[Box] = Field(default_factory=list)
    background_files: list[str] = Field(default_factory=list)   # backgrounds/<page>_<n>.png, même ordre

    style_rules: dict[BlockKind, TextStyle] = Field(default_factory=dict)   # typographie appliquée à cette page

    ocr_signature: str | None = None           # GeometryCorrection.signature() au moment de l'OCR
    ocr_languages: list[str] = Field(default_factory=list)
    mean_confidence: float | None = None
    review_required: bool = False              # routage E : à valider à la main
    route_band: str | None = None              # A..E (voir models.quality.RouteBand)

    @property
    def effective_category(self) -> PageCategory:
        return self.category_override or self.category

    @property
    def ocr_outdated(self) -> bool:
        return self.ocr_signature is not None and self.ocr_signature != self.geometry.signature()

    def text_block(self, block_id: str) -> TextBlock:
        for block in self.text_blocks:
            if block.id == block_id:
                return block
        raise BlockNotFoundError("Bloc de texte introuvable.", detail=block_id)

    def image_block(self, block_id: str) -> ImageBlock:
        for block in self.image_blocks:
            if block.id == block_id:
                return block
        raise BlockNotFoundError("Image introuvable.", detail=block_id)

    def blocks_in_order(self) -> list[TextBlock | ImageBlock]:
        blocks: list[TextBlock | ImageBlock] = [*self.text_blocks, *self.image_blocks]
        return sorted(blocks, key=lambda block: block.order)
