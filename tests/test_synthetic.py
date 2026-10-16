"""Contrôle des pages synthétiques : la vérité terrain doit correspondre à l'image."""
import numpy as np
import pytest

from models import BlockKind, PageCategory
from tests.fixtures import synthetic


def _ink_ratio(image, box):
    left, top, right, bottom = box
    region = image[top:bottom, left:right].mean(axis=2)
    return float((region < 128).mean())


def test_reference_pages_have_consistent_truth(reference_page):
    page = reference_page
    assert page.image.dtype == np.uint8 and page.image.shape == (synthetic.PAGE_H, synthetic.PAGE_W, 3)
    assert len(page.blocks) >= 3
    assert len(page.words) == sum(len(block.text.split()) for block in page.blocks)
    for block in page.blocks:
        assert block.text.strip()
        if not block.on_image:
            assert _ink_ratio(page.image, block.box_px) > 0.02, block.text


def test_categories_and_images(text_page, graphic_page, photo_page):
    assert text_page.category == PageCategory.TEXT and not text_page.images
    assert graphic_page.category == PageCategory.GRAPHIC_BACKGROUND and graphic_page.background_regions_px
    assert photo_page.category == PageCategory.PHOTO_TEXT and len(photo_page.images) == 1
    assert any(block.kind == BlockKind.CAPTION for block in photo_page.blocks)


def test_truth_ocr_matches_words(text_page):
    ocr = text_page.truth_ocr()
    assert len(ocr.words) == len(text_page.words)
    assert "vallée" in ocr.text and ocr.mean_confidence == pytest.approx(95)


def test_transformations_record_truth(text_page):
    assert synthetic.rotated(text_page, 1).expected_fix_cw == 3
    assert synthetic.rotated(text_page, 2).image.shape == text_page.image.shape
    assert synthetic.skewed(text_page, 3).skew_applied_deg == 3
    perspective = synthetic.on_dark_background(text_page)
    assert len(perspective.quad) == 4 and perspective.width > text_page.width
    assert synthetic.curved(text_page).curvature_px > 0


def test_two_columns(two_column_page):
    assert two_column_page.columns == 2
    assert {block.column for block in two_column_page.blocks if block.kind == BlockKind.PARAGRAPH} == {0, 1}
