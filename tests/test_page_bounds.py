"""Bords de page par la couleur du papier, et par les bords droits pour les pages sombres (fond irrégulier : marge claire, bandes noires, tranche du livre)."""
from __future__ import annotations

import numpy as np
import pytest

from models.page import GeometryCorrection
from services import orientation_service, page_bounds
from tests.fixtures import synthetic


def _mixed_background(page: np.ndarray) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Page grise posée dans une photo : marge blanche (capture), bande noire, tranche rayée à droite."""
    gray_page = np.clip(page.astype(np.float32) * 0.82, 0, 255).astype(np.uint8)
    height, width = gray_page.shape[:2]
    canvas_h, canvas_w = int(height * 1.12), int(width * 1.16)
    canvas = np.full((canvas_h, canvas_w, 3), 255, np.uint8)                     # marge blanche
    canvas[int(0.02 * canvas_h):, int(0.03 * canvas_w):] = 25                    # fond noir
    top, left = int(0.05 * canvas_h), int(0.05 * canvas_w)
    canvas[top:top + height, left:left + width] = gray_page
    for x in range(left + width + 4, min(canvas_w, left + width + 60), 4):       # tranche du livre
        canvas[top:top + height, x:x + 2] = 140
    return canvas, (left, top, left + width, top + height)


def test_page_is_found_on_mixed_background():
    page = synthetic.text_page().image
    photo, (left, top, right, bottom) = _mixed_background(page)
    height, width = photo.shape[:2]
    found = page_bounds.detect_paper_quad(photo)
    assert found is not None and not found.uncertain
    xs = [p.x * width for p in found.corners]
    ys = [p.y * height for p in found.corners]
    tolerance = 0.015 * max(width, height)
    assert abs(min(xs) - left) < tolerance and abs(max(xs) - right) < tolerance
    assert abs(min(ys) - top) < tolerance and abs(max(ys) - bottom) < tolerance


def test_geometry_uses_paper_fallback_when_contrast_detection_fails():
    photo, (left, top, right, bottom) = _mixed_background(synthetic.text_page().image)
    outcome = orientation_service.correct_page(photo, GeometryCorrection(auto_orientation=False))
    codes = {issue.code for issue in outcome.issues}
    assert "quad_from_paper" in codes or outcome.geometry.detected_quad is not None
    height, width = outcome.image.shape[:2]
    assert abs(width / height - (right - left) / (bottom - top)) < 0.05
    assert outcome.image[:, :5].mean() > 120 and outcome.image[:, -5:].mean() > 120   # ni noir ni tranche


def test_full_frame_scan_is_not_cropped():
    assert page_bounds.detect_paper_quad(synthetic.text_page().image) is None


def test_quad_overlap():
    from models.geometry import Point

    square = [Point(x=0.1, y=0.1), Point(x=0.9, y=0.1), Point(x=0.9, y=0.9), Point(x=0.1, y=0.9)]
    half = [Point(x=0.1, y=0.1), Point(x=0.5, y=0.1), Point(x=0.5, y=0.9), Point(x=0.1, y=0.9)]
    assert page_bounds.quad_overlap(square, square) > 0.98
    assert 0.45 < page_bounds.quad_overlap(square, half) < 0.55


def test_full_frame_pages_with_edge_illustrations_are_never_cropped():
    """Non-régression : une illustration collée à un bord n'est pas prise pour du fond."""
    for make in (synthetic.text_page, synthetic.photo_text_page, synthetic.graphic_background_page,
                 synthetic.two_column_page):
        assert page_bounds.detect_paper_quad(make().image) is None, make.__name__


