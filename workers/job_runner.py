"""Ordonnanceur de tâches de traitement par lots (Batch Worker) avec gestion de RAM, reprise et journalisation."""
from __future__ import annotations

import gc
import logging
import threading
import time
import uuid
from collections.abc import Callable
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

import config
from models import Issue, Page, PageStatus, Project, Severity
from models.quality import RouteBand
from repositories import job_repository
from services import processing_service, project_service, quality_service, routing_service
from services.processing_service import ALL_STEPS, STEP_LABELS, Step

logger = logging.getLogger("bookocr.worker")


class PageProcessingState(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    REVIEW_REQUIRED = "review_required"
    COMPLETED = "completed"
    FAILED = "failed"


class BatchConfig(BaseModel):
    batch_size: int = Field(default=20, ge=1, le=100)
    max_retries: int = Field(default=2, ge=0, le=5)
    redetect: bool = False
    steps: list[Step] = Field(default_factory=lambda: list(ALL_STEPS))


class BatchWorkerReport(BaseModel):
    job_id: str
    book_id: str
    total_pages: int
    completed_pages: int = 0
    failed_pages: int = 0
    review_required_pages: int = 0
    step_durations_s: dict[str, float] = Field(default_factory=dict)
    errors_by_page: dict[str, str] = Field(default_factory=dict)
    interrupted: bool = False


_active_workers: dict[str, threading.Thread] = {}
_cancel_events: dict[str, threading.Event] = {}
_worker_lock = threading.Lock()


def is_worker_running(book_id: str) -> bool:
    with _worker_lock:
        thread = _active_workers.get(book_id)
        return thread is not None and thread.is_alive()


def request_cancel(book_id: str) -> bool:
    with _worker_lock:
        event = _cancel_events.get(book_id)
        if event is not None:
            event.set()
            return True
        return False


def run_batch_pipeline(
    project: Project,
    page_ids: list[str] | None = None,
    *,
    batch_config: BatchConfig | None = None,
    job_id: str | None = None,
    cancel_event: threading.Event | None = None,
    progress_callback: Callable[[int, int, str], None] | None = None,
) -> BatchWorkerReport:
    """Exécute le pipeline par petits lots isolés avec suivi strict de mémoire et de statut SQLite."""
    cfg = batch_config or BatchConfig()
    target_ids = list(page_ids if page_ids is not None else [p.id for p in project.pages])
    total = len(target_ids)
    jid = job_id or uuid.uuid4().hex[:12]

    # Enregistrer la tâche dans SQLite
    job_repository.create_job(project.id, total_pages=total, job_id=jid, current_step="init")

    report = BatchWorkerReport(job_id=jid, book_id=project.id, total_pages=total)
    by_id = {page.id: page for page in project.pages}

    # Découpage en sous-lots (ex: 20 pages par lot)
    step_keys = [s.value for s in cfg.steps]
    for key in step_keys:
        report.step_durations_s[key] = 0.0

    done_count = 0

    for i in range(0, total, cfg.batch_size):
        chunk_ids = target_ids[i:i + cfg.batch_size]

        for pid in chunk_ids:
            if cancel_event is not None and cancel_event.is_set():
                report.interrupted = True
                job_repository.update_job(jid, state="cancelled", done_pages=done_count)
                logger.info("Traitement du livre %s annulé par l'utilisateur", project.id)
                return report

            page = by_id.get(pid)
            if page is None:
                continue

            page_state = PageProcessingState.PROCESSING
            job_repository.update_job(jid, done_pages=done_count, current_step="page_" + page.id)

            try:
                # 1. Étape Géométrie
                if Step.GEOMETRY in cfg.steps:
                    t0 = time.monotonic()
                    geo_issues = processing_service.run_geometry(project, page, redetect=cfg.redetect)
                    page.issues = geo_issues
                    report.step_durations_s[Step.GEOMETRY.value] += time.monotonic() - t0

                # 2. Étape OCR
                if Step.OCR in cfg.steps:
                    t0 = time.monotonic()
                    ocr_issues = processing_service.run_ocr(project, page)
                    page.issues = [iss for iss in page.issues if iss.code.startswith("geometry_")] + ocr_issues
                    report.step_durations_s[Step.OCR.value] += time.monotonic() - t0

                # 3. Étape Mise en page & Routage
                if Step.LAYOUT in cfg.steps:
                    t0 = time.monotonic()
                    merged, layout_issues = processing_service.run_layout(project, page)
                    # Mettre à jour l'objet page
                    page.text_blocks = merged.text_blocks
                    page.image_blocks = merged.image_blocks
                    page.background_regions = merged.background_regions
                    page.background_files = merged.background_files
                    page.columns = merged.columns
                    page.content_box = merged.content_box
                    page.status = merged.status
                    page.route_band = merged.route_band
                    page.review_required = merged.review_required
                    page.issues += layout_issues
                    report.step_durations_s[Step.LAYOUT.value] += time.monotonic() - t0

                # Contrôle qualité final de la page
                quality = quality_service.evaluate_page(page, project_dir=project_service.project_dir(project))
                routing_service.apply_route(page, quality)

                if page.review_required or quality.band == RouteBand.REVIEW_REQUIRED:
                    page_state = PageProcessingState.REVIEW_REQUIRED
                    report.review_required_pages += 1
                else:
                    page_state = PageProcessingState.COMPLETED
                    report.completed_pages += 1

                page.error = None

            except Exception as error:
                logger.exception("Erreur sur la page %s du livre %s: %s", pid, project.id, error)
                page.status = PageStatus.ERROR
                page.error = str(error)
                page_state = PageProcessingState.FAILED
                report.failed_pages += 1
                report.errors_by_page[pid] = str(error)

            finally:
                done_count += 1
                # Libération explicite de mémoire après chaque page
                gc.collect()

                if progress_callback is not None:
                    progress_callback(done_count, total, f"Page {done_count}/{total} ({page_state.value})")

        # Sauvegarde atomique du projet après chaque lot
        project_service.save_project(project)
        job_repository.update_job(jid, done_pages=done_count, current_step=f"batch_{i // cfg.batch_size + 1}")

    # Finalisation
    final_state = "failed" if report.failed_pages == total and total > 0 else "completed"
    job_repository.update_job(jid, state=final_state, done_pages=done_count)
    return report


def start_background_batch(
    project_id: str,
    page_ids: list[str] | None = None,
    *,
    batch_config: BatchConfig | None = None,
) -> str:
    """Démarre un worker de traitement par lots dans un thread isolé avec verrou global."""
    with _worker_lock:
        if is_worker_running(project_id):
            raise RuntimeError(f"Un traitement est déjà en cours pour le livre {project_id}")
        cancel_event = threading.Event()
        _cancel_events[project_id] = cancel_event
        job_id = uuid.uuid4().hex[:12]

        def _worker():
            try:
                proj = project_service.open_project(project_id)
                run_batch_pipeline(proj, page_ids, batch_config=batch_config, job_id=job_id, cancel_event=cancel_event)
            finally:
                with _worker_lock:
                    _active_workers.pop(project_id, None)
                    _cancel_events.pop(project_id, None)

        thread = threading.Thread(target=_worker, name=f"batch_worker_{project_id}", daemon=True)
        _active_workers[project_id] = thread
        thread.start()
        return job_id
