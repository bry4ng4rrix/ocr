"""PDF du livre : validation de toutes les pages, PDF enregistré et retrouvé « à jour », création automatique
en fin de traitement, conservation des derniers exports, écran (un bouton, puis le téléchargement)."""
from __future__ import annotations

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from components import common
from models.scanning import ScanState
from repositories import scan_repository
from services import project_service, scanning_service
from tests.conftest import ROOT, requires_languages
from tests.fixtures import synthetic
from workers import scan_runner

pytestmark = [requires_languages("fra", "osd"), pytest.mark.tesseract, pytest.mark.slow]


@pytest.fixture
def book(data_dir, tmp_path):
    pages = [synthetic.on_dark_background(synthetic.text_page(), seed=2),
             synthetic.on_dark_background(synthetic.german_text_page(), seed=3)]
    project = scanning_service.create_book("Livre", ["fra"])
    scanning_service.import_book_files(project, [(f"p{index}.jpg", synthetic.save(page, tmp_path / str(index), ".jpg")
                                                  .read_bytes()) for index, page in enumerate(pages, start=1)])
    return project


def test_validate_all_validates_ready_pages_and_lists_the_others(book):
    first, second = book.pages
    scanning_service.process_page(book, first.id, use_ai=False)
    record = scanning_service.load_record(book, first)
    record.review_reasons = ["Orientation incertaine"]          # page « à vérifier »
    scanning_service.save_record(book, record)
    scan_repository.set_state(book.id, first.id, ScanState.REVIEW_REQUIRED)
    summary = scanning_service.validate_all(book)
    assert summary.validated == [1] and summary.not_ready == [2] and summary.already == []
    assert scan_repository.get_task(book.id, first.id).state == ScanState.COMPLETED
    assert scanning_service.load_record(book, first).validated
    assert scanning_service.validate_all(book).already == [1]


def test_saved_pdf_is_found_and_tracked_as_current(book):
    for page in book.pages:
        scanning_service.process_page(book, page.id, use_ai=False)
    assert scanning_service.latest_book_pdf(book) is None
    assert scanning_service.export_problems(book) == []
    scanning_service.export_book_pdf(book)
    latest = scanning_service.latest_book_pdf(book)
    assert latest is not None and not latest.automatic and latest.validation.passed
    assert scanning_service.book_pdf_path(book, latest).is_file()
    assert scanning_service.is_export_current(book, latest)
    scanning_service.validate_all(book)                             # pages à vérifier incluses : contenu identique
    assert scanning_service.is_export_current(book, latest)
    settings = scanning_service.load_book_settings(book)
    scanning_service.save_book_settings(book, settings.model_copy(update={"page_format": "image"}))
    assert not scanning_service.is_export_current(book, latest)    # option changée : PDF à refaire
    scanning_service.save_book_settings(book, settings)
    scanning_service.render_final(book, book.pages[0].id, scanning_service.load_record(book, book.pages[0])
                                  .adjustments.model_copy(update={"contrast": 5.0}))
    assert not scanning_service.is_export_current(book, latest)    # image finale changée


def test_only_the_latest_exports_are_kept(book, monkeypatch):
    monkeypatch.setattr("config.SCAN_KEEP_EXPORTS", 2)
    for page in book.pages:
        scanning_service.process_page(book, page.id, use_ai=False)
    for _ in range(3):
        scanning_service.export_book_pdf(book)
    exports = project_service.project_dir(book) / "exports"
    assert len(list(exports.glob("*numerisation*.pdf"))) == 2
    assert len(list(exports.glob("*numerisation*.export.json"))) == 2


def test_pdf_is_created_automatically_at_the_end_of_processing(book):
    scan_runner.run_book(book.id, use_ai=False)
    latest = scanning_service.latest_book_pdf(project_service.open_project(book.id))
    assert latest is not None and latest.automatic
    settings = scanning_service.load_book_settings(book)
    scanning_service.save_book_settings(book, settings.model_copy(update={"auto_export": False}))
    scan_repository.mark_pending(book.id, [book.pages[0].id])
    scan_runner.run_book(book.id, use_ai=False)
    assert scanning_service.latest_book_pdf(book).signature == latest.signature   # pas de nouveau PDF


def test_incomplete_book_is_not_exported_automatically(book):
    scanning_service.process_page(book, book.pages[0].id, use_ai=False)
    assert scanning_service.auto_export_if_ready(book) is None
    assert any("Page 2" in problem for problem in scanning_service.export_problems(book))


@pytest.mark.ui
def test_one_button_creates_the_pdf_then_download_appears(book):
    for page in book.pages:
        scanning_service.process_page(book, page.id, use_ai=False)
    st.cache_data.clear()
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=240)
    at.session_state["project_id"] = book.id
    at.run()
    at.switch_page(common.SCREEN_SMART_SCAN).run()
    assert not at.exception
    assert not at.get("download_button")                            # rien à télécharger avant la création
    at.button(key="pdf_create").click().run()
    assert not at.exception and not at.error
    assert [button.label for button in at.get("download_button")] == ["Télécharger le PDF"]
    assert scanning_service.latest_book_pdf(book) is not None
    at.button(key="validate_all").click().run()
    assert not at.exception
    assert [button.label for button in at.get("download_button")] == ["Télécharger le PDF"]


