"""Page pipeline: original → frame → OCR image → OCR → illustrations → layout → blocks.

Every derived image is recomputed from the original (or from the frame, which is itself a pure
function of the original and the geometry settings), so image settings never accumulate losses.
Derived files are lossless PNG named after what produced them: a file whose name matches the
current settings is reused instead of being recomputed.
"""
from __future__ import annotations

import hashlib
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

import numpy as np
from PIL import Image

import config
from core import illustration_extraction, image_adjustments, image_loader, image_preprocessing, layout_analysis, ocr_engine
from core.coordinates import iou, norm_to_px, overlap_ratio, page_sheet_geometry, sheet_geometry
from core.document_models import (AdjustmentTarget, Binarization, Box, ImageBlock, LocalAdjustment, Page, PageStatus,
                                  ProcessingMode, Project, TextBlock, utcnow)
from core.errors import LivreOCRError, ProcessingError

from .storage_service import ProjectStorage, save_project

logger = logging.getLogger("livreocr.processing")
Progress = Callable[[float, str], None]     # (fraction 0..1, message)


# --------------------------------------------------------------------------- small in-process cache

class _LRU:
    """Keeps a few decoded images in memory between Streamlit reruns (bounded by count)."""

    def __init__(self, size: int = 6):
        self.size = size
        self.items: OrderedDict = OrderedDict()

    def get(self, key):
        if key in self.items:
            self.items.move_to_end(key)
            return self.items[key]
        return None

    def put(self, key, value):
        self.items[key] = value
        self.items.move_to_end(key)
        while len(self.items) > self.size:
            self.items.popitem(last=False)


_frames = _LRU(4)
_renders = _LRU(8)


