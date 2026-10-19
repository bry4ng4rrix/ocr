"""Vérifications du flux fac-similé image et de ses contrats locaux."""
from __future__ import annotations

import io

import cv2
import numpy as np
import pymupdf
import pytest
from PIL import Image

from services import image_enhancement, page_detection, pdf_export
from services.ollama_vision import VisionAdvice


def test_manual_spread_split_returns_two_independent_pages() -> None:
    rgb = np.zeros((100, 240, 3), dtype=np.uint8)
    rgb[:, :120, 0] = 200
    rgb[:, 120:, 2] = 200
    left, right = page_detection.split_spread(rgb, 0.5)
    assert left.shape[1] < rgb.shape[1]
    assert right.shape[1] < rgb.shape[1]
    assert left.mean(axis=(0, 1))[0] > left.mean(axis=(0, 1))[2]
    assert right.mean(axis=(0, 1))[2] > right.mean(axis=(0, 1))[0]


def test_automatic_detection_proposes_two_physical_pages() -> None:
    rgb = np.full((400, 800, 3), 30, dtype=np.uint8)
    cv2.rectangle(rgb, (30, 30), (390, 370), (245, 245, 245), -1)
    cv2.rectangle(rgb, (410, 30), (770, 370), (245, 245, 245), -1)
    candidates = page_detection.detect_pages(rgb)
    assert len(candidates) == 2
    assert candidates[0].corners[0][0] < candidates[1].corners[0][0]
    assert all("Double page" in candidate.warning for candidate in candidates)


def test_warp_page_preserves_page_pixels_and_color() -> None:
    rgb = np.full((120, 90, 3), 245, dtype=np.uint8)
    rgb[30:90, 20:70] = (200, 40, 30)
    result = page_detection.warp_page(rgb, ((0, 0), (1, 0), (1, 1), (0, 1)), padding=0)
    assert result.shape[:2] == rgb.shape[:2]
    assert result[50, 40, 0] > result[50, 40, 1]


@pytest.mark.parametrize("mode", ["Automatique", "Document couleur", "Document couleur amélioré"])
def test_color_modes_preserve_color_page(mode: str) -> None:
    rgb = np.full((120, 100, 3), 245, dtype=np.uint8)
    rgb[20:80, 20:70] = (190, 30, 20)
    output = image_enhancement.enhance_page(rgb, mode, 20)
    assert output.shape == rgb.shape
    assert int(output[50, 40, 0]) > int(output[50, 40, 1])


def test_pdf_export_creates_exactly_one_sheet_per_image_in_order() -> None:
    red = np.full((40, 30, 3), (230, 20, 20), dtype=np.uint8)
    blue = np.full((30, 40, 3), (20, 20, 230), dtype=np.uint8)
    data = pdf_export.create_image_pdf([red, blue], dpi=100, quality=100)
    with pymupdf.open(stream=data, filetype="pdf") as document:
        assert document.page_count == 2
        first, second = document[0].get_pixmap(), document[1].get_pixmap()
        assert first.width < first.height
        assert second.width > second.height
        assert len(document[0].get_images()) == len(document[1].get_images()) == 1


def test_ollama_advice_schema_rejects_out_of_range_values() -> None:
    with pytest.raises(ValueError):
        VisionAdvice.model_validate({"shadows": 200, "contrast": 1, "brightness": 0,
            "sharpness": 1, "color_document": True, "illustrations": False, "description": "ok"})


def test_image_upload_reader_accepts_png_signature() -> None:
    from services.image_service import decode_image
    buffer = io.BytesIO()
    Image.new("RGB", (18, 12), (20, 100, 200)).save(buffer, format="PNG")
    decoded = decode_image(buffer.getvalue(), name="sample.png")
    assert decoded.shape == (12, 18, 3)
    assert tuple(decoded[0, 0]) == (20, 100, 200)
