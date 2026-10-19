"""Contrôles de qualité et décision de routage (mesurables, sans appel IA)."""
from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from models.reports import Issue


class RouteBand(StrEnum):
    """Politique de routage (prompt maître, phase 8). Les lettres sont stables."""

    RELIABLE = "A"            # OCR local suffisant
    DOUBTFUL = "B"            # second prétraitement ou OCR de secours
    COMPLEX = "C"             # candidat Ollama si activé
    CLOUD_CANDIDATE = "D"     # candidat Gemini si activé et autorisé
    REVIEW_REQUIRED = "E"     # validation humaine obligatoire


class PageQuality(BaseModel):
    page_id: str
    band: RouteBand
    reasons: list[str] = Field(default_factory=list)
    mean_confidence: float | None = None
    empty_text: bool = False
    low_confidence_blocks: int = 0
    missing_illustration_files: int = 0
    error_issues: int = 0
    ocr_outdated: bool = False
    issues: list[Issue] = Field(default_factory=list)


class BookQuality(BaseModel):
    page_count: int
    expected_page_count: int | None = None
    analysed_count: int = 0
    error_count: int = 0
    review_count: int = 0
    pages: list[PageQuality] = Field(default_factory=list)
    issues: list[Issue] = Field(default_factory=list)


class ImageQualityMetrics(BaseModel):
    sharpness_score: float
    mean_luminance: float
    contrast: float
    glare_ratio: float
    is_blurry: bool
    is_underexposed: bool
    is_overexposed: bool
    overall_score: float


class VisualComparisonResult(BaseModel):
    ssim_score: float
    text_coverage_ratio: float
    is_faithful: bool
    issues: list[Issue] = Field(default_factory=list)

