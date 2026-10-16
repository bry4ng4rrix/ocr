"""Résultat brut d'OCR (indépendant du moteur)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from models.geometry import Box


class OcrWord(BaseModel):
    text: str
    box: Box                         # normalisée sur l'image passée à l'OCR (= image corrigée)
    confidence: float = Field(ge=-1, le=100)   # -1 = inconnue
    block_num: int = 0
    par_num: int = 0
    line_num: int = 0
    word_num: int = 0


class OcrResult(BaseModel):
    words: list[OcrWord] = Field(default_factory=list)
    width: int                       # taille en pixels de l'image lue
    height: int
    languages: list[str] = Field(default_factory=list)
    engine: str = "tesseract"
    engine_version: str = ""
    psm: int = 3
    preparation: str = "gray"        # "gray" ou "binarized" (seconde lecture)

    @property
    def mean_confidence(self) -> float:
        known = [word.confidence for word in self.words if word.confidence >= 0]
        return sum(known) / len(known) if known else 0.0

    @property
    def text(self) -> str:
        return " ".join(word.text for word in self.words)
