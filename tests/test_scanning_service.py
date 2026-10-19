"""Numérisation fidèle de bout en bout (projet réel sur disque temporaire, Tesseract, sans modèle Gemma).

Cas : page photographiée en perspective, échec de détection des bords, recadrage manuel, reprise après
interruption, relance des erreurs, conflit d'orientation signalé par Gemma (simulé), réglages manuels
conservés, export d'un PDF multipage recherchable, originaux intacts.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pymupdf
import pytest

from models.errors import ExportError, ProcessingError
from models.geometry import Point
from models.scanning import AnalysisRecord, ScanAdjustments, ScanState, VisualAnalysis
from repositories import scan_repository
from services import image_analysis_service, project_service, scanning_service
from tests.conftest import requires_languages, sha256
from tests.fixtures import synthetic
from tests.fixtures.fake_ollama import VALID_ANALYSIS
from workers import scan_runner

pytestmark = [requires_languages("fra", "osd"), pytest.mark.tesseract, pytest.mark.slow]


def _project(data_dir: Path, tmp_path: Path, pages: list[synthetic.SyntheticPage]):
    files = [synthetic.save(page, tmp_path / "photos", ".jpg") for page in pages]
    project = project_service.create_project("Livre test", languages=["fra"])
    project_service.import_paths(project, files)
    scanning_service.sync_queue(project)
    return project, files


@pytest.fixture
def photographed():
    return synthetic.on_dark_background(synthetic.text_page())


def test_photographed_page_is_straightened_ocr_and_exported(data_dir, tmp_path, photographed):
    project, files = _project(data_dir, tmp_path, [photographed])
    before = sha256(files[0])
    page = project.pages[0]
    record = scanning_service.process_page(project, page.id, use_ai=False)
    assert record.error is None
    page = project.page(page.id)
    assert page.geometry.detected_quad is not None and not page.geometry.quad_uncertain
    final = scanning_service.load_final(project, record)
    # Rapport largeur/hauteur apparent du quadrilatère sur la photo (arêtes maximales, comme warp_quad) :
    # le rapport physique exact demanderait la focale de l'appareil (docs/KNOWN_LIMITATIONS.md).
    h, w = photographed.image.shape[:2]
    tl, tr, br, bl = (np.array(corner) * [w, h] for corner in photographed.quad)
    apparent = max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)) / max(np.linalg.norm(bl - tl),
                                                                            np.linalg.norm(br - tr))
    assert final.shape[1] / final.shape[0] == pytest.approx(apparent, rel=0.03)
    assert final.mean(axis=(0, 1)).min() > 150                                       # fond sombre retiré
    ocr = scanning_service.load_ocr(project, record)
    assert len(ocr.words) > 30 and (ocr.width, ocr.height) == (final.shape[1], final.shape[0])
    assert scanning_service.is_current(page, record)
    task = scan_repository.get_task(project.id, page.id)
    assert task.state in (ScanState.COMPLETED, ScanState.REVIEW_REQUIRED) and task.attempts == 1
    report, validation = scanning_service.export_book_pdf(project)
    assert validation.passed, validation.issues
    assert Path(report.output).with_suffix(".validation.json").is_file()
    assert sha256(files[0]) == before                                               # original intact
    assert sha256(project_service.page_paths(project, page).original) == page.source_sha256


def test_edge_detection_failure_requires_review_without_transform(data_dir, tmp_path):
    flat = synthetic.text_page()                       # page qui remplit l'image : aucun contour de feuille
    project, _ = _project(data_dir, tmp_path, [flat])
    page = project.pages[0]
    record = scanning_service.process_page(project, page.id, use_ai=False)
    assert project.page(page.id).geometry.detected_quad is None
    assert any("contour" in reason.lower() for reason in record.review_reasons)
    assert scan_repository.get_task(project.id, page.id).state == ScanState.REVIEW_REQUIRED
    final = scanning_service.load_final(project, record)
    assert abs(final.shape[0] - flat.height) <= 0.05 * flat.height                 # image entière conservée


def test_manual_crop_is_validated_then_applied(data_dir, tmp_path, photographed):
    project, _ = _project(data_dir, tmp_path, [photographed])
    page = project.pages[0]
    crossed = [Point(x=0.2, y=0.1), Point(x=0.8, y=0.9), Point(x=0.8, y=0.1), Point(x=0.2, y=0.9)]
    with pytest.raises(ProcessingError, match="Recadrage refusé"):
        scanning_service.set_manual_quad(project, page.id, crossed)
    tiny = [Point(x=0.5, y=0.5), Point(x=0.55, y=0.5), Point(x=0.55, y=0.55), Point(x=0.5, y=0.55)]
    with pytest.raises(ProcessingError):
        scanning_service.set_manual_quad(project, page.id, tiny)
    corners = [Point(x=x, y=y) for x, y in photographed.quad]
    record = scanning_service.set_manual_quad(project, page.id, corners)
    reopened = project_service.open_project(project.id)
    assert reopened.page(page.id).geometry.manual_quad == corners
    assert record.final_file and record.error is None
    assert not any("contour" in reason.lower() for reason in record.review_reasons)


def test_resume_after_interruption_skips_completed_pages(data_dir, tmp_path, monkeypatch):
    pages = [synthetic.text_page(), synthetic.german_text_page(), synthetic.two_column_page()]
    project, _ = _project(data_dir, tmp_path, pages)
    ids = [page.id for page in project.pages]
    scan_repository.set_state(project.id, ids[0], ScanState.COMPLETED)
    scan_repository.set_state(project.id, ids[1], ScanState.PROCESSING, count_attempt=True)   # arrêt brutal
    calls: list[str] = []

    def fake_process(project, page_id, **kwargs):
        calls.append(page_id)
        scan_repository.set_state(project.id, page_id, ScanState.COMPLETED)
        return scanning_service.load_record(project, project.page(page_id))

    monkeypatch.setattr(scanning_service, "process_page", fake_process)
    processed, failed = scan_runner.run_book(project.id)
    assert calls == ids[1:] and (processed, failed) == (2, 0)
    assert scan_repository.progress(project.id).counts == {ScanState.COMPLETED: 3}
    calls.clear()
    scan_runner.run_book(project.id)                    # tout est fait : rien n'est refait
    assert calls == []


def test_repeatedly_interrupted_page_fails_and_can_be_retried(data_dir, tmp_path, monkeypatch):
    project, _ = _project(data_dir, tmp_path, [synthetic.text_page()])
    page_id = project.pages[0].id
    for _ in range(3):
        scan_repository.set_state(project.id, page_id, ScanState.PROCESSING, count_attempt=True)
        scan_repository.recover_interrupted(project.id)
    monkeypatch.setattr(scanning_service, "process_page", lambda project, page_id, **kw: pytest.fail("ne doit pas tourner"))
    scan_runner.run_book(project.id)
    assert scan_repository.get_task(project.id, page_id).state == ScanState.FAILED
    calls = []
    monkeypatch.setattr(scanning_service, "process_page",
                        lambda project, page_id, **kw: calls.append(page_id) or scanning_service.load_record(
                            project, project.page(page_id)))
    scan_runner.run_book(project.id, retry_failed=True)
    assert calls == [page_id]


def test_gemma_orientation_conflict_requires_review_without_rotation(data_dir, tmp_path, photographed, monkeypatch):
    project, _ = _project(data_dir, tmp_path, [photographed])
    analysis = VisualAnalysis.model_validate({**VALID_ANALYSIS, "orientation_issue": "upside_down",
                                              "recommended_actions": ["ROTATE", "SHADOW_CORRECTION"]})
    monkeypatch.setattr(image_analysis_service, "analyse_page",
                        lambda rgb, cache_dir, force=False: AnalysisRecord(status="ok", analysis=analysis,
                                                                           model="gemma3:4b"))
    page = project.pages[0]
    record = scanning_service.process_page(project, page.id, use_ai=True, auto_apply=False)
    assert project.page(page.id).geometry.manual_quarter_turns == 0
    assert any("Gemma voit la page" in reason for reason in record.review_reasons)
    assert scan_repository.get_task(project.id, page.id).state == ScanState.REVIEW_REQUIRED
    assert record.adjustments_source in ("neutral", "measured")                     # Gemma n'a rien appliqué


def test_invalid_ai_answer_falls_back_to_measured_processing(data_dir, tmp_path, photographed, monkeypatch):
    project, _ = _project(data_dir, tmp_path, [photographed])
    monkeypatch.setattr(image_analysis_service, "analyse_page",
                        lambda rgb, cache_dir, force=False: AnalysisRecord(status="invalid_response",
                                                                           error="schéma"))
    record = scanning_service.process_page(project, project.pages[0].id, use_ai=True, auto_apply=True)
    assert record.error is None and record.final_file and record.proposed_plan is None
    assert record.analysis.status == "invalid_response"


def test_manual_settings_survive_reprocessing_and_can_be_undone(data_dir, tmp_path, photographed):
    project, _ = _project(data_dir, tmp_path, [photographed])
    page_id = project.pages[0].id
    scanning_service.process_page(project, page_id, use_ai=False)
    manual = ScanAdjustments(exposure=5, contrast=8)
    scanning_service.render_final(project, page_id, manual)
    record = scanning_service.process_page(project, page_id, use_ai=False)
    assert record.adjustments == manual and record.adjustments_source == "manual"
    previous = scanning_service.restore_previous(project, page_id)
    assert previous.adjustments != manual
    neutral = scanning_service.reset_to_original(project, page_id)
    assert neutral.adjustments.is_neutral() and neutral.final_file


def test_export_requires_every_page_and_keeps_order(data_dir, tmp_path):
    pages = [synthetic.on_dark_background(synthetic.text_page(), seed=2),
             synthetic.on_dark_background(synthetic.german_text_page(), seed=3)]
    project, _ = _project(data_dir, tmp_path, pages)
    first, second = project.pages
    scanning_service.process_page(project, first.id, use_ai=False)
    with pytest.raises(ExportError, match="Page 2"):
        scanning_service.export_book_pdf(project)
    scanning_service.process_page(project, second.id, use_ai=False)
    project_service.reorder_pages(project, [second.id, first.id])
    report, validation = scanning_service.export_book_pdf(project)
    assert report.page_count == 2 and validation.passed, validation.issues
    with pymupdf.open(report.output) as document:
        first_page_text = document[0].get_text()
    second_record = scanning_service.load_record(project, project.page(second.id))
    second_words = {word.text for word in scanning_service.load_ocr(project, second_record).words if len(word.text) > 6}
    assert any(word in first_page_text for word in second_words)           # ordre du livre respecté
    record = scanning_service.validate_page(project, first.id)
    assert record.validated
    assert scan_repository.get_task(project.id, first.id).state == ScanState.COMPLETED


def test_validate_refuses_unprocessed_page(data_dir, tmp_path):
    project, _ = _project(data_dir, tmp_path, [synthetic.text_page()])
    with pytest.raises(ProcessingError):
        scanning_service.validate_page(project, project.pages[0].id)


def test_quad_validation_rules():
    good = [Point(x=0.1, y=0.1), Point(x=0.9, y=0.12), Point(x=0.88, y=0.9), Point(x=0.12, y=0.88)]
    assert scanning_service.validate_quad(good) == []
    assert scanning_service.validate_quad(good[:3])
    skewed = [Point(x=0.1, y=0.1), Point(x=0.9, y=0.1), Point(x=0.15, y=0.2), Point(x=0.1, y=0.9)]
    assert scanning_service.validate_quad(skewed)


def test_rescanning_keeps_docx_analysis_status(data_dir, tmp_path, photographed, monkeypatch):
    from models import PageStatus
    from services import processing_service

    project, _ = _project(data_dir, tmp_path, [photographed])
    page_id = project.pages[0].id
    scanning_service.process_page(project, page_id, use_ai=False)
    page = project.page(page_id).model_copy(deep=True)
    page.status = PageStatus.ANALYSED                       # page déjà analysée par le pipeline DOCX
    processing_service.store_page(project, page)
    calls = []
    original = processing_service.process_page
    monkeypatch.setattr(processing_service, "process_page",
                        lambda *args, **kwargs: calls.append(1) or original(*args, **kwargs))
    scanning_service.process_page(project, page_id, use_ai=False)
    assert calls == []                                      # géométrie réutilisée, pas recalculée
    scanning_service.process_page(project, page_id, use_ai=False, regeometry=True)
    assert calls == [1]
    assert project_service.open_project(project.id).page(page_id).status == PageStatus.ANALYSED
