"""Prix de la numérisation : réglages par type de page et montant par livre (pages terminées seulement)."""
from __future__ import annotations

import logging

import config
from models.pricing import BookPriceEstimate, PriceLine, PricingSettings
from models.scanning import BookScanStats, PageKind

logger = logging.getLogger("bookocr.pricing")


def _path():
    return config.data_dir() / "pricing.json"


def load_prices() -> PricingSettings:
    path = _path()
    if path.is_file():
        try:
            return PricingSettings.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logger.warning("Prix illisibles (%s) : valeurs par défaut", type(error).__name__)
    return PricingSettings()


def save_prices(prices: PricingSettings) -> None:
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(prices.model_dump_json(indent=1), encoding="utf-8")
    temporary.replace(path)


def estimate(stats: BookScanStats, prices: PricingSettings) -> BookPriceEstimate:
    """Montant d'un livre : pages terminées de chaque type × prix du type."""
    lines = [PriceLine(kind=kind, pages=stats.by_kind.get(kind, 0), unit_price=prices.price(kind),
                       amount=round(stats.by_kind.get(kind, 0) * prices.price(kind), 2)) for kind in PageKind]
    classified = sum(line.pages for line in lines)
    return BookPriceEstimate(book_id=stats.book_id, book_name=stats.book_name, completed_pages=stats.completed_pages,
                             unclassified_pages=max(0, stats.completed_pages - classified), lines=lines,
                             total=round(sum(line.amount for line in lines), 2), currency=prices.currency)
