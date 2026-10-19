"""Prix de la numérisation par type de page (réglages de l'utilisateur, data/pricing.json)."""
from __future__ import annotations

from pydantic import BaseModel, Field

from models.scanning import PageKind


class PricingSettings(BaseModel):
    currency: str = Field(default="Ar", max_length=12)
    text_page: float = Field(default=0.0, ge=0)        # page de texte seul
    table_page: float = Field(default=0.0, ge=0)       # page avec tableau(x)
    image_page: float = Field(default=0.0, ge=0)       # page avec image(s)
    cover_page: float = Field(default=0.0, ge=0)       # couverture avec image

    def price(self, kind: PageKind) -> float:
        return {PageKind.TEXT: self.text_page, PageKind.TABLE: self.table_page, PageKind.IMAGE: self.image_page,
                PageKind.COVER: self.cover_page}[kind]


class PriceLine(BaseModel):
    kind: PageKind
    pages: int
    unit_price: float
    amount: float


class BookPriceEstimate(BaseModel):
    book_id: str
    book_name: str
    completed_pages: int
    unclassified_pages: int = 0        # terminées mais sans type connu (non comptées)
    lines: list[PriceLine] = Field(default_factory=list)
    total: float = 0.0
    currency: str = "Ar"
