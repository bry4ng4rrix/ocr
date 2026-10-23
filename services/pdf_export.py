"""Export de fac-similés : une image par feuille PDF ; variante recherchable avec couche OCR invisible."""
from __future__ import annotations

import io
import logging
import os
import uuid
import zipfile
from collections.abc import Sequence
from pathlib import Path

import numpy as np
from PIL import Image, UnidentifiedImageError

import config
from models.errors import BookOcrError, ExportError
from models.geometry import MM_TO_PT
from models.ocr import OcrResult, OcrWord
from models.reports import Issue, Severity
from models.scanning import ProbeResult, SearchablePageInput, SearchablePdfReport, SearchablePdfValidation

logger = logging.getLogger("bookocr.pdf_export")


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


# --------------------------------------------------------------------------- PDF recherchable (numérisation)

OCR_FONT_FAMILY = "Liberation Sans"   # police Unicode embarquée (sous-ensemble) pour la couche invisible
OCR_FONT_NAME = "ocrlayer"
COLOR_CHECK_SIDE = 300                # rendu de contrôle des couleurs (pixels du grand côté)
MAX_COLOR_DELTA = 6.0                 # écart moyen Lab toléré entre le rendu PDF et l'image finale
PROBE_MIN_LETTERS = 4
PROBE_MIN_CONFIDENCE = 70.0
UNIFORM_MARGIN = 0.03                 # marge blanche (fraction) autour de l'image en format uniforme


def _ocr_font() -> tuple[str, "object | None"]:
    """(fichier TTF, police PyMuPDF) ; repli sur Helvetica intégrée (latin-1 seulement) si la TTF manque."""
    import pymupdf

    from services import fonts

    try:
        path = fonts.font_file(OCR_FONT_FAMILY, False, False)
        return str(path), pymupdf.Font(fontfile=str(path))
    except (OSError, RuntimeError, ValueError, BookOcrError) as error:
        logger.warning("Police %s introuvable (%s) : repli sur Helvetica", OCR_FONT_FAMILY, error)
        return "", pymupdf.Font("helv")


def _read_ocr(path: str | None) -> OcrResult | None:
    if not path or not Path(path).is_file():
        return None
    return OcrResult.model_validate_json(Path(path).read_text(encoding="utf-8"))


def _jpeg_bytes(path: str) -> tuple[bytes, int, int]:
    try:
        data = Path(path).read_bytes()
        with Image.open(io.BytesIO(data)) as image:
            if image.format != "JPEG":
                raise ExportError("L'image finale d'une page n'est pas un JPEG.", detail=Path(path).name)
            width, height = image.size
    except (OSError, UnidentifiedImageError) as error:
        raise ExportError("Image finale illisible.", detail=f"{Path(path).name}: {error}") from error
    return data, width, height


def _write_words(page, words: list[OcrWord], font, fontfile: str, area) -> int:
    """Chaque mot en rendu 3 (invisible), ligne de base en bas de sa boîte, étiré à la largeur de la boîte.

    `area` : rectangle de l'image sur la page (les boîtes OCR sont normalisées sur l'image).
    """
    import pymupdf

    scale_x, scale_y = area.width, area.height

    ascender, descender = float(font.ascender), float(font.descender)
    em_height = max(ascender - descender, 0.5)
    written = 0
    for word in words:
        text = word.text.strip()
        if not text:
            continue
        left = area.x0 + word.box.x * scale_x
        width = word.box.w * scale_x
        height = word.box.h * scale_y
        bottom = area.y0 + (word.box.y + word.box.h) * scale_y
        size = max(height / em_height, 1.0)
        natural = font.text_length(text, fontsize=size)
        if natural <= 0 or width <= 0:
            continue
        origin = pymupdf.Point(left, bottom + descender * size)
        page.insert_text(origin, text, fontsize=size, fontname=OCR_FONT_NAME if fontfile else "helv",
                         fontfile=fontfile or None, render_mode=3,
                         morph=(origin, pymupdf.Matrix(width / natural, 1.0)))
        written += 1
    return written


