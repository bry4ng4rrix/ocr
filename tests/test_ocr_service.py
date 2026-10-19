"""OCR Tesseract : texte, accents, confiance, boîtes, seconde lecture, cache, langues et erreurs."""
import difflib
import subprocess

import cv2
import numpy as np
import pytest

import config
from models.errors import OcrFailedError, OcrUnavailableError
from services import image_service, ocr_service
from tests.conftest import requires_languages
from tests.fixtures import synthetic

needs_fra = requires_languages("fra")
pytestmark = pytest.mark.tesseract


@pytest.fixture(scope="module")
def page():
    return synthetic.text_page()


@pytest.fixture(scope="module")
def page_gray(page):
    return image_service.prepare_ocr_image(page.image)


@pytest.fixture(scope="module")
def page_result(page_gray):
    if "fra" not in ocr_service.available_languages():
        pytest.skip("langue fra absente")
    return ocr_service.run_ocr(page_gray, ["fra"])


def _normalised(text: str) -> str:
    return " ".join(text.split())


def _similarity(expected: str, found: str) -> float:
    return difflib.SequenceMatcher(None, _normalised(expected), _normalised(found)).ratio()


@needs_fra
def test_text_found_with_accents(page, page_result):
    assert _similarity(page.full_text(), page_result.text) >= 0.9
    for word in ("quittâmes", "rivière", "déjà", "église", "fîmes", "âgé", "vallée"):
        assert word in page_result.text, word


@needs_fra
def test_words_have_confidence_and_normalised_boxes(page, page_result):
    assert page_result.width == page.width and page_result.height == page.height
    assert page_result.languages == ["fra"] and page_result.engine == "tesseract"
    assert page_result.engine_version == ocr_service.tesseract_version()
    assert page_result.preparation == "gray" and page_result.mean_confidence > 80
    assert all(0 <= word.confidence <= 100 for word in page_result.words)
    truth = next(word for word in page.words if word.text == "lendemain")
    found = next(word for word in page_result.words if word.text == "lendemain")
    assert found.box.iou(page.box(truth.box_px)) > 0.6
    assert {(w.block_num, w.line_num) for w in page_result.words}.__len__() > 10


@needs_fra
def test_low_confidence_words_are_flagged_on_a_degraded_page(page, page_result):
    gray = cv2.cvtColor(page.image, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray, None, fx=0.38, fy=0.38, interpolation=cv2.INTER_AREA)
    rng = np.random.default_rng(4)
    noisy = small.astype(np.float32) * 0.45 + 120 + rng.normal(0, 14, small.shape)
    degraded = cv2.GaussianBlur(np.clip(noisy, 0, 255).astype(np.uint8), (0, 0), 0.9)
    result = ocr_service.run_ocr(degraded, ["fra"])
    low = [word for word in result.words if word.confidence < config.LOW_CONFIDENCE]
    assert result.words and low
    assert result.mean_confidence < page_result.mean_confidence


@needs_fra
def test_second_binarized_reading_when_confidence_is_low(page_gray, monkeypatch):
    calls = []
    original = ocr_service.TesseractEngine.recognize

    def recognize(self, gray, languages, psm=3):
        calls.append(gray.copy())
        result = original(self, gray, languages, psm)
        if len(calls) == 1:                    # première lecture « mauvaise »
            result.words = [word.model_copy(update={"confidence": 20.0}) for word in result.words]
        return result

    monkeypatch.setattr(ocr_service.TesseractEngine, "recognize", recognize)
    result = ocr_service.run_ocr(page_gray, ["fra"])
    assert len(calls) == 2 and set(np.unique(calls[1])) <= {0, 255}
    assert result.preparation == "binarized" and result.mean_confidence > 80


@needs_fra
def test_cache_serves_second_call_without_subprocess(page_gray, tmp_path, monkeypatch):
    first = ocr_service.run_ocr(page_gray, ["fra"], cache_dir=tmp_path)
    assert len(list(tmp_path.glob("*.json"))) == 1

    def no_subprocess(*args, **kwargs):
        raise AssertionError("Tesseract relancé alors que le résultat est en cache")

    monkeypatch.setattr(subprocess, "run", no_subprocess)
    second = ocr_service.run_ocr(page_gray, ["fra"], cache_dir=tmp_path)
    assert second == first


