"""Blocs d'une page : texte et images."""
from __future__ import annotations

import uuid
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator

from models.geometry import Box
from models.ocr import OcrWord


def new_id() -> str:
    return uuid.uuid4().hex[:12]


class BlockKind(StrEnum):
    HEADING_1 = "heading_1"
    HEADING_2 = "heading_2"
    PARAGRAPH = "paragraph"
    LIST_ITEM = "list_item"
    CAPTION = "caption"
    HEADER = "header"
    FOOTER = "footer"
    PAGE_NUMBER = "page_number"
    NOTE = "note"
    TABLE = "table"
    SPECIAL = "special"            # zone de texte spéciale (encadré, citation, texte décoratif…)


HEADING_KINDS = {BlockKind.HEADING_1, BlockKind.HEADING_2}
MARGINAL_KINDS = {BlockKind.HEADER, BlockKind.FOOTER, BlockKind.PAGE_NUMBER}


class Alignment(StrEnum):
    LEFT = "left"
    CENTER = "center"
    RIGHT = "right"
    JUSTIFY = "justify"


class TextStyle(BaseModel):
    """Style partiel : un champ `None` signifie « non défini à ce niveau » (hérité)."""

    font_family: str | None = None
    font_size_pt: float | None = Field(default=None, gt=0, le=400)
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    color: str | None = None                   # "#RRGGBB"
    alignment: Alignment | None = None
    line_spacing: float | None = Field(default=None, gt=0, le=5)    # multiple de l'interligne simple
    space_before_pt: float | None = Field(default=None, ge=0, le=500)
    space_after_pt: float | None = Field(default=None, ge=0, le=500)
    first_line_indent_pt: float | None = Field(default=None, ge=-200, le=500)
    left_indent_pt: float | None = Field(default=None, ge=0, le=1000)
    right_indent_pt: float | None = Field(default=None, ge=0, le=1000)

    @field_validator("color")
    @classmethod
    def _hex_color(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.strip()
        if len(value) != 7 or not value.startswith("#"):
            raise ValueError("couleur attendue au format #RRGGBB")
        int(value[1:], 16)
        return value.upper()

    def defined_fields(self) -> set[str]:
        return {name for name, value in self if value is not None}

    def merged_with(self, higher: "TextStyle") -> "TextStyle":
        """Applique `higher` par-dessus `self` (les champs définis de `higher` gagnent)."""
        data = self.model_dump()
        data.update({name: value for name, value in higher if value is not None})
        return TextStyle(**data)

    def without(self, fields: set[str]) -> "TextStyle":
        data = self.model_dump()
        for name in fields:
            data[name] = None
        return TextStyle(**data)


class OcrLine(BaseModel):
    text: str
    box: Box
    confidence: float = -1
    words: list[OcrWord] = Field(default_factory=list)


class TextBlock(BaseModel):
    id: str = Field(default_factory=new_id)
    kind: BlockKind = BlockKind.PARAGRAPH
    box: Box
    ocr_text: str = ""
    corrected_text: str | None = None          # None = jamais corrigé ; le texte OCR reste consultable
    lines: list[OcrLine] = Field(default_factory=list)
    confidence: float = -1                     # moyenne des mots, 0..100 ; -1 inconnu
    needs_review: bool = False
    language: str | None = None
    estimated_style: TextStyle = Field(default_factory=TextStyle)   # estimé par l'analyse
    style_override: TextStyle = Field(default_factory=TextStyle)    # choix manuels de l'utilisateur
    on_image: bool = False                     # posé sur une image ou un fond graphique
    user_created: bool = False
    user_modified: bool = False                # texte, type, boîte ou style modifié à la main
    hidden: bool = False                       # exclu des exports
    order: int = 0                             # rang dans l'ordre de lecture

    @property
    def text(self) -> str:
        return self.corrected_text if self.corrected_text is not None else self.ocr_text

    def low_confidence_words(self, threshold: float) -> list[OcrWord]:
        return [word for line in self.lines for word in line.words if 0 <= word.confidence < threshold]


class ImageKind(StrEnum):
    PHOTO = "photo"
    ILLUSTRATION = "illustration"
    DRAWING = "drawing"
    TABLE_IMAGE = "table_image"


class ImageBlock(BaseModel):
    id: str = Field(default_factory=new_id)
    kind: ImageKind = ImageKind.PHOTO
    box: Box
    file: str = ""                             # relatif au dossier du projet (images/<id>.png) ; "" = pas encore extrait
    raw_file: str = ""                         # découpe brute si `file` a eu du texte effacé
    caption_block_id: str | None = None
    confidence: float = 1.0                    # 0..1 (certitude de la détection)
    uncertain: bool = False
    user_created: bool = False
    user_modified: bool = False
    hidden: bool = False
    order: int = 0
