"""Operations on a project that do not need the OCR: import, page order, rotation, block edits.

Everything here works on core.document_models objects and the project folder; nothing depends on
Streamlit, so it is tested directly.
"""
from __future__ import annotations

import io
import logging
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
from PIL import Image

import config
from core import image_loader
from core.coordinates import rotate_box_quarter
from core.document_models import (Box, BlockType, ImageAdjustments, ImageBlock, LocalAdjustment, OCRLine, OCRWord,
                                  Page, ProcessingSettings, Project, RenderSettings, TextBlock, new_id)
from core.errors import InvalidFileError, LivreOCRError, ProjectError

from .storage_service import ProjectStorage, save_project

logger = logging.getLogger("livreocr.project")


def create_project(name: str, languages: list[str] | None = None) -> Project:
    project = Project(name=name.strip() or "Nouveau livre",
                      processing=ProcessingSettings(languages=languages or list(config.DEFAULT_LANGUAGES)),
                      render=RenderSettings(title=name.strip()))
    ProjectStorage(project.id).ensure()
    save_project(project)
    return project


# --------------------------------------------------------------------------- import

@dataclass
class ImportReport:
    added: list[str] = field(default_factory=list)       # page ids
    errors: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)     # duplicates


def import_files(project: Project, files: list[tuple[str, bytes]], progress=None) -> ImportReport:
    """Add the pages of the given files at the end of the project (originals stored untouched).

    A PDF or multi-frame TIFF gives one page per frame; a file already imported (same SHA-256) is
    skipped. Errors are collected per file and never stop the other files.
    """
    report = ImportReport()
    storage = ProjectStorage(project.id).ensure()
    known = {(p.sha256, p.source_index) for p in project.pages}
    total = len(files)
    for n, (name, data) in enumerate(files):
        if progress:
            progress(n, total, f"Import de {name}")
        try:
            inspected = image_loader.inspect_file(data, name)
        except InvalidFileError as exc:
            report.errors.append(str(exc))
            logger.info("import refusé : %s", exc)
            continue
        ext = ".pdf" if inspected.kind == "pdf" else (Path(name).suffix.lower() or ".img")
        original_rel = f"originals/{inspected.sha256[:24]}{ext}"
        if not storage.exists(original_rel):
            storage.write_bytes(original_rel, data)
        for item in inspected.items:
            if (inspected.sha256, item.source_index) in known:
                report.skipped.append(f"{name}" + (f" (page {item.source_index + 1})" if len(inspected.items) > 1 else ""))
                continue
            if len(project.pages) >= config.MAX_PAGES_PER_PROJECT:
                report.errors.append(f"Limite de {config.MAX_PAGES_PER_PROJECT} pages atteinte : {name} non importé en entier.")
                break
            page = Page(original_path=original_rel, original_filename=name, source_index=item.source_index,
                        original_width=item.width, original_height=item.height, sha256=inspected.sha256,
                        processing_mode=project.processing.default_mode)
            page.geometry.auto_orientation = project.processing.auto_orientation
            page.geometry.auto_crop = project.processing.auto_crop
            page.geometry.auto_deskew = project.processing.auto_deskew
            try:
                rgb = image_loader.read_image(storage.path(original_rel), item.source_index)
                page.original_width, page.original_height = rgb.shape[1], rgb.shape[0]
                page.thumbnail_path = f"thumbnails/{page.id}.png"
                storage.write_bytes(page.thumbnail_path,
                                    image_loader.to_png_bytes(image_loader.thumbnail(rgb, config.THUMBNAIL_MAX_SIDE)))
                del rgb
            except LivreOCRError as exc:
                report.errors.append(f"{name} : {exc}")
                continue
            project.pages.append(page)
            known.add((inspected.sha256, item.source_index))
            report.added.append(page.id)
    if progress:
        progress(total, total, "Import terminé")
    save_project(project)
    return report


# --------------------------------------------------------------------------- page order

def move_page(project: Project, page_id: str, delta: int) -> None:
    index = project.page_index(page_id)
    target = min(max(index + delta, 0), len(project.pages) - 1)
    page = project.pages.pop(index)
    project.pages.insert(target, page)


def reorder_pages(project: Project, ordered_ids: list[str]) -> None:
    current = {p.id: p for p in project.pages}
    if sorted(ordered_ids) != sorted(current):
        raise ProjectError("La nouvelle liste ne correspond pas aux pages du projet.")
    project.pages = [current[i] for i in ordered_ids]