def _dark_cover_on_bench() -> tuple[np.ndarray, tuple[int, int, int, int]]:
    """Couverture sombre (photo brune) sur banc noir : tranche blanche au-dessus, page claire en dessous,
    bord droit presque invisible (comme la photo 00019 du client)."""
    rng = np.random.default_rng(7)
    height, width = 1300, 2000
    photo = np.clip(rng.normal(11, 1.5, (height, width, 3)), 0, 255).astype(np.uint8)     # banc noir
    left, top, right, bottom = 630, 470, 1475, 1150
    cover = np.zeros((bottom - top, right - left, 3), np.float32)
    gradient = np.linspace(1.0, 0.25, right - left, dtype=np.float32)[None, :, None]     # plus sombre à droite
    cover[:] = np.array([75, 48, 28], np.float32)
    cover = cover * gradient + rng.normal(0, 2, cover.shape)
    photo[top:bottom, left:right] = np.clip(cover, 0, 255).astype(np.uint8)
    photo[top - 40:top - 8, left + 10:right - 10] = 235                                    # tranche des pages
    photo[bottom + 6:, left - 10:right + 5] = 215                                           # page en vis-à-vis
    for y in range(top + 150, top + 420, 28):                                              # texte clair imprimé
        photo[y:y + 8, left + 200:right - 300] = 200
    return photo, (left, top, right, bottom)


def test_dark_cover_on_dark_bench_is_found_by_its_straight_edges():
    photo, (left, top, right, bottom) = _dark_cover_on_bench()
    found = page_bounds.detect_line_quad(photo)
    assert found is not None
    height, width = photo.shape[:2]
    xs = [p.x * width for p in found.corners]
    ys = [p.y * height for p in found.corners]
    tolerance = 0.02 * width
    assert abs(min(xs) - left) < tolerance and abs(max(xs) - right) < tolerance
    assert abs(min(ys) - top) < tolerance and abs(max(ys) - bottom) < tolerance


def test_geometry_falls_back_to_straight_edges_for_dark_pages():
    photo, (left, top, right, bottom) = _dark_cover_on_bench()
    outcome = orientation_service.correct_page(photo, GeometryCorrection(auto_orientation=False, auto_deskew=False,
                                                                         auto_dewarp=False))
    assert outcome.geometry.detected_quad is not None
    height, width = outcome.image.shape[:2]
    assert abs(width / height - (right - left) / (bottom - top)) < 0.06
    assert outcome.image[:, :, :].mean() < 80                     # la couverture sombre, pas le banc entier


def test_straight_edge_detector_ignores_photos_inside_pages():
    for make in (synthetic.text_page, synthetic.photo_text_page, synthetic.graphic_background_page,
                 synthetic.two_column_page):
        assert page_bounds.detect_line_quad(make().image) is None, make.__name__


def test_dark_page_gets_no_edge_trim_nor_false_review():
    from services import image_enhancement, image_quality_service

    photo, (left, top, right, bottom) = _dark_cover_on_bench()
    cover = photo[top:bottom, left:right]
    metrics = image_quality_service.measure(cover)
    plan = image_enhancement.plan_adjustments(metrics, remove_background=True)
    adjustments = plan.adjustments
    assert not adjustments.has_crop and not adjustments.background_removal and not plan.review_required
    check = image_quality_service.compare(cover, cover.copy(), metrics)
    assert check.passed



@pytest.mark.real_images
def test_real_dark_cover_photo_00019(real_pages):
    """Photo réelle du client : couverture brune (photo pleine page) sur le banc noir."""
    from services import image_service

    photo = next((path for path in real_pages if path.name == "00019.jpg"), None)
    if photo is None:
        pytest.skip("photo 00019.jpg absente")
    found = page_bounds.detect_line_quad(image_service.read_image(photo))
    assert found is not None
    xs = sorted(p.x for p in found.corners)
    ys = sorted(p.y for p in found.corners)
    assert abs(xs[0] - 0.315) < 0.015 and abs(xs[-1] - 0.737) < 0.015      # bords gauche et droit de la couverture
    assert abs(ys[0] - 0.36) < 0.02 and abs(ys[-1] - 0.868) < 0.015         # sous la tranche, au-dessus de la page claire
