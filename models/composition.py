"""Page composée en points : source unique de l'aperçu, du DOCX et du PDF.

Repère : points typographiques (1/72 po), origine en HAUT à gauche de la page (y vers le bas).
PyMuPDF utilise le même sens ; python-docx reçoit des distances depuis le haut et la gauche.
"""
from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

from models.blocks import Alignment, BlockKind
from models.page import PageCategory
from models.project import ReconstructionMode
from models.reports import Issue


class Rect(BaseModel):
    x_pt: float
    y_pt: float
    width_pt: float = Field(gt=0)
    height_pt: float = Field(gt=0)

    @property
    def right_pt(self) -> float:
        return self.x_pt + self.width_pt

    @property
    def bottom_pt(self) -> float:
        return self.y_pt + self.height_pt


class Margins(BaseModel):
    top_pt: float = 0.0
    right_pt: float = 0.0
    bottom_pt: float = 0.0
    left_pt: float = 0.0


class ResolvedStyle(BaseModel):
    """Style complet (aucun champ hérité) après résolution par typography_service."""

    font_family: str
    font_size_pt: float
    bold: bool = False
    italic: bool = False
    underline: bool = False
    color: str = "#000000"
    alignment: Alignment = Alignment.LEFT
    line_spacing: float = 1.15
    space_before_pt: float = 0.0
    space_after_pt: float = 0.0
    first_line_indent_pt: float = 0.0
    left_indent_pt: float = 0.0
    right_indent_pt: float = 0.0


# flow     : paragraphe dans le flux (DOCX) ; `rect` = position d'origine (PDF, aperçu)
# absolute : positionné sur la page (cadre DOCX, image ancrée)
# header / footer : placé dans l'en-tête ou le pied de la section DOCX
Positioning = Literal["flow", "absolute", "header", "footer"]


class TextElement(BaseModel):
    element_type: Literal["text"] = "text"
    block_id: str
    kind: BlockKind
    text: str
    rect: Rect
    style: ResolvedStyle
    positioning: Positioning = "absolute"
    band_index: int | None = None          # bande de colonnes (flux), voir ComposedPage.bands
    column_index: int | None = None        # colonne 0.. dans la bande
    gap_before_pt: float = 0.0             # flux : espace vertical d'origine avec l'élément précédent de la colonne
    on_image: bool = False
    z_index: int = 10


class ImageElement(BaseModel):
    element_type: Literal["image"] = "image"
    block_id: str | None = None            # None pour un fond
    kind: Literal["photo", "illustration", "drawing", "table_image", "background"]
    file: str                              # chemin ABSOLU du PNG à insérer
    rect: Rect
    positioning: Positioning = "absolute"
    band_index: int | None = None
    column_index: int | None = None
    gap_before_pt: float = 0.0
    caption_block_id: str | None = None
    z_index: int = 5                       # fonds : 0, images : 5, texte : 10


PageElement = Annotated[Union[TextElement, ImageElement], Field(discriminator="element_type")]


class ColumnBand(BaseModel):
    """Bande horizontale de la page ayant un nombre de colonnes donné (sections DOCX continues)."""

    index: int
    top_pt: float
    bottom_pt: float
    columns: int = 1
    gap_pt: float = 0.0
    column_rects: list[Rect] = Field(default_factory=list)


class ComposedPage(BaseModel):
    page_id: str
    page_number: int                       # 1.. dans le livre
    width_pt: float
    height_pt: float
    margins: Margins = Field(default_factory=Margins)
    mode: ReconstructionMode
    category: PageCategory = PageCategory.UNKNOWN
    backgrounds: list[ImageElement] = Field(default_factory=list)   # derrière tout le reste
    elements: list[PageElement] = Field(default_factory=list)       # ordre de lecture
    bands: list[ColumnBand] = Field(default_factory=list)
    source_image: str | None = None        # image corrigée (chemin absolu), pour le fac-similé et la comparaison
    analysed: bool = True                  # False : page non analysée -> page vide signalée, jamais la photo
    issues: list[Issue] = Field(default_factory=list)

    @property
    def text_elements(self) -> list[TextElement]:
        return [element for element in self.elements if isinstance(element, TextElement)]

    @property
    def image_elements(self) -> list[ImageElement]:
        return [element for element in self.elements if isinstance(element, ImageElement)]