def sort_pages_by_filename(project: Project) -> None:
    project.pages.sort(key=lambda p: (p.original_filename.lower(), p.source_index))


def delete_page(project: Project, page_id: str) -> None:
    """Remove a page and its derived files; the original stays if another page uses it."""
    page = project.pages.pop(project.page_index(page_id))
    storage = ProjectStorage(project.id)
    for rel in (page.frame_path, page.ocr_image_path, page.thumbnail_path):
        storage.remove(rel)
    for block in page.image_blocks:
        storage.remove(block.image_path)
        if block.original_image_path != block.image_path:
            storage.remove(block.original_image_path)
    if not any(p.original_path == page.original_path for p in project.pages):
        storage.remove(page.original_path)


# --------------------------------------------------------------------------- geometry edits

def _turn_point(point, degrees: int):
    x, y = point
    return {90: (1.0 - y, x), 180: (1.0 - x, 1.0 - y), 270: (y, 1.0 - x)}[degrees]


def _turn_quad(quad, degrees: int):
    if quad is None:
        return None
    turned = [_turn_point(p, degrees) for p in quad]            # tl, tr, br, bl rotated
    shift = degrees // 90                                         # new tl is the old corner `shift` before
    return [tuple(min(max(v, 0.0), 1.0) for v in turned[(i - shift) % 4]) for i in range(4)]


def _turn_lines(lines: list[OCRLine], degrees: int) -> list[OCRLine]:
    out = []
    for line in lines:
        lb = rotate_box_quarter(Box.clamped(line.x, line.y, line.width, line.height), degrees)
        words = []
        for w in line.words:
            wb = rotate_box_quarter(Box.clamped(w.x, w.y, w.width, w.height), degrees)
            words.append(OCRWord(text=w.text, x=wb.x, y=wb.y, width=wb.width, height=wb.height, confidence=w.confidence))
        out.append(OCRLine(x=lb.x, y=lb.y, width=lb.width, height=lb.height, words=words))
    return out


def rotate_page(project: Project, page: Page, degrees: int) -> None:
    """Turn the page clockwise by a quarter-turn multiple.

    Every stored coordinate is turned with it (quads, trims, blocks, OCR lines, local corrections)
    and illustration sources are turned losslessly, so OCR results stay valid without a new OCR.
    The frame image must be rebuilt afterwards (processing_service.ensure_frame).
    """
    degrees %= 360
    if degrees not in (90, 180, 270):
        return
    g = page.geometry
    signature_was_current = bool(page.ocr_geometry_signature) and not page.ocr_outdated
    g.rotation = (g.rotation + degrees) % 360
    g.manual_quad = _turn_quad(g.manual_quad, degrees)
    g.detected_quad = _turn_quad(g.detected_quad, degrees)
    trims = [g.trim_left, g.trim_top, g.trim_right, g.trim_bottom]          # left, top, right, bottom
    shift = degrees // 90
    g.trim_left, g.trim_top, g.trim_right, g.trim_bottom = [trims[(i - shift) % 4] for i in range(4)]
    storage = ProjectStorage(project.id)
    for block in page.blocks:
        block.set_box(rotate_box_quarter(block.box, degrees))
        if isinstance(block, TextBlock):
            block.lines = _turn_lines(block.lines, degrees)
        elif isinstance(block, ImageBlock):
            if block.source_box is not None:
                block.source_box = rotate_box_quarter(block.source_box, degrees)
            if storage.exists(block.image_path):
                with Image.open(storage.path(block.image_path)) as im:
                    turned = im.convert("RGB").rotate(-degrees, expand=True)
                new_rel = f"illustrations/{block.id}_{new_id()}.png"
                buffer = io.BytesIO()
                turned.save(buffer, "PNG")
                storage.write_bytes(new_rel, buffer.getvalue())
                if block.image_path != block.original_image_path:
                    storage.remove(block.image_path)
                block.image_path = new_rel
                block.source_width, block.source_height = turned.size
    for local in page.local_adjustments:
        local.box = rotate_box_quarter(local.box, degrees)
    if page.frame_width and degrees in (90, 270):
        page.frame_width, page.frame_height = page.frame_height, page.frame_width
    page.frame_path = ""            # stale: rebuilt on demand
    page.ocr_image_path = ""
    if signature_was_current:
        page.ocr_geometry_signature = g.signature()


# --------------------------------------------------------------------------- block edits (editor)