def _oriented(size: tuple[float, float], orientation: str, landscape_image: bool) -> tuple[float, float]:
    """Taille de page selon l'orientation : portrait, paysage, ou « auto » (même sens que la photo)."""
    short, long = min(size), max(size)
    if orientation == "portrait" or (orientation == "auto" and not landscape_image):
        return short, long
    return long, short


def build_searchable_pdf(pages: Sequence[SearchablePageInput], output: Path, dpi: int | None = None, *,
                         uniform: bool = False, margin: float = UNIFORM_MARGIN,
                         paper_mm: tuple[float, float] | None = None, orientation: str = "auto",
                         margin_mm: float | None = None) -> SearchablePdfReport:
    """Un PDF : une page par entrée, dans l'ordre ; photographie finale visible, mots OCR invisibles.

    L'image (JPEG final, sans EXIF) est insérée sans recompression ; la page n'est jamais tournée.
    - par défaut : la page a la taille de l'image ;
    - `uniform=True` : format commun = taille médiane des images, plus la marge ;
    - `paper_mm=(largeur, hauteur)` : format papier (A4…), plus la marge `margin_mm` (10 mm par défaut).
    Formats communs : `orientation` « auto » (chaque page suit le sens de sa photo), « portrait » ou
    « paysage » ; chaque image est ajustée dans la zone utile sans déformation et centrée (fond blanc). Si
    l'entrée a un `content_box` (page à fond supprimé), c'est ce bloc de contenu qui est centré.
    Les mots sont écrits en mode de rendu 3 (invisibles, pas de texte blanc) dans leur boîte.
    """
    import pymupdf

    if not pages:
        raise ExportError("Aucune page à exporter.")
    if orientation not in ("auto", "portrait", "landscape"):
        raise ExportError("Orientation de page inconnue.", detail=orientation)
    dpi = int(dpi or config.SCAN_PDF_DPI)
    output = Path(output)
    report = SearchablePdfReport(output=str(output), expected_pages=len(pages), dpi=dpi)
    fontfile, font = _ocr_font()
    images = [_jpeg_bytes(item.image_path) for item in pages]
    points = 72.0 / dpi
    uniform = uniform or paper_mm is not None
    if paper_mm is not None:
        base = (paper_mm[0] * MM_TO_PT, paper_mm[1] * MM_TO_PT)
        margin_pt = (10.0 if margin_mm is None else margin_mm) * MM_TO_PT
    elif uniform:
        # Format commun = taille médiane des pages du livre : des photos de résolutions différentes donnent
        # des pages de même taille.
        median = (float(np.median([width for _, width, _ in images])) * points,
                  float(np.median([height for _, _, height in images])) * points)
        margin_pt = margin_mm * MM_TO_PT if margin_mm is not None else margin * min(median)
        base = (median[0] + 2 * margin_pt, median[1] + 2 * margin_pt)
    document = pymupdf.open()
    try:
        document.set_metadata({"creator": "book-ocr (numérisation)", "producer": f"PyMuPDF {pymupdf.VersionBind}"})
        for index, (item, (data, width, height)) in enumerate(zip(pages, images)):
            if uniform:
                page_width, page_height = _oriented(base, orientation, landscape_image=width > height)
                usable_width, usable_height = page_width - 2 * margin_pt, page_height - 2 * margin_pt
                if usable_width <= 10 or usable_height <= 10:
                    raise ExportError("Marge trop grande pour ce format de page.")
                page = document.new_page(width=page_width, height=page_height)
                fit = min(usable_width / width, usable_height / height)
                shown_width, shown_height = width * fit, height * fit
                if item.content_box is not None:
                    # Centre du bloc de contenu au centre de la page (fond blanc : le blanc débordant est coupé).
                    box = item.content_box
                    x0 = page_width / 2 - (box.x + box.w / 2) * shown_width
                    y0 = page_height / 2 - (box.y + box.h / 2) * shown_height
                else:
                    x0, y0 = (page_width - shown_width) / 2, (page_height - shown_height) / 2
                area = pymupdf.Rect(x0, y0, x0 + shown_width, y0 + shown_height)
                if item.content_box is not None and not page.rect.contains(_content_rect(area, item.content_box)):
                    report.issues.append(Issue(
                        code="scan_pdf_content_clipped", severity=Severity.WARNING, page_id=item.page_id,
                        message=f"Page {index + 1} : le bloc de contenu centré dépasse la page."))
            else:
                page = document.new_page(width=width * points, height=height * points)
                area = page.rect
            page.insert_image(area, stream=data, keep_proportion=False)
            ocr = _read_ocr(item.ocr_path)
            if ocr is None or not ocr.words:
                report.pages_without_text.append(index)
                deliberate = item.ocr_path is None             # OCR non demandé : information, pas un défaut
                report.issues.append(Issue(code="scan_pdf_no_text", page_id=item.page_id,
                                           severity=Severity.INFO if deliberate else Severity.WARNING,
                                           message=f"Page {index + 1} : aucune couche de texte "
                                                   f"({'OCR non demandé' if deliberate else 'OCR absent ou vide'})."))
                continue
            if (ocr.width, ocr.height) != (width, height):
                report.issues.append(Issue(code="scan_pdf_ocr_size", severity=Severity.WARNING, page_id=item.page_id,
                                           message=f"Page {index + 1} : l'OCR a été fait sur une image de taille "
                                                   "différente ; coordonnées remises à l'échelle."))
            report.words_written += _write_words(page, ocr.words, font, fontfile, area)
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex[:8]}.tmp")
        try:
            if fontfile:
                document.subset_fonts()
            document.save(temporary, garbage=3, deflate=True)
            os.replace(temporary, output)
        finally:
            temporary.unlink(missing_ok=True)
    finally:
        document.close()
    with pymupdf.open(output) as written:
        report.page_count = written.page_count
    report.file_size = output.stat().st_size
    if report.page_count != len(pages):
        report.issues.append(Issue(code="page_count_mismatch", severity=Severity.ERROR,
                                   message=f"Le PDF relu a {report.page_count} pages au lieu de {len(pages)}."))
    logger.info("PDF de numérisation écrit : %s (%d pages, %d mots)", output.name, report.page_count,
                report.words_written)
    return report


