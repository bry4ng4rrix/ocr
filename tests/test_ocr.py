"""Tesseract wrapper: real OCR on synthetic pages, explicit errors, orientation."""
import difflib
import stat

import cv2
import numpy as np
import pytest

import config
from core import ocr_engine
from core.document_models import Box
from core.errors import OCRFailedError, OCRUnavailableError
from tests import synthetic
from tests.conftest import requires_tesseract

ACCENTED = ("« Ça commence ici », dit-il. La leçon était très chère à son cœur ; "
            "où êtes-vous, Noël ? L'été dernier, Hélène a vu l'œuvre.")


def _gray(rgb):
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)


def _text(result) -> str:
    return " ".join(w.text for w in result.words)


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, " ".join(a.split()), " ".join(b.split())).ratio()


@requires_tesseract
def test_version_and_languages():
    assert ocr_engine.tesseract_version().startswith(("4", "5"))
    langs = ocr_engine.available_languages()
    assert {"fra", "eng"} <= set(langs)
    assert "osd" not in langs
    ocr_engine.check_languages(["fra", "eng"])   # no exception


@requires_tesseract
def test_simple_page_french_text_and_confidence():
    page = synthetic.simple_page(paragraphs=[synthetic.FR_PARAGRAPH, ACCENTED])
    result = ocr_engine.read_page(_gray(page.image), ["fra", "eng"])
    text = _text(result)
    truth = " ".join(b.text for b in page.blocks)
    assert _similarity(text, truth) > 0.9, text
    for char in "éèàçœ«»êëÇâ":
        assert char in text, f"{char!r} manquant dans {text!r}"
    assert result.width == synthetic.PAGE_W and result.height == synthetic.PAGE_H
    assert all(0 <= w.conf <= 100 for w in result.words)
    assert result.mean_confidence > 80
    assert result.preparation == "gris"
    words = {w.text: w for w in result.words}
    title = words["Chapitre"]
    assert title.block >= 1 and title.line >= 1 and title.height > 30


@requires_tesseract
def test_blank_page_reads_nothing():
    blank = np.full((800, 600), 255, np.uint8)
    result = ocr_engine.read_page(blank, ["fra"])
    assert result.words == [] and result.mean_confidence == -1.0


@requires_tesseract
def test_ocr_region_reads_one_block():
    page = synthetic.simple_page()
    l, t, r, b = page.of_kind("title")[0].box
    W, H = synthetic.PAGE_W, synthetic.PAGE_H
    text, conf = ocr_engine.ocr_region(_gray(page.image), Box(x=l / W, y=t / H, width=(r - l) / W, height=(b - t) / H),
                                       ["fra"])
    assert text == "Chapitre premier"
    assert conf > 80


@requires_tesseract
def test_missing_language_is_explicit():
    with pytest.raises(OCRUnavailableError) as err:
        ocr_engine.check_languages(["fra", "klingon"])
    message = str(err.value)
    assert "klingon" in message and "tesseract-ocr-klingon" in message and "tesseract-data-klingon" in message
    with pytest.raises(OCRUnavailableError):
        ocr_engine.run_ocr(np.full((50, 50), 255, np.uint8), ["klingon"])


def test_missing_binary_is_explicit(monkeypatch):
    monkeypatch.setattr(config, "TESSERACT_CMD", "/nonexistent/tesseract-absent")
    assert ocr_engine.tesseract_version() is None
    assert ocr_engine.available_languages() == []
    assert ocr_engine.detect_orientation(np.full((50, 50, 3), 255, np.uint8)) is None
    with pytest.raises(OCRUnavailableError) as err:
        ocr_engine.check_languages(["fra"])
    assert "apt install" in str(err.value) and "pacman" in str(err.value)
    with pytest.raises(OCRUnavailableError):
        ocr_engine.run_ocr(np.full((50, 50), 255, np.uint8), ["fra"])


def _fake_tesseract(tmp_path, body: str) -> str:
    script = tmp_path / "fake-tesseract"
    script.write_text("#!/bin/sh\nif [ \"$1\" = \"--list-langs\" ]; then\n"
                      "  echo 'List of available languages (2):'; echo fra; echo osd; exit 0\nfi\n" + body)
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return str(script)


def test_timeout_and_crash_raise_ocr_failed(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TESSERACT_CMD", _fake_tesseract(tmp_path, "sleep 5\n"))
    with pytest.raises(OCRFailedError) as err:
        ocr_engine.run_ocr(np.full((50, 50), 255, np.uint8), ["fra"], timeout=1)
    assert "délai" in str(err.value)
    monkeypatch.setattr(config, "TESSERACT_CMD", _fake_tesseract(tmp_path, "echo 'boom' >&2; exit 3\n"))
    with pytest.raises(OCRFailedError) as err:
        ocr_engine.run_ocr(np.full((50, 50), 255, np.uint8), ["fra"])
    assert "boom" in str(err.value)


def test_binarize_helper_keeps_shape():
    gray = _gray(synthetic.simple_page().image)
    binary = ocr_engine._binarize(gray)
    assert binary.shape == gray.shape and set(np.unique(binary)) <= {0, 255}


def test_empty_ocr_retries_binarized_and_keeps_recognized_text(monkeypatch):
    gray = np.full((20, 30), 255, np.uint8)
    empty = ocr_engine.OCRResult([], 30, 20, ["fra"])
    recovered = ocr_engine.OCRResult(
        [ocr_engine.OCRWordPx("Bonjour", 1, 1, 10, 5, 90.0)], 30, 20, ["fra"])
    readings = iter((empty, recovered))
    images = []

    def fake_run_ocr(image, languages):
        images.append(image)
        return next(readings)

    monkeypatch.setattr(ocr_engine, "run_ocr", fake_run_ocr)
    monkeypatch.setattr(ocr_engine, "_binarize", lambda image: np.zeros_like(image))

    result = ocr_engine.read_page(gray, ["fra"])

    assert len(images) == 2
    assert result.words[0].text == "Bonjour"
    assert result.preparation == "binarisé"


@requires_tesseract
def test_low_confidence_retry_keeps_the_better_reading():
    gray = _gray(synthetic.simple_page(size_px=20).image)
    small = cv2.resize(gray, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    degraded = cv2.resize(small, (gray.shape[1], gray.shape[0]), interpolation=cv2.INTER_LINEAR)
    first = ocr_engine.run_ocr(degraded, ["fra", "eng"])
    kept = ocr_engine.read_page(degraded, ["fra", "eng"], low_confidence=60)
    assert first.mean_confidence < 60
    assert kept.preparation in ("gris", "binarisé")
    assert kept.score >= first.score


@requires_tesseract
@pytest.mark.parametrize("turns, expected", [(1, 90), (3, 270)])
def test_orientation_of_a_turned_page(turns, expected):
    """np.rot90 turns counter-clockwise; the correction is the clockwise turn undoing it.

    Measured: k=1 -> OSD (90, ~9.6) confirmed by reading; k=3 -> (270, ~9.3); upright -> (0, ~16.5).
    """
    found = ocr_engine.detect_orientation(np.rot90(synthetic.simple_page().image, turns))
    assert found is not None
    angle, confidence = found
    assert angle == expected and confidence > 0
    upright = np.rot90(np.rot90(synthetic.simple_page().image, turns), k=-(angle // 90))
    assert np.array_equal(upright, synthetic.simple_page().image)


@requires_tesseract
def test_orientation_of_a_blank_page_is_none():
    assert ocr_engine.detect_orientation(np.full((600, 400, 3), 255, np.uint8)) is None
