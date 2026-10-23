"""Traitement de numérisation en arrière-plan : file SQLite, reprise, annulation, un livre après l'autre.

Un seul worker de numérisation à la fois dans le processus ; il traite `config.SCAN_WORKERS` pages en même
temps dans des fils (Tesseract tourne dans un sous-processus, OpenCV et Pillow libèrent le GIL), une seule
avec Gemma (Ollama traite une requête à la fois). Les fils restent dans ce processus : l'enregistrement de
project.json est protégé par `project_service.project_lock`, qui ne vaut que dans un processus. Aucune image
de livre n'est gardée en mémoire entre deux pages. Après une interruption
(arrêt de Streamlit, plantage), les pages restées PROCESSING repassent PENDING et sont reprises ;
les pages COMPLETED ne sont pas refaites. Les pages FAILED ne sont relancées que sur demande.
"""
from __future__ import annotations

import gc
import logging
import threading
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor
from concurrent.futures import wait as wait_futures
from datetime import datetime, timezone

from pydantic import BaseModel, Field

import config
from models.errors import BookOcrError, JobAlreadyRunningError
from models.scanning import ScanState
from repositories import scan_repository
from services import image_analysis_service, project_service, scanning_service

logger = logging.getLogger("bookocr.scan_runner")


class RunnerStatus(BaseModel):
    running: bool = False
    book_ids: list[str] = Field(default_factory=list)
    current_book: str | None = None
    current_pages: list[str] = Field(default_factory=list)     # pages en cours de traitement
    workers: int = 1                            # pages traitées en même temps
    processed: int = 0
    failed: int = 0
    started_at: datetime | None = None
    seconds_per_page: float | None = None       # débit mesuré sur ce lancement (durée écoulée / pages faites)
    last_export: str | None = None              # dernier PDF créé automatiquement (relatif au projet)
    export_message: str = ""                    # résultat de la création automatique du PDF
    message: str = ""


_lock = threading.Lock()
_thread: threading.Thread | None = None
_cancel = threading.Event()
_status = RunnerStatus()


def is_running() -> bool:
    return _thread is not None and _thread.is_alive()


def status() -> RunnerStatus:
    with _lock:
        current = _status.model_copy()
    current.running = is_running()
    return current


def cancel() -> bool:
    """Demande l'arrêt après la page en cours (jamais au milieu d'une écriture)."""
    if not is_running():
        return False
    _cancel.set()
    return True


def select_pages(book_id: str, page_ids: list[str] | None, retry_failed: bool) -> list[str]:
    """Pages à traiter : celles demandées (remises en file), sinon PENDING (+ FAILED si relance)."""
    if page_ids:
        scan_repository.mark_pending(book_id, page_ids, reset_attempts=True)
        return list(page_ids)
    if retry_failed:
        failed = [task.page_id for task in scan_repository.tasks(book_id, [ScanState.FAILED])]
        scan_repository.mark_pending(book_id, failed, reset_attempts=True)
    selected = []
    for task in scan_repository.tasks(book_id, [ScanState.PENDING]):
        if task.attempts >= config.SCAN_MAX_ATTEMPTS:
            # Page interrompue plusieurs fois (plantage pendant son traitement) : on n'insiste plus.
            scan_repository.set_state(book_id, task.page_id, ScanState.FAILED,
                                      error="Traitement interrompu à plusieurs reprises : relancez-la à la main.")
            continue
        selected.append(task.page_id)
    return selected


def worker_count(use_ai: bool | None, requested: int | None = None) -> int:
    """Pages traitées en même temps : le réglage (au moins 1), mais une seule quand Gemma analyse les pages."""
    ai = image_analysis_service.analysis_enabled() if use_ai is None else use_ai
    return 1 if ai else max(1, requested if requested is not None else config.SCAN_WORKERS)


def _process_one(project, page_id: str, use_ai: bool | None) -> bool:
    """Traite une page dans un fil du pool ; vrai si la page est en échec."""
    _page_started(page_id)
    try:
        record = scanning_service.process_page(project, page_id, use_ai=use_ai)
        return record.error is not None
    finally:
        _page_finished(page_id)
        gc.collect()


