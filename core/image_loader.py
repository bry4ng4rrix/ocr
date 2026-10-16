"""Reading and validating imported files (JPEG, PNG, TIFF with several frames, PDF pages).

The real format is checked by decoding, never trusted from the extension. Images are returned as
RGB uint8 arrays, upright according to their EXIF orientation.
"""
from __future__ import annotations

import hashlib
import io
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image, ImageOps, UnidentifiedImageError

import config

from .errors import InvalidFileError

IMAGE_FORMATS = {"JPEG", "PNG", "TIFF", "MPO"}
Image.MAX_IMAGE_PIXELS = config.MAX_IMAGE_PIXELS


@dataclass
class SourceItem:
    """One page found in an imported file."""
    source_index: int        # frame of a TIFF / page of a PDF, 0 for a single image
    width: int
    height: int


@dataclass
class InspectedFile:
    kind: str                # "image" or "pdf"
    format: str
    items: list[SourceItem]
    sha256: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_size(data: bytes, name: str) -> None:
    if len(data) > config.MAX_FILE_MB * 1024 * 1024:
        raise InvalidFileError(f"{name} : fichier trop volumineux ({len(data) / 1e6:.0f} Mo, maximum {config.MAX_FILE_MB} Mo).")
    if not data:
        raise InvalidFileError(f"{name} : fichier vide.")


def inspect_file(data: bytes, name: str) -> InspectedFile:
    """Check an uploaded file and list its pages. Raises InvalidFileError with a user message."""
    _check_size(data, name)
    ext = Path(name).suffix.lower()
    if ext not in config.ALLOWED_EXTENSIONS:
        raise InvalidFileError(f"{name} : extension {ext or '(aucune)'} non prise en charge "
                               f"({', '.join(sorted(config.ALLOWED_EXTENSIONS))}).")
    if data[:5] == b"%PDF-":
        return InspectedFile("pdf", "PDF", _pdf_items(data, name), sha256_bytes(data))
    if ext == ".pdf":
        raise InvalidFileError(f"{name} : ce n'est pas un PDF valide.")
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        with Image.open(io.BytesIO(data)) as image:
            fmt = image.format or ""
            if fmt not in IMAGE_FORMATS:
                raise InvalidFileError(f"{name} : format {fmt or 'inconnu'} non pris en charge.")
            items = []
            frames = getattr(image, "n_frames", 1) if fmt == "TIFF" else 1
            for index in range(frames):
                image.seek(index)
                w, h = image.size
                if w * h > config.MAX_IMAGE_PIXELS:
                    raise InvalidFileError(f"{name} : image trop grande ({w} × {h} pixels).")
                if w < 50 or h < 50:
                    raise InvalidFileError(f"{name} : image trop petite ({w} × {h} pixels).")
                orientation = image.getexif().get(0x0112, 1) if index == 0 else 1
                if orientation in (5, 6, 7, 8):
                    w, h = h, w
                items.append(SourceItem(index, w, h))
    except InvalidFileError:
        raise
    except Image.DecompressionBombError:
        raise InvalidFileError(f"{name} : image trop grande (nombre de pixels).")
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError) as exc:
        raise InvalidFileError(f"{name} : image illisible ou endommagée ({exc.__class__.__name__}).")
    return InspectedFile("image", fmt, items, sha256_bytes(data))


def _pdf_items(data: bytes, name: str) -> list[SourceItem]:
    import pypdfium2 as pdfium
    try:
        pdf = pdfium.PdfDocument(data)
    except Exception as exc:
        raise InvalidFileError(f"{name} : PDF illisible ({exc}).")
    try:
        count = len(pdf)
        if count == 0:
            raise InvalidFileError(f"{name} : le PDF ne contient aucune page.")
        if count > config.MAX_PDF_PAGES_PER_FILE:
            raise InvalidFileError(f"{name} : {count} pages, maximum {config.MAX_PDF_PAGES_PER_FILE}.")
        items = []
        for index in range(count):
            w_pt, h_pt = pdf[index].get_size()
            scale = config.PDF_IMPORT_DPI / 72
            items.append(SourceItem(index, int(w_pt * scale), int(h_pt * scale)))
        return items
    finally:
        pdf.close()


def render_pdf_page(data: bytes, index: int, dpi: int | None = None) -> np.ndarray:
    """Rasterise one PDF page (RGB), limited to MAX_IMAGE_PIXELS."""
    import pypdfium2 as pdfium
    dpi = dpi or config.PDF_IMPORT_DPI
    pdf = pdfium.PdfDocument(data)
    try:
        page = pdf[index]
        w_pt, h_pt = page.get_size()
        scale = dpi / 72
        if w_pt * h_pt * scale * scale > config.MAX_IMAGE_PIXELS:
            scale = (config.MAX_IMAGE_PIXELS / (w_pt * h_pt)) ** 0.5
        return np.array(page.render(scale=scale).to_pil().convert("RGB"))
    finally:
        pdf.close()


def decode_image(data: bytes, source_index: int = 0) -> np.ndarray:
    """RGB array of one frame of an image file, upright (EXIF), alpha flattened on white."""
    try:
        with Image.open(io.BytesIO(data)) as image:
            if source_index:
                image.seek(source_index)
            image = ImageOps.exif_transpose(image) if source_index == 0 else image.copy()
            if image.mode in ("RGBA", "LA", "PA") or (image.mode == "P" and "transparency" in image.info):
                rgba = image.convert("RGBA")
                background = Image.new("RGBA", rgba.size, (255, 255, 255, 255))
                image = Image.alpha_composite(background, rgba)
            elif image.mode in ("I;16", "I;16B", "I;16L", "I"):
                arr = np.array(image).astype(np.float32)
                peak = max(float(arr.max()), 1.0)
                image = Image.fromarray((arr * (255.0 / peak)).astype(np.uint8))
            return np.array(image.convert("RGB"))
    except Image.DecompressionBombError:
        raise InvalidFileError("image trop grande (nombre de pixels).")
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, EOFError) as exc:
        raise InvalidFileError(f"image illisible ({exc.__class__.__name__}).")


def read_image(path: Path, source_index: int = 0) -> np.ndarray:
    """RGB array of a stored original (image file, or one page of a PDF)."""
    try:
        data = Path(path).read_bytes()
    except OSError as exc:
        raise InvalidFileError(f"fichier inaccessible : {Path(path).name} ({exc.strerror}).")
    if data[:5] == b"%PDF-":
        return render_pdf_page(data, source_index)
    return decode_image(data, source_index)


def to_png_bytes(rgb: np.ndarray, dpi: int | None = None) -> bytes:
    """Lossless PNG encoding (used for every derived image: no repeated lossy compression)."""
    buffer = io.BytesIO()
    image = Image.fromarray(rgb)
    image.save(buffer, "PNG", compress_level=6, **({"dpi": (dpi, dpi)} if dpi else {}))
    return buffer.getvalue()


def thumbnail(rgb: np.ndarray, max_side: int) -> np.ndarray:
    image = Image.fromarray(rgb)
    image.thumbnail((max_side, max_side), Image.LANCZOS)
    return np.array(image)