def _digest(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


# --------------------------------------------------------------------------- frame

def frame_key(page: Page) -> str:
    return _digest(page.original_path, page.source_index, page.geometry.signature(), page.geometry.scale_percent)


def frame_rel_path(page: Page) -> str:
    return f"work/{page.id}_frame_{frame_key(page)}.png"


def read_original(project: Project, page: Page) -> np.ndarray:
    storage = ProjectStorage(project.id)
    return image_loader.read_image(storage.path(page.original_path), page.source_index)


def _orientation_detector(rgb: np.ndarray) -> int | None:
    try:
        found = ocr_engine.detect_orientation(rgb)
    except LivreOCRError as exc:
        logger.info("orientation non détectée : %s", exc)
        return None
    return found[0] if found else None


def ensure_frame(project: Project, page: Page, redetect: bool = False) -> np.ndarray:
    """The page frame (RGB), built and stored if needed.

    Automatic detections (orientation, sheet, skew) run once and are stored in the geometry
    settings; later rebuilds reuse them, so a reloaded project gives exactly the same frame.
    """
    g = page.geometry
    need_detection = redetect or (
        (g.auto_orientation and g.detected_orientation is None)
        or (g.auto_crop and g.manual_quad is None and g.detected_quad is None and not page.frame_path)
        or (g.auto_deskew and g.deskew_angle is None and g.detected_skew is None))
    rel = frame_rel_path(page)
    storage = ProjectStorage(project.id).ensure()
    if not need_detection:
        cached = _frames.get((project.id, rel))
        if cached is not None:
            return cached
        if page.frame_path == rel and storage.exists(rel):
            frame = image_loader.read_image(storage.path(rel))
            _frames.put((project.id, rel), frame)
            return frame
    original = read_original(project, page)
    detector = _orientation_detector if (project.processing.auto_orientation and g.auto_orientation) else None
    try:
        result = image_preprocessing.build_frame(original, page.geometry, detect=need_detection,
                                                 orientation_detector=detector)
    except LivreOCRError:
        raise
    except Exception as exc:
        logger.exception("cadre impossible pour %s", page.id)
        raise ProcessingError(f"Préparation de la page impossible ({exc.__class__.__name__} : {exc}).")
    del original
    page.geometry = result.geometry
    if need_detection:
        # "Searched, nothing reliable found" is remembered as 0 so it is not searched again.
        if page.geometry.auto_orientation and page.geometry.detected_orientation is None:
            page.geometry.detected_orientation = 0
        if page.geometry.auto_deskew and page.geometry.detected_skew is None:
            page.geometry.detected_skew = 0.0
    for warning in result.warnings:
        if warning not in page.warnings:
            page.warnings.append(warning)
    rel = frame_rel_path(page)          # detection may have changed the signature
    if page.frame_path and page.frame_path != rel:
        storage.remove(page.frame_path)
    storage.write_bytes(rel, image_loader.to_png_bytes(result.image))
    page.frame_path = rel
    page.frame_height, page.frame_width = result.image.shape[:2]
    _frames.put((project.id, rel), result.image)
    return result.image


def resolve_mode(page: Page, frame: np.ndarray) -> ProcessingMode:
    if page.processing_mode != ProcessingMode.AUTO:
        return page.processing_mode
    if page.resolved_mode and page.resolved_mode != ProcessingMode.AUTO:
        return page.resolved_mode
    small, _ = image_preprocessing.downscale(frame, 1200)
    return image_preprocessing.suggest_mode(small)


def _text_boxes(page: Page) -> list[Box]:
    return [b.box for b in page.text_blocks if not b.hidden]


def _illustration_boxes(page: Page) -> list[Box]:
    return [b.source_box or b.box for b in page.image_blocks]


def print_image(project: Project, page: Page, max_side: int | None = None) -> np.ndarray:
    """The colour page with its print settings (global, local, mode): facsimile and previews."""
    frame = ensure_frame(project, page)
    mode = resolve_mode(page, frame)
    key = (project.id, frame_key(page), page.print_adjustments.model_dump_json(),
           tuple(l.model_dump_json() for l in page.local_adjustments), mode.value,
           tuple(round(v, 4) for b in _text_boxes(page) for v in (b.x, b.y, b.width, b.height)), max_side)
    cached = _renders.get(key)
    if cached is not None:
        return cached
    if max_side:
        frame, _ = image_preprocessing.downscale(frame, max_side)
    image = image_preprocessing.render_print_image(frame, page.print_adjustments, page.local_adjustments, mode,
                                                   _text_boxes(page))
    _renders.put(key, image)
    return image


def ocr_image(project: Project, page: Page, frame: np.ndarray | None = None, mode: ProcessingMode | None = None,
              illustration_boxes: list[Box] | None = None, max_side: int | None = None) -> np.ndarray:
    frame = ensure_frame(project, page) if frame is None else frame
    mode = mode or resolve_mode(page, frame)
    if max_side:
        frame, _ = image_preprocessing.downscale(frame, max_side)
    boxes = _illustration_boxes(page) if illustration_boxes is None else illustration_boxes
    return image_preprocessing.render_ocr_image(frame, page.ocr_adjustments, page.local_adjustments, mode, boxes)


# --------------------------------------------------------------------------- illustrations as used in the PDF

def _relative_locals(locals_: list[LocalAdjustment], source: Box) -> list[LocalAdjustment]:
    """Local corrections of the page that touch an illustration, in the illustration's own 0..1 space."""
    out = []
    for local in locals_:
        if not local.enabled or local.target == AdjustmentTarget.OCR or overlap_ratio(local.box, source) <= 0:
            continue
        x0, y0 = max(local.box.x, source.x), max(local.box.y, source.y)
        x1, y1 = min(local.box.x2, source.x2), min(local.box.y2, source.y2)
        if x1 - x0 <= 0 or y1 - y0 <= 0:
            continue
        box = Box.clamped((x0 - source.x) / source.width, (y0 - source.y) / source.height,
                          (x1 - x0) / source.width, (y1 - y0) / source.height)
        out.append(local.model_copy(update={"box": box}))
    return out


def illustration_array(project: Project, page: Page, block: ImageBlock, draft: bool = False) -> np.ndarray:
    """The illustration exactly as the PDF shows it: source → page colour settings → local corrections
    → crop → its own (validated, or draft when previewing) settings. Recomputed from the source."""
    storage = ProjectStorage(project.id)
    adjustments = block.draft_adjustments if (draft and block.draft_adjustments is not None) else block.adjustments
    key = (project.id, block.image_path, block.crop.model_dump_json(), adjustments.model_dump_json(),
           page.print_adjustments.model_dump_json() if block.source_box else "",
           tuple(l.model_dump_json() for l in page.local_adjustments) if block.source_box else ())
    cached = _renders.get(key)
    if cached is not None:
        return cached
    source = image_loader.read_image(storage.path(block.image_path))
    raw = source
    if block.source_box is not None:      # cut from the page: the page's colour settings apply to it too
        global_only = page.print_adjustments.model_copy(update={"binarization": Binarization.NONE, "grayscale": False})
        source = image_adjustments.apply_adjustments(source, global_only)
        locals_ = _relative_locals(page.local_adjustments, block.source_box)
        if locals_:
            source = image_adjustments.apply_local_adjustments(source, locals_, AdjustmentTarget.PRINT, reference=raw)
    h, w = source.shape[:2]
    left, top, right, bottom = norm_to_px(block.crop, w, h)
    source = source[top:bottom, left:right]
    result = image_adjustments.apply_adjustments(np.ascontiguousarray(source), adjustments)
    _renders.put(key, result)
    return result


def illustration_provider(project: Project):
    def provide(page: Page, block: ImageBlock) -> Image.Image:
        return Image.fromarray(illustration_array(project, page, block))
    return provide


def page_image_provider(project: Project):
    def provide(page: Page) -> Image.Image:
        return Image.fromarray(print_image(project, page))
    return provide


# --------------------------------------------------------------------------- OCR + layout

@dataclass
class PageResult:
    page_id: str
    ok: bool
    message: str = ""
    text_blocks: int = 0
    image_blocks: int = 0
    seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)


