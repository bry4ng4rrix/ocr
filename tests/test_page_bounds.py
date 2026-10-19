"""Bords de page par la couleur du papier (fond irrégulier : marge claire, bandes noires, tranche du livre)."""
from __future__ import annotations

import numpy as np

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