def run_book(book_id: str, page_ids: list[str] | None = None, *, retry_failed: bool = False,
             use_ai: bool | None = None, cancel_event: threading.Event | None = None,
             workers: int | None = None) -> tuple[int, int]:
    """Traite un livre (plusieurs pages à la fois) ; renvoie (pages traitées, pages en échec).

    Les pages partent dans l'ordre du livre ; il n'y en a jamais plus de `workers` en cours, si bien qu'une
    annulation ne laisse finir que les pages déjà commencées.
    """
    project = project_service.open_project(book_id)
    scanning_service.sync_queue(project)
    scan_repository.recover_interrupted(book_id)
    todo = select_pages(book_id, page_ids, retry_failed)
    count = worker_count(use_ai, workers)
    _update(current_book=book_id, workers=count)
    processed = failed = 0

    def collect(done: set[Future]) -> None:
        nonlocal processed, failed
        for future in done:
            page_failed = future.result()          # une erreur imprévue remonte comme avant (fil du worker)
            processed += 1
            failed += int(page_failed)
            _record_page(failed=page_failed)

    with ThreadPoolExecutor(max_workers=count, thread_name_prefix="scan_page") as pool:
        pending: set[Future] = set()
        for position, page_id in enumerate(todo):
            if cancel_event is not None and cancel_event.is_set():
                break
            if position % max(1, config.SCAN_BATCH_SIZE) == 0 and position:
                project = project_service.open_project(book_id)    # ordre et réglages faits entre-temps
            try:
                project.page(page_id)
            except BookOcrError:
                continue                                           # page supprimée entre-temps
            while len(pending) >= count:
                done, pending = wait_futures(pending, return_when=FIRST_COMPLETED)
                collect(done)
            pending.add(pool.submit(_process_one, project, page_id, use_ai))
        done, _ = wait_futures(pending)
        collect(done)
    if not (cancel_event is not None and cancel_event.is_set()):
        _auto_export(book_id)
    return processed, failed


def _auto_export(book_id: str) -> None:
    """Fin de traitement : PDF créé et enregistré si le livre le demande et que toutes ses pages sont prêtes."""
    try:
        _update(current_pages=[], export_message="Création automatique du PDF…")
        export = scanning_service.auto_export_if_ready(project_service.open_project(book_id))
        _update(last_export=export.pdf_file if export else None,
                export_message=f"PDF enregistré : {export.pdf_file.rsplit('/', 1)[-1]}" if export else "")
    except BookOcrError as error:
        logger.warning("PDF automatique non créé pour le livre %s : %s", book_id, error)
        _update(export_message=f"PDF automatique non créé : {error.user_message}")


def _update(**values) -> None:
    with _lock:
        for key, value in values.items():
            setattr(_status, key, value)


def _page_started(page_id: str) -> None:
    with _lock:
        _status.current_pages = [*_status.current_pages, page_id]


def _page_finished(page_id: str) -> None:
    with _lock:
        _status.current_pages = [current for current in _status.current_pages if current != page_id]


def _record_page(failed: bool) -> None:
    """Une page de plus ; débit = durée écoulée depuis le lancement / pages faites (pages simultanées comprises)."""
    with _lock:
        _status.processed += 1
        _status.failed += int(failed)
        if _status.started_at is not None:
            elapsed = (datetime.now(timezone.utc) - _status.started_at).total_seconds()
            _status.seconds_per_page = round(elapsed / _status.processed, 1)


def is_page_busy(page_id: str) -> bool:
    """Vrai si cette page est en cours de traitement en arrière-plan."""
    return is_running() and page_id in status().current_pages


def start(book_ids: list[str], page_ids: list[str] | None = None, *, retry_failed: bool = False,
          use_ai: bool | None = None) -> None:
    """Lance le traitement des livres (dans l'ordre) dans un fil d'arrière-plan. page_ids : un seul livre.

    use_ai=None : selon la configuration ; False : aucune requête Ollama pendant ce lancement.
    """
    global _thread, _status
    if page_ids and len(book_ids) != 1:
        raise ValueError("Une sélection de pages ne concerne qu'un livre.")
    with _lock:
        if _thread is not None and _thread.is_alive():
            raise JobAlreadyRunningError("Une numérisation est déjà en cours.")
        _cancel.clear()
        _status = RunnerStatus(running=True, book_ids=list(book_ids), started_at=datetime.now(timezone.utc),
                               message="Traitement en cours.")

        def work() -> None:
            try:
                for book_id in book_ids:
                    if _cancel.is_set():
                        break
                    run_book(book_id, page_ids, retry_failed=retry_failed, use_ai=use_ai, cancel_event=_cancel)
                _update(message="Arrêté à la demande." if _cancel.is_set() else "Traitement terminé.")
            except BookOcrError as error:
                logger.warning("Numérisation interrompue : %s", error)
                _update(message=error.user_message)
            except Exception as error:   # le fil ne doit pas mourir en silence : journalisé et affiché
                logger.exception("Erreur imprévue du worker de numérisation")
                _update(message=f"Erreur inattendue : {error}")
            finally:
                _update(current_pages=[], current_book=None)

        _thread = threading.Thread(target=work, name="scan_runner", daemon=True)
        _thread.start()


def wait(timeout: float | None = None) -> bool:
    """Attend la fin du worker (tests, scripts) ; vrai s'il est terminé."""
    thread = _thread
    if thread is not None:
        thread.join(timeout)
    return not is_running()
