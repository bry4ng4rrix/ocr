"""Qualité mesurable et routage A–E, sans modèle IA."""
from models import Box, ImageBlock, ImageKind, OcrResult, Page, PageStatus, Project, TextBlock
from models.quality import RouteBand
from services import gemini_service, ollama_service, processing_service, project_service, quality_service, routing_service


def _page(**kwargs) -> Page:
    page = Page(source_file="originals/a.jpg", source_sha256="abc", original_name="a.jpg",
                status=PageStatus.ANALYSED, mean_confidence=90,
                text_blocks=[TextBlock(box=Box(x=0.1, y=0.1, w=0.8, h=0.2), ocr_text="Bonjour")])
    return page.model_copy(update=kwargs)


def test_reliable_page_is_band_a():
    quality = quality_service.evaluate_page(_page())
    assert quality.band == RouteBand.RELIABLE
    assert not quality.empty_text


def test_low_confidence_is_band_c():
    quality = quality_service.evaluate_page(_page(mean_confidence=40))
    assert quality.band == RouteBand.COMPLEX


def test_empty_analysed_page_requires_review():
    quality = quality_service.evaluate_page(_page(text_blocks=[]))
    assert quality.band == RouteBand.REVIEW_REQUIRED
    assert quality.empty_text


def test_imported_page_is_not_reliable():
    quality = quality_service.evaluate_page(_page(status=PageStatus.IMPORTED, text_blocks=[], mean_confidence=None))
    assert quality.band == RouteBand.DOUBTFUL


def test_missing_illustration_file_requires_review(tmp_path):
    page = _page(image_blocks=[ImageBlock(kind=ImageKind.PHOTO, box=Box(x=0.2, y=0.4, w=0.5, h=0.4),
                                         file="images/missing.png")])
    quality = quality_service.evaluate_page(page, project_dir=tmp_path)
    assert quality.band == RouteBand.REVIEW_REQUIRED
    assert quality.missing_illustration_files == 1


def test_expected_page_count_mismatch():
    project = Project(name="Livre", expected_page_count=10, pages=[_page()])
    report = quality_service.evaluate_book(project)
    assert any(issue.code == "quality_page_count" for issue in report.issues)


def test_routing_without_ai_sends_complex_to_review():
    project = Project(name="Livre", ollama_allowed=False, cloud_ai_allowed=False)
    page = _page(mean_confidence=40)
    quality = routing_service.route_page(project, page)
    assert quality.band == RouteBand.REVIEW_REQUIRED
    assert page.review_required
    assert page.route_band == "E"


def test_ollama_disabled_by_default():
    assert ollama_service.is_enabled() is False
    status = ollama_service.status()
    assert status.available is False


def test_gemini_disabled_without_key(monkeypatch):
    monkeypatch.setattr("config.GEMINI_ENABLED", True)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    project = Project(name="Livre", cloud_ai_allowed=True)
    status = gemini_service.status(project)
    assert status.available is False
    assert "GEMINI_API_KEY" in status.message


def test_ocr_archive_keeps_previous_result(data_dir):
    project = project_service.create_project("Archive")
    page = _page()
    project.pages.append(page)
    project_service.save_project(project)
    path = processing_service.ocr_result_path(project, page)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(OcrResult(words=[], width=10, height=10, engine="tesseract").model_dump_json(),
                    encoding="utf-8")
    archived = processing_service.archive_ocr_result(project, page)
    assert archived is not None and archived.is_file()
    assert path.is_file()
    assert archived.read_text(encoding="utf-8") == path.read_text(encoding="utf-8")