def _kept_blocks(page: Page) -> list:
    """Blocks the user created or corrected: a new OCR never overwrites them."""
    kept = []
    for block in page.blocks:
        if block.manual:
            kept.append(block)
        elif isinstance(block, TextBlock) and block.is_corrected:
            kept.append(block)
        elif isinstance(block, ImageBlock) and (block.detection == "replaced" or not block.adjustments.is_neutral()
                                                or block.crop != Box()):
            kept.append(block)
    return kept


def process_page(project: Project, page: Page, progress: Progress | None = None, redetect: bool = False) -> PageResult:
    """Run the whole analysis on one page and store the result in `page` (not saved)."""
    started = time.monotonic()
    step = progress or (lambda f, m: None)
    settings = project.processing
    ocr_engine.check_languages(settings.languages)
    step(0.05, "Préparation géométrique")
    frame = ensure_frame(project, page, redetect=redetect)
    mode = resolve_mode(page, frame)
    page.resolved_mode = mode
    storage = ProjectStorage(project.id)

    step(0.15, "Recherche des illustrations")
    regions: list = []
    if settings.detect_illustrations:
        try:
            regions = illustration_extraction.detect_illustrations(frame)
        except Exception as exc:
            logger.exception("détection des illustrations (1re passe) en échec, page %s", page.id)
            page.warnings.append(f"Détection des illustrations impossible : {exc}")
    sure = [r.box for r in regions if not r.uncertain and r.kind != "table"]

    step(0.25, "Préparation de l'image OCR")
    gray = ocr_image(project, page, frame, mode, sure if mode != ProcessingMode.TEXT else [])
    rel_ocr = f"work/{page.id}_ocr_{frame_key(page)}.png"
    if page.ocr_image_path and page.ocr_image_path != rel_ocr:
        storage.remove(page.ocr_image_path)
    storage.write_bytes(rel_ocr, image_loader.to_png_bytes(gray))
    page.ocr_image_path = rel_ocr

    step(0.35, "Reconnaissance du texte (OCR)")
    ocr = ocr_engine.read_page(gray, settings.languages, retry_binarized=settings.retry_binarized,
                               low_confidence=settings.low_confidence)
    del gray

    if settings.detect_illustrations:
        step(0.7, "Affinage des illustrations")
        good_words = [(w.left, w.top, w.left + w.width, w.top + w.height) for w in ocr.words
                      if w.conf >= settings.low_confidence and w.text.strip()]
        try:
            regions = illustration_extraction.detect_illustrations(frame, good_words)
        except Exception as exc:
            logger.exception("détection des illustrations en échec, page %s", page.id)
            page.warnings.append(f"Détection des illustrations impossible : {exc}")

    step(0.8, "Analyse de la mise en page")
    sheet = sheet_geometry(project.render, frame.shape[1], frame.shape[0])
    layout = layout_analysis.analyse_layout(ocr, frame, regions, sheet.content_height, settings)

    step(0.9, "Extraction des illustrations")
    kept = _kept_blocks(page)
    old_files = {b.image_path for b in page.image_blocks} | {b.original_image_path for b in page.image_blocks}
    new_blocks = []
    for block in layout.blocks:
        if any(iou(block.box, k.box) > 0.3 or overlap_ratio(block.box, k.box) > 0.6 for k in kept):
            continue                       # the user's version of this region wins
        if isinstance(block, ImageBlock):
            try:
                crop = illustration_extraction.crop_region(frame, block.box)
                rel = f"illustrations/{block.id}_src.png"
                storage.write_bytes(rel, image_loader.to_png_bytes(crop))
            except Exception as exc:
                logger.exception("découpe d'illustration en échec")
                page.warnings.append(f"Illustration non extraite ({exc}) : zone signalée.")
                continue
            block.image_path = block.original_image_path = rel
            block.source_height, block.source_width = crop.shape[:2]
            block.source_box = block.box
        new_blocks.append(block)
    page.blocks = kept + new_blocks
    kept_files = {p for b in page.image_blocks for p in (b.image_path, b.original_image_path)}
    for rel in old_files - kept_files:
        storage.remove(rel)
    layout_analysis.reading_order(page.blocks)

    page.columns = layout.columns
    page.margins = layout.margins
    page.printed_page_number = layout.printed_page_number
    page.font_reference_height_pt = sheet.content_height
    page.ocr_geometry_signature = page.geometry.signature()
    page.ocr_languages = list(settings.languages)
    page.ocr_mean_confidence = layout.mean_confidence
    page.ocr_preparation = ocr.preparation
    page.warnings = list(dict.fromkeys(layout.warnings))
    if not page.text_blocks:
        page.warnings.append("Aucun texte reconnu sur cette page (page blanche, image seule ou OCR sans résultat).")
    page.status = PageStatus.PROCESSED
    page.error = ""
    page.processed_at = utcnow()
    step(1.0, "Page terminée")
    return PageResult(page.id, True, "", len(page.text_blocks), len(page.image_blocks),
                      time.monotonic() - started, list(page.warnings))