def test_book_without_ocr_then_with_ocr(book):
    import pymupdf

    settings = scanning_service.load_book_settings(book)
    scanning_service.save_book_settings(book, settings.model_copy(update={"run_ocr": False}))
    for page in book.pages:
        record = scanning_service.process_page(book, page.id, use_ai=False)
        assert record.ocr_skipped and record.ocr_file is None and record.error is None
        assert not any("Aucun texte" in reason for reason in record.review_reasons)
        assert "Pas d'OCR : PDF sans texte recherchable" in scanning_service.describe_changes(book, page, record)
    assert scanning_service.export_problems(book) == []
    report, validation = scanning_service.export_book_pdf(book)
    assert validation.passed and report.words_written == 0
    with pymupdf.open(report.output) as document:
        assert all(not page.get_text().strip() for page in document)       # aucune couche texte
    scanning_service.save_book_settings(book, settings.model_copy(update={"run_ocr": True}))
    assert all("retraitez" in problem for problem in scanning_service.export_problems(book))
    assert not scanning_service.is_export_current(book, scanning_service.latest_book_pdf(book))
    record = scanning_service.process_page(book, book.pages[0].id, use_ai=False)
    assert not record.ocr_skipped and record.ocr_file and record.quality.ocr_word_count > 10


def test_exports_in_the_same_second_do_not_overwrite_each_other(data_dir):
    from services import export_service

    project = project_service.create_project("Livre", ["fra"])
    first = export_service.export_path(project, ".pdf", "numerisation")
    first.write_bytes(b"%PDF")
    second = export_service.export_path(project, ".pdf", "numerisation")
    assert second != first and not second.exists()


def test_book_settings_page_formats_and_legacy_value():
    from models.scanning import ScanBookSettings

    assert ScanBookSettings.model_validate({"uniform_pages": False}).page_format == "image"
    assert ScanBookSettings.model_validate({"uniform_pages": True}).uniform_pages
    assert ScanBookSettings(page_format="A3").paper_size_mm == (297, 420)
    assert ScanBookSettings(page_format="custom", custom_width_mm=300, custom_height_mm=200).paper_size_mm == (200, 300)
    with pytest.raises(ValueError):
        ScanBookSettings(page_format="Z9")


def test_book_pdf_uses_the_chosen_format(book):
    import pymupdf

    for page in book.pages:
        scanning_service.process_page(book, page.id, use_ai=False)
    settings = scanning_service.load_book_settings(book)
    scanning_service.save_book_settings(book, settings.model_copy(update={"page_format": "A5", "orientation": "portrait"}))
    report, validation = scanning_service.export_book_pdf(book)
    assert validation.passed
    with pymupdf.open(report.output) as document:
        assert {(round(page.rect.width / 72 * 25.4), round(page.rect.height / 72 * 25.4)) for page in document} == {(148, 210)}
    latest = scanning_service.latest_book_pdf(book)
    scanning_service.save_book_settings(book, settings.model_copy(update={"page_format": "A4"}))
    assert not scanning_service.is_export_current(book, latest)       # format changé : PDF à mettre à jour


def test_bulk_quad_rotation_and_extra_trim(book):
    from models.geometry import Point
    from models.errors import ProcessingError

    for page in book.pages:
        scanning_service.process_page(book, page.id, use_ai=False)
    crossed = [Point(x=0.2, y=0.1), Point(x=0.8, y=0.9), Point(x=0.8, y=0.1), Point(x=0.2, y=0.9)]
    with pytest.raises(ProcessingError):
        scanning_service.apply_quad_to_pages(book, crossed)
    corners = [Point(x=0.15, y=0.08), Point(x=0.85, y=0.08), Point(x=0.85, y=0.92), Point(x=0.15, y=0.92)]
    assert len(scanning_service.apply_quad_to_pages(book, corners)) == 2
    reopened = project_service.open_project(book.id)
    assert all(page.geometry.manual_quad == corners for page in reopened.pages)
    assert scanning_service.apply_quad_to_pages(book, corners) == []              # rien à changer
    assert len(scanning_service.apply_quad_to_pages(book, None)) == 2
    assert all(page.geometry.manual_quad is None for page in project_service.open_project(book.id).pages)
    assert scanning_service.apply_rotation_to_pages(book, 2, [book.pages[0].id]) == [book.pages[0].id]
    assert project_service.open_project(book.id).page(book.pages[0].id).geometry.manual_quarter_turns == 2

    first = book.pages[1]
    automatic = scanning_service.load_record(book, first).adjustments
    assert scanning_service.apply_trim_to_pages(book, {"top": 0.03}, [first.id]) == [first.id]
    record = scanning_service.process_page(book, first.id, use_ai=False)      # retraitement : le rognage reste
    assert record.adjustments.extra_top == 0.03 and record.adjustments_source != "manual"
    assert (record.adjustments.crop_left, record.adjustments.crop_right) == (automatic.crop_left, automatic.crop_right)
    assert any("Rognage supplémentaire" in change for change in scanning_service.describe_changes(book, first, record))
    scanning_service.apply_trim_to_pages(book, None, [first.id])
    assert scanning_service.load_record(book, first).adjustments.extra_top == 0
