"""Tests de l'aperçu en direct : fonctions pures de rendu (live_preview_render)."""
from __future__ import annotations

import numpy as np
import pytest

from components.live_preview_render import draw_overlay_boxes, render_flow_html, render_positioned_html
from models import BlockKind, Box, ImageBlock, ImageKind, Page, PageStatus, Project, TextBlock


def _sample_project_and_page() -> tuple[Project, Page]:
    project = Project(id="test_proj", name="Livre de test")
    page = Page(id="page_1", source_file="originals/dummy.jpg", source_sha256="a" * 64,
                original_name="p001.jpg", status=PageStatus.ANALYSED)
    page.text_blocks = [
        TextBlock(id="b1", kind=BlockKind.HEADING_1, box=Box(x=0.1, y=0.1, w=0.8, h=0.08), ocr_text="Titre du livre",
                  order=0),
        TextBlock(id="b2", kind=BlockKind.PARAGRAPH, box=Box(x=0.1, y=0.22, w=0.8, h=0.2),
                  ocr_text="Premier paragraphe avec des caractères spéciaux : <test> & \"guillemets\".", order=1),
        TextBlock(id="b3", kind=BlockKind.HEADER, box=Box(x=0.1, y=0.04, w=0.8, h=0.04), ocr_text="EN-TÊTE DE PAGE",
                  order=2),
        TextBlock(id="b4", kind=BlockKind.PAGE_NUMBER, box=Box(x=0.45, y=0.92, w=0.1, h=0.04), ocr_text="42",
                  order=3),
        TextBlock(id="b_hidden", kind=BlockKind.PARAGRAPH, box=Box(x=0.1, y=0.5, w=0.8, h=0.1),
                  ocr_text="Bloc masqué", hidden=True, order=4),
    ]
    page.image_blocks = [
        ImageBlock(id="img1", kind=ImageKind.PHOTO, box=Box(x=0.2, y=0.5, w=0.6, h=0.3), order=5),
    ]
    project.pages = [page]
    return project, page


def test_render_positioned_html_empty():
    project = Project(id="p", name="Test")
    empty_page = Page(id="empty", source_file="originals/dummy.jpg", source_sha256="0" * 64, original_name="empty.jpg")
    output = render_positioned_html(project, empty_page)
    assert "background-color: #ffffff" in output
    assert "Aucun bloc extrait" in output


def test_render_positioned_html_with_blocks():
    project, page = _sample_project_and_page()
    output = render_positioned_html(project, page, aspect_ratio=0.7)
    assert "background-color: #ffffff" in output
    assert "Titre du livre" in output
    # HTML escaping
    assert "&lt;test&gt; &amp; &quot;guillemets&quot;." in output
    # Block coordinates
    assert "left: 10.00%" in output
    assert "top: 10.00%" in output
    # Hidden block should not be present
    assert "Bloc masqué" not in output
    # Image block placeholder
    assert "Illustration" in output


def test_render_positioned_html_with_image_uri():
    project, page = _sample_project_and_page()
    uris = {"img1": "data:image/jpeg;base64,abc123"}
    output = render_positioned_html(project, page, image_data_uris=uris)
    assert 'src="data:image/jpeg;base64,abc123"' in output


def test_render_flow_html_empty():
    project = Project(id="p", name="Test")
    empty_page = Page(id="empty", source_file="originals/dummy.jpg", source_sha256="0" * 64, original_name="empty.jpg")
    output = render_flow_html(project, empty_page)
    assert "background-color: #ffffff" in output
    assert "Aucun texte extrait" in output


def test_render_flow_html_structure():
    project, page = _sample_project_and_page()
    output = render_flow_html(project, page)
    assert "background-color: #ffffff" in output
    # Header element
    assert "EN-TÊTE DE PAGE" in output
    # Title element
    assert "<h1" in output
    assert "Titre du livre</h1>" in output
    # Paragraph element
    assert "<p" in output
    assert "&lt;test&gt; &amp; &quot;guillemets&quot;." in output
    # Page number in footer
    assert "42" in output
    # Hidden block excluded
    assert "Bloc masqué" not in output


def test_draw_overlay_boxes():
    project, page = _sample_project_and_page()
    image = np.full((100, 100, 3), 255, dtype=np.uint8)
    drawn = draw_overlay_boxes(image, page)
    assert drawn.shape == image.shape
    assert drawn.dtype == np.uint8
    # Canvas should have modified pixels (rectangles drawn)
    assert not np.array_equal(image, drawn)


def test_draw_overlay_boxes_invalid():
    page = Page(id="p", source_file="originals/dummy.jpg", source_sha256="0" * 64, original_name="p.jpg")
    assert draw_overlay_boxes(None, page) is None
    gray = np.full((50, 50), 200, dtype=np.uint8)
    assert np.array_equal(draw_overlay_boxes(gray, page), gray)