def _content_rect(area, box):
    import pymupdf

    return pymupdf.Rect(area.x0 + box.x * area.width, area.y0 + box.y * area.height,
                        area.x0 + box.right * area.width, area.y0 + box.bottom * area.height)


def _probe_words(ocr: OcrResult, count: int) -> list[str]:
    candidates = []
    for word in ocr.words:
        text = word.text.strip(".,;:!?()[]«»\"'’")
        if sum(ch.isalpha() for ch in text) >= PROBE_MIN_LETTERS and (
                word.confidence < 0 or word.confidence >= PROBE_MIN_CONFIDENCE) and text.isalpha():
            candidates.append(text)
    unique = sorted(set(candidates), key=lambda value: (-len(value), value))
    return unique[:count]


def _render_delta(page, image_bytes: bytes, area) -> float:
    """Écart moyen Lab entre le rendu de la partie visible de l'image (couche texte comprise) et la même
    partie de l'image source réduite (une image décalée pour centrer le contenu peut déborder de la page)."""
    import cv2
    import pymupdf

    visible = area & page.rect
    zoom = COLOR_CHECK_SIDE / max(visible.width, visible.height)
    pixmap = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), colorspace=pymupdf.csRGB, alpha=False, clip=visible)
    rendered = np.frombuffer(pixmap.samples, np.uint8).reshape(pixmap.height, pixmap.width, 3)
    with Image.open(io.BytesIO(image_bytes)) as image:
        width, height = image.size
        crop = ((visible.x0 - area.x0) / area.width * width, (visible.y0 - area.y0) / area.height * height,
                (visible.x1 - area.x0) / area.width * width, (visible.y1 - area.y0) / area.height * height)
        source = np.asarray(image.convert("RGB").crop(tuple(round(v) for v in crop)).resize(
            (pixmap.width, pixmap.height), Image.Resampling.BOX))
    lab_rendered = cv2.cvtColor(rendered.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)
    lab_source = cv2.cvtColor(source.astype(np.float32) / 255.0, cv2.COLOR_RGB2Lab)
    return float(np.linalg.norm(lab_rendered - lab_source, axis=2).mean())


