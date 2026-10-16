"""Pipeline par page, cache OCR, reprise après erreur, traitement en arrière-plan."""
import subprocess

import pytest

from models import PageStatus
from services import processing_service, project_service
from services.processing_service import Step
from tests.conftest import requires_languages, sha256
from tests.fixtures import synthetic

pytestmark = [requires_languages("fra"), pytest.mark.tesseract, pytest.mark.slow]


def _project_with(pages, tmp_path):
    project = project_service.create_project("Essai", ["fra"])
    files = [(path.name, path.read_bytes()) for path in (synthetic.save(page, tmp_path / "src", ".jpg") for page in pages)]
    report = project_service.import_files(project, files)
    assert len(report.added_page_ids) == len(pages)
    return project


def test_full_pipeline_photo_page(data_dir, tmp_path):
    project = _project_with([synthetic.photo_text_page()], tmp_path)
    page = project.pages[0]
    original = project_service.page_paths(project, page).original
    before = sha256(original)

    report = processing_service.process_pages(project, None, processing_service.ALL_STEPS)

    assert report.failed == {} and report.processed == [page.id]
    reloaded = project_service.open_project(project.id)
    done = reloaded.page(page.id)
    assert done.status == PageStatus.ANALYSED
    assert done.text_blocks and len(done.image_blocks) >= 1
    root = project_service.project_dir(project)
    assert all((root / block.file).exists() for block in done.image_blocks)
    assert processing_service.ocr_result_path(reloaded, done).exists()
    assert sha256(original) == before


def test_ocr_cache_avoids_second_recognition(data_dir, tmp_path, monkeypatch):
    project = _project_with([synthetic.text_page()], tmp_path)
    page_id = project.pages[0].id
    processing_service.process_pages(project, [page_id], [Step.GEOMETRY, Step.OCR])

    def forbidden(*args, **kwargs):
        if args and "tesseract" in str(args[0][0]):
            raise AssertionError("tesseract relancé alors que le résultat était en cache")
        return original_run(*args, **kwargs)

    original_run = subprocess.run
    monkeypatch.setattr(subprocess, "run", forbidden)
    report = processing_service.process_pages(project, [page_id], [Step.OCR])
    assert report.failed == {}


def test_batch_continues_after_page_error(data_dir, tmp_path):
    project = _project_with([synthetic.text_page(), synthetic.german_text_page()], tmp_path)
    broken, healthy = project.pages
    project_service.page_paths(project, broken).original.write_bytes(b"illisible")   # copie de travail du test

    report = processing_service.process_pages(project, None, [Step.GEOMETRY])

    assert broken.id in report.failed and healthy.id in report.processed
    reloaded = project_service.open_project(project.id)
    assert reloaded.page(broken.id).status == PageStatus.ERROR
    assert reloaded.page(broken.id).error
    assert reloaded.page(healthy.id).status == PageStatus.CORRECTED
    assert processing_service.pages_needing(reloaded, Step.GEOMETRY) == [broken.id]


def test_user_corrections_survive_relaunch(data_dir, tmp_path):
    project = _project_with([synthetic.text_page()], tmp_path)
    page_id = project.pages[0].id
    processing_service.process_pages(project, None, processing_service.ALL_STEPS)
    page = project.page(page_id)
    block = page.text_blocks[0]
    block.corrected_text = "Texte corrigé à la main"
    block.user_modified = True
    project_service.update_page(project, page, reason="test")

    processing_service.process_pages(project, None, [Step.LAYOUT])

    texts = [b.text for b in project_service.open_project(project.id).page(page_id).text_blocks]
    assert "Texte corrigé à la main" in texts


def test_background_job_and_concurrent_edit(data_dir, tmp_path):
    project = _project_with([synthetic.text_page(), synthetic.two_column_page()], tmp_path)
    first, second = project.pages
    # modification faite « dans l'interface » sur la seconde page pendant que le job tourne
    editing = project_service.open_project(project.id)
    editing.page(second.id).enhancement.contrast = 12
    project_service.update_page(editing, editing.page(second.id))

    processing_service.start_background_job(project.id, [first.id], [Step.GEOMETRY])
    status = processing_service.wait_for_job(project.id, timeout=300)

    assert status.state == "done" and status.done == 1
    reloaded = project_service.open_project(project.id)
    assert reloaded.page(first.id).status == PageStatus.CORRECTED
    assert reloaded.page(second.id).enhancement.contrast == 12


def test_interrupted_job_is_reported(data_dir, tmp_path):
    project = _project_with([synthetic.text_page()], tmp_path)
    from models import JobStatus
    from models.project import utc_now
    processing_service._write_job(JobStatus(job_id="x", project_id=project.id, state="running",
                                            page_ids=[project.pages[0].id], started_at=utc_now()))
    assert processing_service.job_status(project.id).state == "interrupted"