@needs_fra
def test_cache_key_depends_on_image_languages_psm_and_version(page_gray):
    key = ocr_service.cache_key(page_gray, ["fra"], 3, "5.5.3")
    changed = page_gray.copy()
    changed[0, 0] ^= 1
    others = {ocr_service.cache_key(changed, ["fra"], 3, "5.5.3"), ocr_service.cache_key(page_gray, ["eng"], 3, "5.5.3"),
              ocr_service.cache_key(page_gray, ["fra"], 6, "5.5.3"), ocr_service.cache_key(page_gray, ["fra"], 3, "5.4.0")}
    assert key not in others and len(others) == 4


@needs_fra
def test_corrupted_cache_is_recomputed(page_gray, tmp_path):
    first = ocr_service.run_ocr(page_gray, ["fra"], cache_dir=tmp_path)
    cache_file = next(tmp_path.glob("*.json"))
    cache_file.write_text("{pas du json", encoding="utf-8")
    assert ocr_service.run_ocr(page_gray, ["fra"], cache_dir=tmp_path) == first


@needs_fra
def test_ocr_region_reads_one_block(page, page_gray):
    heading = next(block for block in page.blocks if block.kind == "heading_1")
    text, confidence = ocr_service.ocr_region(page_gray, page.box(heading.box_px).expanded(0.01), ["fra"])
    assert _similarity(heading.text, text) >= 0.9 and confidence > 70


@needs_fra
def test_estimated_dpi_is_close_to_real(page_gray):
    assert 150 <= ocr_service.estimate_dpi(page_gray) <= 260


def test_missing_language_raises_with_install_commands():
    if not ocr_service.tesseract_version():
        pytest.skip("Tesseract absent")
    with pytest.raises(OcrUnavailableError) as error:
        ocr_service.check_languages(["fra", "xyz"])
    message = error.value.user_message
    assert "xyz" in message and "sudo pacman -S tesseract-data-xyz" in message
    assert "sudo apt install tesseract-ocr-xyz" in message
    with pytest.raises(OcrUnavailableError):
        ocr_service.run_ocr(np.full((50, 50), 255, np.uint8), ["xyz"])
    with pytest.raises(OcrUnavailableError):
        ocr_service.check_languages([])


def test_available_languages_exclude_osd():
    languages = ocr_service.available_languages()
    assert "osd" not in languages
    if ocr_service.tesseract_version():
        assert languages == sorted(languages) and languages


def test_missing_tesseract_is_reported(monkeypatch):
    monkeypatch.setattr(config, "TESSERACT_CMD", "/chemin/absent/tesseract")
    assert ocr_service.tesseract_version() is None and ocr_service.available_languages() == []
    assert ocr_service.run_osd(np.full((50, 50), 255, np.uint8)) is None
    with pytest.raises(OcrUnavailableError) as error:
        ocr_service.run_ocr(np.full((50, 50), 255, np.uint8), ["fra"])
    assert "Tesseract" in error.value.user_message


@needs_fra
def test_timeout_and_crash_become_ocr_failed_error(page_gray, monkeypatch):
    real_run = subprocess.run

    def slow(command, *args, **kwargs):
        if "tsv" in command:
            raise subprocess.TimeoutExpired(command, 1)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", slow)
    with pytest.raises(OcrFailedError) as error:
        ocr_service.run_ocr(page_gray, ["fra"])
    assert "délai" in error.value.user_message

    def crash(command, *args, **kwargs):
        if "tsv" in command:
            return subprocess.CompletedProcess(command, 1, "", "Error: image illisible")
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", crash)
    with pytest.raises(OcrFailedError) as error:
        ocr_service.run_ocr(page_gray, ["fra"])
    assert "image illisible" in error.value.detail


def test_parse_tsv_keeps_words_only():
    tsv = ("level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext\n"
           "1\t1\t0\t0\t0\t0\t0\t0\t200\t100\t-1\t\n"
           "4\t1\t1\t1\t1\t0\t10\t10\t180\t20\t-1\t\n"
           "5\t1\t1\t1\t1\t1\t10\t10\t50\t20\t96.5\tÉté\n"
           "5\t1\t1\t1\t1\t2\t70\t10\t40\t20\t41.0\tdéjà\n"
           "5\t1\t1\t1\t1\t3\t120\t10\t40\t20\t95\t \n")
    words = ocr_service.parse_tsv(tsv, 200, 100)
    assert [(w.text, w.confidence, w.word_num) for w in words] == [("Été", 96.5, 1), ("déjà", 41.0, 2)]
    box = words[0].box
    assert (box.x, box.y, box.w, box.h) == pytest.approx((0.05, 0.1, 0.25, 0.2))


def test_tesseract_engine_follows_the_protocol():
    engine: ocr_service.OcrEngine = ocr_service.TesseractEngine()
    assert engine.name == "tesseract" and callable(engine.recognize) and isinstance(engine.version(), str)