def validate_searchable_pdf(path: Path, pages: Sequence[SearchablePageInput],
                            probes_per_page: int = 3) -> SearchablePdfValidation:
    """Relit le PDF : pages, texte extractible, couche 100 % invisible, recherche de mots, images, couleurs."""
    import pymupdf

    path = Path(path)
    result = SearchablePdfValidation(path=str(path), expected_pages=len(pages))
    try:
        document = pymupdf.open(path)
    except (RuntimeError, OSError) as error:
        result.issues.append(Issue(code="scan_pdf_unreadable", severity=Severity.ERROR,
                                   message=f"PDF illisible : {error}"))
        return result
    invisible = True
    unrecompressed = True
    deltas: list[float] = []
    with document:
        result.page_count = document.page_count
        result.page_count_ok = document.page_count == len(pages)
        if not result.page_count_ok:
            result.issues.append(Issue(code="scan_pdf_page_count", severity=Severity.ERROR,
                                       message=f"{document.page_count} pages au lieu de {len(pages)}."))
        for index, (pdf_page, item) in enumerate(zip(document, pages)):
            ocr = _read_ocr(item.ocr_path)
            if pdf_page.get_text().strip():
                result.pages_with_text += 1
            elif ocr is not None and ocr.words:
                result.issues.append(Issue(code="scan_pdf_text_missing", severity=Severity.ERROR, page_id=item.page_id,
                                           message=f"Page {index + 1} : texte OCR non extractible du PDF."))
            visible = [span for span in pdf_page.get_texttrace() if span.get("type") != 3]
            if visible:
                invisible = False
                result.issues.append(Issue(code="scan_pdf_visible_text", severity=Severity.ERROR, page_id=item.page_id,
                                           message=f"Page {index + 1} : {len(visible)} élément(s) de texte visible."))
            source = Path(item.image_path).read_bytes()
            images = pdf_page.get_images(full=True)
            if len(images) != 1 or document.extract_image(images[0][0])["image"] != source:
                unrecompressed = False
                result.issues.append(Issue(code="scan_pdf_image_changed", severity=Severity.ERROR, page_id=item.page_id,
                                           message=f"Page {index + 1} : l'image du PDF diffère du JPEG final."))
            if images:
                rects = pdf_page.get_image_rects(images[0][0])
                deltas.append(_render_delta(pdf_page, source, rects[0] if rects else pdf_page.rect))
            if ocr is not None:
                for word in _probe_words(ocr, probes_per_page):
                    found = bool(pdf_page.search_for(word))
                    result.probes.append(ProbeResult(word=word, page_index=index, found=found))
                    if not found:
                        result.issues.append(Issue(code="scan_pdf_probe_missing", severity=Severity.WARNING,
                                                   page_id=item.page_id,
                                                   message=f"Page {index + 1} : un mot reconnu est introuvable par la recherche."))
    result.invisible_text_only = invisible
    result.images_unrecompressed = unrecompressed
    result.max_color_delta = round(max(deltas), 2) if deltas else None
    if result.max_color_delta is not None and result.max_color_delta > MAX_COLOR_DELTA:
        result.issues.append(Issue(code="scan_pdf_color", severity=Severity.ERROR,
                                   message=f"Rendu PDF éloigné de l'image finale (écart {result.max_color_delta})."))
    result.passed = not any(issue.severity == Severity.ERROR for issue in result.issues)
    return result
