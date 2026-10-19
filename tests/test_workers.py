"""Tests unitaires pour l'ordonnanceur de traitement par lots (workers/job_runner)."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest

from models import Page, PageStatus, Project
from tests.fixtures import synthetic
from workers.job_runner import BatchConfig, BatchWorkerReport, PageProcessingState, is_worker_running, run_batch_pipeline
from services import project_service
from services.processing_service import Step


def _create_test_project(tmp_path: Path) -> Project:
    project = project_service.create_project("Livre Worker", ["fra"])
    # Ajouter deux pages synthétiques
    src_folder = tmp_path / "src"
    src_folder.mkdir(parents=True, exist_ok=True)
    p1_path = synthetic.save(synthetic.text_page(), src_folder, ".jpg")
    p2_path = synthetic.save(synthetic.photo_text_page(), src_folder, ".jpg")

    report = project_service.import_files(
        project,
        [(p1_path.name, p1_path.read_bytes()), (p2_path.name, p2_path.read_bytes())],
    )
    assert len(report.added_page_ids) == 2
    return project_service.open_project(project.id)


def test_batch_worker_execution(tmp_path, data_dir):
    project = _create_test_project(tmp_path)
    progress_updates = []

    def on_progress(done, total, msg):
        progress_updates.append((done, total, msg))

    config = BatchConfig(batch_size=1, steps=[Step.GEOMETRY])
    report = run_batch_pipeline(project, batch_config=config, progress_callback=on_progress)

    assert report.total_pages == 2
    assert report.completed_pages + report.review_required_pages == 2
    assert report.failed_pages == 0
    assert not report.interrupted
    assert len(progress_updates) == 2
    assert progress_updates[-1][0] == 2


def test_batch_worker_cancellation(tmp_path, data_dir):
    project = _create_test_project(tmp_path)
    cancel_event = threading.Event()
    cancel_event.set()  # Annulation immédiate

    report = run_batch_pipeline(project, cancel_event=cancel_event)
    assert report.interrupted is True
    assert report.completed_pages == 0


def test_page_processing_state_enum():
    assert PageProcessingState.PENDING == "pending"
    assert PageProcessingState.PROCESSING == "processing"
    assert PageProcessingState.COMPLETED == "completed"
    assert PageProcessingState.FAILED == "failed"
