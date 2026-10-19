"""Export de fac-similés : une image validée par feuille PDF."""
from __future__ import annotations

import io
import zipfile
from collections.abc import Sequence

import numpy as np
from PIL import Image


def create_image_pdf(pages: Sequence[np.ndarray], dpi: int = 250, quality: int = 90,
                     paper: str = "Format réel") -> bytes:
    """Crée un PDF image, avec exactement une feuille par image d'entrée."""
    import pymupdf

    if not pages:
        raise ValueError("Aucune page validée à exporter.")
    document = pymupdf.open()
    for rgb in pages:
        height, width = rgb.shape[:2]
        if paper == "A4":
            page_width, page_height = 595.28, 841.89
            if width > height:
                page_width, page_height = page_height, page_width
        else:
            page_width, page_height = width * 72 / dpi, height * 72 / dpi
        page = document.new_page(width=page_width, height=page_height)
        image = Image.fromarray(rgb)
        stream = io.BytesIO()
        image.save(stream, format="JPEG", quality=quality, optimize=True)
        rect = page.rect
        scale = min(rect.width / width, rect.height / height)
        dw, dh = width * scale, height * scale
        x, y = (rect.width-dw)/2, (rect.height-dh)/2
        page.insert_image(pymupdf.Rect(x, y, x+dw, y+dh), stream=stream.getvalue())
    return document.tobytes(garbage=4, deflate=True)


def create_pages_zip(pages: Sequence[tuple[str, np.ndarray]]) -> bytes:
    """Archive ZIP contenant les pages en PNG sans perte."""
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for index, (name, image) in enumerate(pages, 1):
            buffer = io.BytesIO()
            Image.fromarray(image).save(buffer, "PNG")
            archive.writestr(f"page_{index:04d}_{name[:40]}.png", buffer.getvalue())
    return output.getvalue()
