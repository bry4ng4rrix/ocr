"""Résultats intermédiaires des détections (orientation, contour, mise en page)."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from models.blocks import ImageBlock, ImageKind, TextBlock
from models.geometry import Box, PageFormat, Point
from models.page import PageCategory
from models.reports import Issue


class OrientationResult(BaseModel):
    quarter_turns: int = Field(ge=0, le=3)    # rotation HORAIRE à appliquer pour redresser
    confidence: float = 0.0
    method: Literal["osd", "reading", "none"] = "none"
    uncertain: bool = True
    details: str = ""


class QuadResult(BaseModel):
    corners: list[Point]                      # tl, tr, br, bl normalisés
    score: float = 0.0                        # 0..1
    uncertain: bool = False


class SkewResult(BaseModel):
    angle_deg: float = 0.0                    # sens antihoraire positif ; rotation à appliquer = -angle
    confidence: float = 0.0
    uncertain: bool = True


class CurvatureResult(BaseModel):
    applicable: bool = False
    reason: str = ""
    lines_used: int = 0
    max_offset_px: float = 0.0


class DetectedRegion(BaseModel):
    box: Box
    kind: ImageKind
    confidence: float = 1.0
    uncertain: bool = False
    reason: str = ""


class LayoutSettings(BaseModel):
    languages: list[str] = Field(default_factory=list)
    low_confidence: float = 60.0


class LayoutResult(BaseModel):
    text_blocks: list[TextBlock] = Field(default_factory=list)
    image_blocks: list[ImageBlock] = Field(default_factory=list)   # file == "" : la découpe est faite par processing_service
    category: PageCategory = PageCategory.UNKNOWN
    columns: int = 1
    content_box: Box | None = None
    printed_page_number: str | None = None
    background_regions: list[Box] = Field(default_factory=list)
    detected_format: PageFormat | None = None
    body_font_size_pt: float | None = None
    mean_confidence: float = 0.0
    issues: list[Issue] = Field(default_factory=list)
