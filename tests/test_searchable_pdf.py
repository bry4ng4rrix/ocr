"""PDF fidèle recherchable : photographie visible (JPEG inséré tel quel), mots OCR en texte invisible.

Les OcrResult sont construits avec des boîtes exactes (pas besoin de Tesseract) ; un test de bout en bout
avec Tesseract est conditionné à sa présence.
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
import pymupdf
import pytest
from PIL import Image, ImageDraw, ImageFont

from models.errors import ExportError
from models.geometry import Box
from models.ocr import OcrResult, OcrWord
from models.scanning import SearchablePageInput
from services import fonts, image_service, ocr_service, pdf_export
from tests.conftest import requires_languages

WIDTH, HEIGHT = 1200, 1600


def _page(tmp_path: Path, name: str, words: list[str], color=(200, 40, 30)) -> SearchablePageInput:
    """Image couleur avec des mots dessinés à des positions connues ; OCR « parfait » associé."""
    image = Image.new("RGB", (WIDTH, HEIGHT), (238, 232, 220))
    draw = ImageDraw.Draw(image)
    draw.rectangle((100, 1100, 1100, 1450), fill=color)                  # aplat coloré (illustration)
    font = ImageFont.truetype(str(fonts.font_file("Liberation Serif", False, False)), 48)
    ocr_words = []
    x, y = 100, 150
    for index, word in enumerate(words):
        left, top, right, bottom = draw.textbbox((x, y), word, font=font)
        draw.text((x, y), word, font=font, fill=(20, 20, 20))
        ocr_words.append(OcrWord(text=word, confidence=95, block_num=1, par_num=1, line_num=index // 3 + 1,
                                 word_num=index % 3 + 1,
                                 box=Box(x=left / WIDTH, y=top / HEIGHT, w=(right - left) / WIDTH,
                                         h=(bottom - top) / HEIGHT)))
        x = right + 40
        if index % 3 == 2:
            x, y = 100, y + 90
    image_path = tmp_path / f"{name}.jpg"
    image.save(image_path, "JPEG", quality=92, subsampling=0)
    ocr_path = tmp_path / f"{name}.json"
    ocr_path.write_text(OcrResult(words=ocr_words, width=WIDTH, height=HEIGHT).model_dump_json(), encoding="utf-8")
    return SearchablePageInput(page_id=name, image_path=str(image_path), ocr_path=str(ocr_path))


@pytest.fixture
def book(tmp_path) -> list[SearchablePageInput]:
    return [_page(tmp_path, "p1", ["Première", "Größe", "Straße", "Mädchen", "œuvre", "Kapitel"]),
            _page(tmp_path, "p2", ["Deuxième", "Gemeinde", "Lenkung", "Valais"], color=(30, 120, 60)),
            _page(tmp_path, "p3", ["Troisième", "Siedler", "Spuren"], color=(40, 60, 200))]


def test_single_page_has_invisible_searchable_layer(tmp_path, book):
    output = tmp_path / "un.pdf"
    report = pdf_export.build_searchable_pdf(book[:1], output, dpi=300)
    assert report.page_count == 1 and report.words_written == 6 and not report.issues
    with pymupdf.open(output) as document:
        page = document[0]
        assert page.rotation == 0
        assert page.rect.width == pytest.approx(WIDTH * 72 / 300) and page.rect.height == pytest.approx(HEIGHT * 72 / 300)
        assert all(span["type"] == 3 for span in page.get_texttrace())          # rendu 3 : invisible
        assert "Größe" in page.get_text() and "œuvre" in page.get_text()
        for word in ("Größe", "Straße", "Mädchen", "œuvre"):
            assert page.search_for(word), word


def test_invisible_words_are_aligned_with_printed_words(tmp_path, book):
    output = tmp_path / "align.pdf"
    pdf_export.build_searchable_pdf(book[:1], output, dpi=300)
    ocr = OcrResult.model_validate_json(Path(book[0].ocr_path).read_text(encoding="utf-8"))
    with pymupdf.open(output) as document:
        page = document[0]
        for word in ocr.words:
            hit = page.search_for(word.text)[0]
            expected = pymupdf.Rect(word.box.x * page.rect.width, word.box.y * page.rect.height,
                                    word.box.right * page.rect.width, word.box.bottom * page.rect.height)
            assert abs(hit.x0 - expected.x0) < 1.5 and abs(hit.x1 - expected.x1) < 1.5, word.text
            assert (hit & expected).get_area() > 0.6 * expected.get_area(), word.text


def test_text_layer_does_not_change_the_rendering(tmp_path, book):
    with_text, without_text = tmp_path / "a.pdf", tmp_path / "b.pdf"
    pdf_export.build_searchable_pdf(book[:1], with_text)
    pdf_export.build_searchable_pdf([book[0].model_copy(update={"ocr_path": None})], without_text)
    renders = []
    for path in (with_text, without_text):
        with pymupdf.open(path) as document:
            pixmap = document[0].get_pixmap(dpi=72)
            renders.append(np.frombuffer(pixmap.samples, np.uint8).copy())
    assert np.array_equal(*renders)


def test_multipage_pdf_keeps_order_and_jpeg_bytes(tmp_path, book):
    output = tmp_path / "livre.pdf"
    report = pdf_export.build_searchable_pdf(book, output)
    assert report.page_count == 3
    with pymupdf.open(output) as document:
        assert [w in document[i].get_text() for i, w in enumerate(["Première", "Deuxième", "Troisième"])] == [True] * 3
        for page, item in zip(document, book):
            xref = page.get_images(full=True)[0][0]
            assert document.extract_image(xref)["image"] == Path(item.image_path).read_bytes()
    validation = pdf_export.validate_searchable_pdf(output, book)
    assert validation.passed, validation.issues
    assert validation.page_count_ok and validation.pages_with_text == 3
    assert validation.invisible_text_only and validation.images_unrecompressed
    assert validation.max_color_delta is not None and validation.max_color_delta < 3
    assert validation.probes and all(probe.found for probe in validation.probes)


def test_search_finds_known_words_across_book(tmp_path, book):
    output = tmp_path / "recherche.pdf"
    pdf_export.build_searchable_pdf(book, output)
    with pymupdf.open(output) as document:
        hits = {word: [index for index, page in enumerate(document) if page.search_for(word)]
                for word in ("Gemeinde", "Siedler", "Kapitel", "absent")}
    assert hits == {"Gemeinde": [1], "Siedler": [2], "Kapitel": [0], "absent": []}


def test_page_without_ocr_is_reported(tmp_path, book):
    pages = [book[0], book[1].model_copy(update={"ocr_path": None})]
    report = pdf_export.build_searchable_pdf(pages, tmp_path / "trou.pdf")
    assert report.page_count == 2 and report.pages_without_text == [1]
    assert any(issue.code == "scan_pdf_no_text" for issue in report.issues)


def test_validation_detects_visible_text_and_wrong_page_count(tmp_path, book):
    output = tmp_path / "v.pdf"
    pdf_export.build_searchable_pdf(book[:2], output)
    with pymupdf.open(output) as document:
        document[0].insert_text((50, 50), "visible", fontsize=12)
        document.save(tmp_path / "w.pdf")
    validation = pdf_export.validate_searchable_pdf(tmp_path / "w.pdf", book)
    assert not validation.passed and not validation.page_count_ok and not validation.invisible_text_only


def test_empty_book_is_refused(tmp_path):
    with pytest.raises(ExportError):
        pdf_export.build_searchable_pdf([], tmp_path / "vide.pdf")


def test_non_jpeg_image_is_refused(tmp_path):
    path = tmp_path / "x.png"
    Image.new("RGB", (100, 100), "white").save(path)
    with pytest.raises(ExportError):
        pdf_export.build_searchable_pdf([SearchablePageInput(page_id="x", image_path=str(path))], tmp_path / "x.pdf")


@requires_languages("fra")
def test_end_to_end_with_tesseract(tmp_path):
    page = _page(tmp_path, "t", ["Bonjour", "chapitre", "histoire", "village", "école", "rivière"])
    rgb = image_service.read_image(Path(page.image_path))
    result = ocr_service.run_ocr(image_service.prepare_ocr_image(rgb), ["fra"])
    ocr_path = tmp_path / "t_tess.json"
    ocr_path.write_text(result.model_dump_json(), encoding="utf-8")
    item = page.model_copy(update={"ocr_path": str(ocr_path)})
    output = tmp_path / "tess.pdf"
    pdf_export.build_searchable_pdf([item], output)
    validation = pdf_export.validate_searchable_pdf(output, [item])
    assert validation.passed, validation.issues
    with pymupdf.open(output) as document:
        assert document[0].search_for("chapitre") and document[0].search_for("village")
