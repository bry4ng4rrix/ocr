"""Suppression du fond (papier → blanc, images conservées) et centrage du contenu dans le PDF."""
from __future__ import annotations

import io
from pathlib import Path

import cv2
import numpy as np
import pymupdf
import pytest
from PIL import Image

from models.geometry import Box
from models.ocr import OcrResult, OcrWord
from models.scanning import ScanAction, ScanAdjustments, SearchablePageInput
from services import image_enhancement, image_quality_service, image_service, pdf_export
from tests.fixtures import synthetic


def _dull(rgb: np.ndarray) -> np.ndarray:
    """Photo de page : papier gris et éclairage irrégulier."""
    height, width = rgb.shape[:2]
    ramp = np.linspace(0.85, 0.6, width, dtype=np.float32)[None, :, None]
    return np.clip(rgb.astype(np.float32) * ramp, 0, 255).astype(np.uint8)


@pytest.fixture(scope="module")
def photo_page():
    return synthetic.photo_text_page()


def test_paper_becomes_white_text_stays_dark_and_smooth():
    page = synthetic.text_page()
    out = image_enhancement.remove_background(_dull(page.image))
    gray = image_service.to_gray(out)
    left, top, right, bottom = page.blocks[0].box_px
    margin = gray[:top - 5, :]                                          # marge haute : papier seul
    assert margin.min() >= 250                                          # fond blanc pur
    block = gray[top:bottom, left:right]
    assert (block < 100).mean() > 0.02                                  # le texte reste noir
    assert len(np.unique(block)) > 20                                   # pas de seuillage : bords lissés


def test_photo_colors_are_kept(photo_page):
    dull = _dull(photo_page.image)
    out = image_enhancement.remove_background(dull)
    left, top, right, bottom = photo_page.images[0].box_px
    before = image_quality_service.colorfulness(dull[top:bottom, left:right])
    after = image_quality_service.colorfulness(out[top:bottom, left:right])
    assert after >= 0.8 * before
    assert out[top + 10:bottom - 10, left + 10:right - 10].mean() < 245     # la photo n'est pas blanchie
    check = image_quality_service.compare(dull, out, background_removed=True)
    assert not check.enhancement_degraded, check.reasons


def test_framed_reproduction_is_protected():
    """Une reproduction encadrée, claire comme le papier, n'est pas effacée comme du fond."""
    page = synthetic.text_page().image.copy()
    height, width = page.shape[:2]
    x0, y0, x1, y1 = int(0.55 * width), int(0.55 * height), int(0.92 * width), int(0.9 * height)
    page[y0:y1, x0:x1] = (232, 228, 215)
    rng = np.random.default_rng(3)
    for _ in range(60):                                                  # « écriture » grise dans le cadre
        x, y = int(rng.integers(x0 + 20, x1 - 80)), int(rng.integers(y0 + 20, y1 - 20))
        cv2.line(page, (x, y), (x + 60, y + int(rng.integers(-3, 4))), (150, 140, 130), 2)
    cv2.rectangle(page, (x0, y0), (x1, y1), (90, 90, 90), 4)
    out = image_enhancement.remove_background(_dull(page))
    inside = image_service.to_gray(out)[y0 + 30:y1 - 30, x0 + 30:x1 - 30]
    assert np.median(inside) < 250                                       # fond de la reproduction gardé


def test_plan_removes_background_only_when_asked_and_with_paper(photo_page):
    metrics = image_quality_service.measure(_dull(photo_page.image))
    plan = image_enhancement.plan_adjustments(metrics, remove_background=True)
    assert plan.adjustments.background_removal == 100 and ScanAction.LIGHTING_CORRECTION in plan.actions
    assert plan.adjustments.contrast == 0                                 # pas de contraste global sur papier blanc
    assert image_enhancement.plan_adjustments(metrics).adjustments.background_removal == 0
    cover = np.full((800, 600, 3), (40, 90, 30), np.uint8)               # couverture sombre : pas de papier
    dark = image_enhancement.plan_adjustments(image_quality_service.measure(cover), remove_background=True)
    assert dark.adjustments.background_removal == 0


def test_content_box_ignores_dust_and_white_margins():
    page = np.full((1000, 800, 3), 255, np.uint8)
    page[200:700, 150:500] = 30                                          # bloc de contenu décentré
    page[950, 790] = 0                                                   # poussière isolée
    box = image_quality_service.content_box(page)
    assert box is not None
    assert abs(box.x - 0.1875) < 0.02 and abs(box.right - 0.625) < 0.02
    assert abs(box.y - 0.2) < 0.02 and abs(box.bottom - 0.7) < 0.02
    assert image_quality_service.content_box(np.full((500, 400, 3), 255, np.uint8)) is None


def _page_input(tmp_path: Path, content: Box | None) -> SearchablePageInput:
    image = np.full((1600, 1200, 3), 255, np.uint8)
    image[300:900, 100:700] = (40, 60, 160)                              # contenu en haut à gauche
    path = tmp_path / "decentree.jpg"
    Image.fromarray(image).save(path, "JPEG", quality=92, subsampling=0)
    word = OcrWord(text="Inhalt", confidence=95, box=Box(x=100 / 1200, y=300 / 1600, w=600 / 1200, h=80 / 1600))
    ocr = tmp_path / "decentree.json"
    ocr.write_text(OcrResult(words=[word], width=1200, height=1600).model_dump_json(), encoding="utf-8")
    return SearchablePageInput(page_id="p", image_path=str(path), ocr_path=str(ocr), content_box=content)


def test_pdf_centres_the_content_block(tmp_path):
    content = Box(x=100 / 1200, y=300 / 1600, w=600 / 1200, h=600 / 1600)
    item = _page_input(tmp_path, content)
    output = tmp_path / "centre.pdf"
    report = pdf_export.build_searchable_pdf([item], output, uniform=True)
    assert not report.issues
    with pymupdf.open(output) as document:
        page = document[0]
        area = page.get_image_rects(page.get_images()[0][0])[0]
        block = pymupdf.Rect(area.x0 + content.x * area.width, area.y0 + content.y * area.height,
                             area.x0 + content.right * area.width, area.y0 + content.bottom * area.height)
        assert abs((block.x0 + block.x1) / 2 - page.rect.width / 2) < 0.5
        assert abs((block.y0 + block.y1) / 2 - page.rect.height / 2) < 0.5
        hit = page.search_for("Inhalt")[0]
        assert abs(hit.x0 - block.x0) < 2                                 # couche texte décalée avec l'image
    validation = pdf_export.validate_searchable_pdf(output, [item])
    assert validation.passed, validation.issues
