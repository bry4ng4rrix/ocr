"""Géométrie : boîtes normalisées (0..1, origine en haut à gauche de l'image corrigée), formats de page."""
from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

MM_TO_PT = 72.0 / 25.4
_EPS = 1e-6


class Point(BaseModel):
    """Point normalisé 0..1."""

    model_config = ConfigDict(frozen=True)

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)


class Box(BaseModel):
    """Rectangle normalisé par rapport à l'image corrigée de la page (largeur et hauteur = 1)."""

    model_config = ConfigDict(frozen=True)

    x: float = Field(ge=0.0, le=1.0)
    y: float = Field(ge=0.0, le=1.0)
    w: float = Field(gt=0.0, le=1.0)
    h: float = Field(gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _inside_page(self) -> "Box":
        if self.x + self.w > 1.0 + _EPS or self.y + self.h > 1.0 + _EPS:
            raise ValueError(f"boîte hors de la page : {self}")
        return self

    @property
    def right(self) -> float:
        return self.x + self.w

    @property
    def bottom(self) -> float:
        return self.y + self.h

    @property
    def center(self) -> tuple[float, float]:
        return self.x + self.w / 2, self.y + self.h / 2

    @property
    def area(self) -> float:
        return self.w * self.h

    @classmethod
    def from_edges(cls, left: float, top: float, right: float, bottom: float) -> "Box":
        """Construit une boîte à partir de bords normalisés, bornés à la page."""
        left, top = max(0.0, min(1.0, left)), max(0.0, min(1.0, top))
        right, bottom = max(0.0, min(1.0, right)), max(0.0, min(1.0, bottom))
        width = max(right - left, 1e-4)
        height = max(bottom - top, 1e-4)
        left = min(left, 1.0 - width)
        top = min(top, 1.0 - height)
        return cls(x=left, y=top, w=width, h=height)

    @classmethod
    def from_pixels(cls, left: int | float, top: int | float, right: int | float, bottom: int | float,
                    width: int, height: int) -> "Box":
        return cls.from_edges(left / width, top / height, right / width, bottom / height)

    def to_pixels(self, width: int, height: int) -> tuple[int, int, int, int]:
        """(gauche, haut, droite, bas) en pixels entiers, bornés à l'image."""
        left = max(0, min(width - 1, int(round(self.x * width))))
        top = max(0, min(height - 1, int(round(self.y * height))))
        right = max(left + 1, min(width, int(round(self.right * width))))
        bottom = max(top + 1, min(height, int(round(self.bottom * height))))
        return left, top, right, bottom

    def intersection_area(self, other: "Box") -> float:
        width = min(self.right, other.right) - max(self.x, other.x)
        height = min(self.bottom, other.bottom) - max(self.y, other.y)
        return max(0.0, width) * max(0.0, height)

    def iou(self, other: "Box") -> float:
        inter = self.intersection_area(other)
        union = self.area + other.area - inter
        return inter / union if union > 0 else 0.0

    def fraction_inside(self, other: "Box") -> float:
        """Part de `self` (0..1) située dans `other`."""
        return self.intersection_area(other) / self.area if self.area > 0 else 0.0

    def union(self, other: "Box") -> "Box":
        return Box.from_edges(min(self.x, other.x), min(self.y, other.y),
                              max(self.right, other.right), max(self.bottom, other.bottom))

    def expanded(self, margin: float) -> "Box":
        return Box.from_edges(self.x - margin, self.y - margin, self.right + margin, self.bottom + margin)


class PageFormat(BaseModel):
    """Format physique d'une page."""

    model_config = ConfigDict(frozen=True)

    name: str
    width_mm: float = Field(gt=10, le=2000)
    height_mm: float = Field(gt=10, le=2000)

    @property
    def width_pt(self) -> float:
        return self.width_mm * MM_TO_PT

    @property
    def height_pt(self) -> float:
        return self.height_mm * MM_TO_PT

    @property
    def aspect(self) -> float:
        return self.width_mm / self.height_mm


STANDARD_FORMATS: dict[str, PageFormat] = {
    fmt.name: fmt
    for fmt in [
        PageFormat(name="A4", width_mm=210, height_mm=297),
        PageFormat(name="A5", width_mm=148, height_mm=210),
        PageFormat(name="B5", width_mm=176, height_mm=250),
        PageFormat(name="Letter", width_mm=215.9, height_mm=279.4),
        PageFormat(name="11 × 17,8 cm", width_mm=110, height_mm=178),
        PageFormat(name="13 × 20 cm", width_mm=130, height_mm=200),
        PageFormat(name="15,5 × 24 cm", width_mm=155, height_mm=240),
        PageFormat(name="17 × 24 cm", width_mm=170, height_mm=240),
        PageFormat(name="21 × 28 cm", width_mm=210, height_mm=280),
        PageFormat(name="23 × 30 cm", width_mm=230, height_mm=300),
    ]
}