def process_pages(project: Project, page_ids: list[str] | None = None,
                  progress: Callable[[int, int, float, str], None] | None = None,
                  redetect: bool = False) -> list[PageResult]:
    """Process pages one by one; an error on a page is recorded and the others go on.
    The project is saved after every page, so an interrupted run loses nothing already done."""
    pages = [p for p in project.pages if page_ids is None or p.id in page_ids]
    results = []
    for index, page in enumerate(pages):
        def step(fraction, message, _i=index):
            if progress:
                progress(_i, len(pages), fraction, message)
        try:
            results.append(process_page(project, page, step, redetect=redetect))
        except LivreOCRError as exc:
            page.status, page.error = PageStatus.ERROR, str(exc)
            results.append(PageResult(page.id, False, str(exc)))
            logger.warning("page %s : %s", page.id, exc)
        except Exception as exc:          # never lose the other pages because of one
            logger.exception("erreur inattendue sur la page %s", page.id)
            page.status, page.error = PageStatus.ERROR, f"Erreur inattendue : {exc.__class__.__name__} : {exc}"
            results.append(PageResult(page.id, False, page.error))
        save_project(project)
    return results


def rebuild_frame(project: Project, page: Page, redetect: bool = False) -> list[str]:
    """Rebuild the frame after a geometry change; returns warnings for the interface."""
    ensure_frame(project, page, redetect=redetect)
    warnings = []
    if page.ocr_outdated:
        warnings.append("Le cadre de la page a changé depuis l'OCR : les positions des blocs ne correspondent "
                        "plus à l'image. Relancez l'OCR de cette page (vos corrections seront conservées).")
    return warnings


def preview_size(page: Page, project: Project) -> tuple[float, float]:
    sheet = page_sheet_geometry(project.render, page)
    return sheet.page_width, sheet.page_height


def clear_caches() -> None:
    _frames.items.clear()
    _renders.items.clear()