def add_text_block(page: Page, box: Box | None = None, text: str = "Nouveau texte", font_size: float = 11.0) -> TextBlock:
    box = box or Box(x=0.1, y=0.1, width=0.5, height=0.08)
    block = TextBlock(x=box.x, y=box.y, width=box.width, height=box.height, text=text, ocr_text="",
                      manual=True, font_size=font_size, reading_order=len(page.blocks))
    page.blocks.append(block)
    return block


def add_image_block(project: Project, page: Page, data: bytes, filename: str, box: Box | None = None) -> ImageBlock:
    """Add an uploaded illustration (stored as lossless PNG)."""
    rgb = _decode_upload(data, filename)
    block_id = new_id("b_")
    rel = f"illustrations/{block_id}_upload.png"
    ProjectStorage(project.id).ensure().write_bytes(rel, image_loader.to_png_bytes(rgb))
    h, w = rgb.shape[:2]
    if box is None:
        aspect = (h / w) * ((page.frame_width or 1) / (page.frame_height or 1))
        width = 0.4
        box = Box.clamped(0.3, 0.3, width, min(width * aspect, 0.6))
    block = ImageBlock(id=block_id, x=box.x, y=box.y, width=box.width, height=box.height, image_path=rel,
                       original_image_path=rel, source_width=w, source_height=h, detection="manual",
                       manual=True, reading_order=len(page.blocks))
    page.blocks.append(block)
    return block


def replace_image(project: Project, block: ImageBlock, data: bytes, filename: str) -> None:
    """New source for an illustration; the first extraction stays available for revert."""
    rgb = _decode_upload(data, filename)
    rel = f"illustrations/{block.id}_{new_id()}.png"
    storage = ProjectStorage(project.id)
    storage.write_bytes(rel, image_loader.to_png_bytes(rgb))
    if block.image_path != block.original_image_path:
        storage.remove(block.image_path)
    block.image_path = rel
    block.source_width, block.source_height = rgb.shape[1], rgb.shape[0]
    block.crop = Box()
    block.detection = "replaced"
    block.source_box = None
    block.adjustments = ImageAdjustments()
    block.draft_adjustments = None


def revert_image(project: Project, block: ImageBlock) -> None:
    if not block.original_image_path or block.image_path == block.original_image_path:
        return
    storage = ProjectStorage(project.id)
    storage.remove(block.image_path)
    block.image_path = block.original_image_path
    with Image.open(storage.path(block.image_path)) as im:
        block.source_width, block.source_height = im.size
    block.crop = Box()


def _decode_upload(data: bytes, filename: str) -> np.ndarray:
    inspected = image_loader.inspect_file(data, filename)
    if inspected.kind != "image":
        raise InvalidFileError(f"{filename} : une image est attendue (JPG, PNG, TIFF).")
    return image_loader.decode_image(data, 0)


def delete_block(project: Project, page: Page, block_id: str) -> None:
    block = page.block(block_id)
    if block is None:
        return
    page.blocks.remove(block)
    for other in page.image_blocks:
        if other.caption_id == block_id:
            other.caption_id = None
    renumber(page)


def renumber(page: Page) -> None:
    for index, block in enumerate(page.ordered_blocks()):
        block.reading_order = index


def move_in_reading_order(page: Page, block_id: str, delta: int) -> None:
    ordered = page.ordered_blocks()
    index = next(i for i, b in enumerate(ordered) if b.id == block_id)
    target = min(max(index + delta, 0), len(ordered) - 1)
    ordered.insert(target, ordered.pop(index))
    for i, block in enumerate(ordered):
        block.reading_order = i


def set_block_box(block, x: float, y: float, width: float, height: float) -> list[str]:
    """Move / resize a block, forced inside the frame. Returns a warning when it had to be clamped."""
    box = Box.clamped(x, y, width, height)
    warnings = []
    if (abs(box.x - x) > 1e-4 or abs(box.y - y) > 1e-4 or abs(box.width - width) > 1e-4
            or abs(box.height - height) > 1e-4):
        warnings.append("Le bloc dépassait de la page : il a été ramené à l'intérieur.")
    block.set_box(box)
    block.manual = True
    return warnings


def add_local_adjustment(page: Page, box: Box, label: str = "") -> LocalAdjustment:
    local = LocalAdjustment(box=box, label=label or f"Zone {len(page.local_adjustments) + 1}")
    page.local_adjustments.append(local)
    return local


def block_type_options(block) -> list[BlockType]:
    if isinstance(block, ImageBlock):
        return [BlockType.ILLUSTRATION, BlockType.DECORATION, BlockType.TABLE, BlockType.OTHER]
    return [t for t in BlockType if t not in (BlockType.ILLUSTRATION, BlockType.DECORATION)]
