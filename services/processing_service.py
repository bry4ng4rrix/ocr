"""Pipeline par page : géométrie -> amélioration -> OCR -> mise en page et extraction des images.

Chaque étape lit le résultat de la précédente sur disque et écrit un fichier séparé, régénérable.
Le projet est enregistré après chaque page : un lot interrompu reprend là où il s'est arrêté.
Le traitement en arrière-plan n'enregistre que la page qu'il vient de traiter, en relisant le projet
sur disque sous verrou, pour ne pas écraser les modifications faites en parallèle dans l'interface.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Iterable
from enum import StrEnum
from pathlib import Path

import numpy as np

import config
from models import (ImageBlock, Issue, JobStatus, LayoutSettings, OcrResult, Page, PageStatus, ProcessingReport,
                    ProgressEvent, Project, Severity, new_id)
from models.errors import BookOcrError, JobAlreadyRunningError, ProcessingError
from models.project import utc_now
from services import (image_service, layout_service, ocr_service, orientation_service, project_service)

logger = logging.getLogger("bookocr.processing")


class Step(StrEnum):
    GEOMETRY = "geometry"
    ENHANCE = "enhance"
    OCR = "ocr"
    LAYOUT = "layout"


ALL_STEPS = (Step.GEOMETRY, Step.ENHANCE, Step.OCR, Step.LAYOUT)
STEP_LABELS = {Step.GEOMETRY: "Correction géométrique", Step.ENHANCE: "Amélioration de l'image",
               Step.OCR: "Reconnaissance du texte", Step.LAYOUT: "Mise en page et images"}

# Verrou unique par projet, partagé avec project_service.save_project et l'interface.
project_lock = project_service.project_lock


# --------------------------------------------------------------------------- fichiers de travail


def ocr_result_path(project: Project, page: Page) -> Path:
    return project_service.project_dir(project) / "ocr" / f"{page.id}.json"


def working_image_path(project: Project, page: Page) -> Path:
    """Image de travail de la page : version améliorée si elle existe, sinon image corrigée."""
    paths = project_service.page_paths(project, page)
    if not page.enhancement.is_neutral() and paths.enhanced.exists():
        return paths.enhanced
    return paths.corrected


def load_working_image(project: Project, page: Page) -> np.ndarray:
    path = working_image_path(project, page)
    if not path.exists():
        raise ProcessingError("L'image corrigée de cette page n'existe pas encore : lancez la correction.")
    return image_service.read_image(path)


def load_ocr_result(project: Project, page: Page) -> OcrResult | None:
    path = ocr_result_path(project, page)
    if not path.exists():
        return None
    try:
        return OcrResult.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        logger.warning("Résultat OCR illisible, il sera recalculé : %s", path)
        return None


def _write_json(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(path)


# --------------------------------------------------------------------------- étapes


def run_geometry(project: Project, page: Page, redetect: bool = False) -> list[Issue]:
    """Corrige la géométrie depuis l'original et écrit corrected/<id>.png (l'original n'est jamais modifié)."""
    original = project_service.load_original(project, page)
    detect = redetect or not page.geometry.detection_done
    outcome = orientation_service.correct_page(original, page.geometry, detect=detect, languages=project.languages)
    paths = project_service.page_paths(project, page)
    image_service.write_png(outcome.image, paths.corrected)
    page.geometry = outcome.geometry
    page.corrected_height, page.corrected_width = outcome.image.shape[:2]
    page.status = PageStatus.CORRECTED
    if not page.enhancement.is_neutral():
        _write_enhanced(project, page, outcome.image)
    return [issue.model_copy(update={"page_id": page.id}) for issue in outcome.issues]


def _write_enhanced(project: Project, page: Page, corrected: np.ndarray) -> None:
    paths = project_service.page_paths(project, page)
    image_service.write_png(image_service.enhance(corrected, page.enhancement), paths.enhanced)


def run_enhance(project: Project, page: Page) -> list[Issue]:
    """Écrit enhanced/<id>.png si les réglages ne sont pas neutres, sinon supprime la version améliorée."""
    paths = project_service.page_paths(project, page)
    if page.enhancement.is_neutral():
        paths.enhanced.unlink(missing_ok=True)
        return []
    corrected = image_service.read_image(paths.corrected)
    _write_enhanced(project, page, corrected)
    return []


def run_ocr(project: Project, page: Page) -> list[Issue]:
    """OCR de l'image de travail (cache selon l'image et les paramètres)."""
    ocr_service.check_languages(project.languages)
    working = load_working_image(project, page)
    illustrations = [block.box for block in page.image_blocks if not block.hidden]
    gray = image_service.prepare_ocr_image(working, illustrations)
    paths = project_service.page_paths(project, page)
    image_service.write_png(gray, paths.ocr_input)
    result = ocr_service.run_ocr(gray, project.languages, psm=project.ocr_psm, cache_dir=paths.cache_dir)
    _write_json(ocr_result_path(project, page), result.model_dump_json())
    page.ocr_signature = page.geometry.signature()
    page.ocr_languages = list(project.languages)
    page.mean_confidence = result.mean_confidence
    page.status = PageStatus.OCR_DONE
    issues = []
    if not result.words:
        issues.append(Issue(code="ocr_empty", message="Aucun texte reconnu sur cette page.", page_id=page.id))
    elif result.mean_confidence < config.LOW_CONFIDENCE:
        issues.append(Issue(code="ocr_low_confidence", page_id=page.id,
                            message=f"Confiance OCR faible ({result.mean_confidence:.0f} %) : vérifiez le texte."))
    return issues


def run_layout(project: Project, page: Page) -> tuple[Page, list[Issue]]:
    """Analyse la mise en page, fusionne avec les corrections de l'utilisateur, extrait images et fonds."""
    ocr = load_ocr_result(project, page)
    if ocr is None:
        raise ProcessingError("La reconnaissance du texte n'a pas encore été faite pour cette page.")
    working = load_working_image(project, page)
    result = layout_service.analyse_page(ocr, working, LayoutSettings(languages=project.languages,
                                                                       low_confidence=config.LOW_CONFIDENCE))
    merged = layout_service.merge_layout(page, result)
    issues = [issue.model_copy(update={"page_id": page.id}) for issue in result.issues]
    issues += extract_page_images(project, merged, working)
    merged.status = PageStatus.ANALYSED
    return merged, issues


def extract_page_images(project: Project, page: Page, working: np.ndarray) -> list[Issue]:
    """Écrit images/<block>.png et backgrounds/<page>_<n>.png ; le texte posé dessus est effacé (estimation)."""
    paths = project_service.page_paths(project, page)
    issues: list[Issue] = []
    overlay_boxes = [block.box for block in page.text_blocks if block.on_image and not block.hidden]
    for block in page.image_blocks:
        issues += _extract_image_block(project, page, block, working, overlay_boxes, paths.images_dir)
    page.background_files = []
    for index, region in enumerate(page.background_regions):
        inside = [box for box in overlay_boxes if box.fraction_inside(region) > 0.5]
        crop = image_service.crop_box(working, region)
        if inside:
            crop = image_service.erase_text_regions(crop, [_relative(box, region) for box in inside],
                                                    method=project.background_method)
        target = paths.backgrounds_dir / f"{page.id}_{index}.png"
        image_service.write_png(crop, target)
        page.background_files.append(target.relative_to(project_service.project_dir(project)).as_posix())
    if page.background_regions:
        issues.append(Issue(code="background_estimated", severity=Severity.INFO, page_id=page.id,
                            message="Le fond sous le texte a été reconstitué par estimation ; l'original est conservé."))
    return issues


def _extract_image_block(project: Project, page: Page, block: ImageBlock, working: np.ndarray,
                         overlay_boxes: list, images_dir: Path) -> list[Issue]:
    if block.file and block.user_modified and (project_service.project_dir(project) / block.file).exists():
        return []
    crop = image_service.crop_box(working, block.box)
    root = project_service.project_dir(project)
    target = images_dir / f"{block.id}.png"
    inside = [box for box in overlay_boxes if box.fraction_inside(block.box) > 0.5]
    if inside:
        raw = images_dir / f"{block.id}_raw.png"
        image_service.write_png(crop, raw)
        block.raw_file = raw.relative_to(root).as_posix()
        crop = image_service.erase_text_regions(crop, [_relative(box, block.box) for box in inside],
                                                method=project.background_method)
    image_service.write_png(crop, target)
    block.file = target.relative_to(root).as_posix()
    return []


def _relative(box, container):
    """Boîte exprimée dans le repère d'une boîte englobante."""
    from models import Box

    return Box.from_edges((box.x - container.x) / container.w, (box.y - container.y) / container.h,
                          (box.right - container.x) / container.w, (box.bottom - container.y) / container.h)


def recrop_image_block(project: Project, page: Page, block_id: str) -> None:
    """Re-découpe une image après déplacement ou redimensionnement de sa boîte dans l'éditeur."""
    working = load_working_image(project, page)
    block = page.image_block(block_id)
    block.user_modified = False
    overlay = [b.box for b in page.text_blocks if b.on_image and not b.hidden]
    _extract_image_block(project, page, block, working, overlay, project_service.page_paths(project, page).images_dir)
    block.user_modified = True


# --------------------------------------------------------------------------- lots


def process_page(project: Project, page_id: str, steps: Iterable[Step], redetect: bool = False,
                 progress: Callable[[str], None] | None = None) -> tuple[Page, list[Issue]]:
    """Traite une page (étapes dans l'ordre du pipeline) ; renvoie la page mise à jour (non enregistrée)."""
    wanted = {Step(step) for step in steps}
    page = project.page(page_id).model_copy(deep=True)
    issues: list[Issue] = []
    for step in ALL_STEPS:
        if step not in wanted:
            continue
        if progress:
            progress(step.value)
        if step == Step.GEOMETRY:
            issues += run_geometry(project, page, redetect=redetect)
        elif step == Step.ENHANCE:
            issues += run_enhance(project, page)
        elif step == Step.OCR:
            issues += run_ocr(project, page)
        elif step == Step.LAYOUT:
            page, layout_issues = run_layout(project, page)
            issues += layout_issues
    page.error = None
    page.issues = _merge_issues(page.issues, issues, wanted)
    return page, issues


def _merge_issues(previous: list[Issue], fresh: list[Issue], steps: set[Step]) -> list[Issue]:
    if Step.GEOMETRY in steps:
        return list(fresh)
    return [issue for issue in previous if issue.code not in {i.code for i in fresh}] + list(fresh)


def _store_page(project: Project, page: Page) -> None:
    """Remplace la page dans le projet en mémoire ET sur disque (relu sous verrou)."""
    with project_lock(project.id):
        on_disk = project_service.open_project(project.id)
        try:
            on_disk.page(page.id)
        except BookOcrError:
            return    # page supprimée entre-temps
        project_service.update_page(on_disk, page)
    for index, existing in enumerate(project.pages):
        if existing.id == page.id:
            project.pages[index] = page


def process_pages(project: Project, page_ids: list[str] | None, steps: Iterable[Step],
                  progress: Callable[[ProgressEvent], None] | None = None,
                  cancel: threading.Event | None = None, redetect: bool = False) -> ProcessingReport:
    """Traite plusieurs pages ; une erreur sur une page la marque en erreur et le lot continue."""
    wanted = [Step(step) for step in steps]
    ids = page_ids if page_ids is not None else [page.id for page in project.pages]
    report = ProcessingReport()
    started = time.monotonic()
    for position, page_id in enumerate(ids):
        if cancel is not None and cancel.is_set():
            report.cancelled = True
            break

        def notify(step: str, position=position, page_id=page_id) -> None:
            if progress:
                progress(ProgressEvent(page_id=page_id, page_position=position, total=len(ids), step=step,
                                       message=STEP_LABELS.get(Step(step), step)))
        try:
            page, issues = process_page(project, page_id, wanted, redetect=redetect, progress=notify)
            _store_page(project, page)
            report.processed.append(page_id)
            report.issues += issues
        except BookOcrError as error:
            _mark_error(project, page_id, error.user_message)
            report.failed[page_id] = error.user_message
        except Exception as error:   # erreur imprévue : la page est marquée, le lot continue, la trace est journalisée
            logger.exception("Erreur imprévue sur la page %s", page_id)
            _mark_error(project, page_id, f"Erreur inattendue : {error}")
            report.failed[page_id] = f"Erreur inattendue : {error}"
    report.duration_s = time.monotonic() - started
    return report


def _mark_error(project: Project, page_id: str, message: str) -> None:
    try:
        page = project.page(page_id).model_copy(deep=True)
    except BookOcrError:
        return
    page.status = PageStatus.ERROR
    page.error = message
    _store_page(project, page)


# --------------------------------------------------------------------------- arrière-plan


_jobs: dict[str, tuple[threading.Thread, threading.Event]] = {}


def _job_path(project_id: str) -> Path:
    return project_service.project_dir(project_id) / "job.json"


def _write_job(status: JobStatus) -> None:
    _write_json(_job_path(status.project_id), status.model_dump_json(indent=1))


def job_status(project_id: str) -> JobStatus | None:
    """État du dernier traitement ; un job « running » sans thread vivant (redémarrage) devient « interrupted »."""
    path = _job_path(project_id)
    if not path.exists():
        return None
    try:
        status = JobStatus.model_validate_json(path.read_text(encoding="utf-8"))
    except ValueError:
        return None
    if status.state == "running" and not is_running(project_id):
        status.state = "interrupted"
        status.message = "Traitement interrompu (application redémarrée) : relancez-le pour reprendre."
    return status


def is_running(project_id: str) -> bool:
    job = _jobs.get(project_id)
    return job is not None and job[0].is_alive()


def start_background_job(project_id: str, page_ids: list[str] | None, steps: Iterable[Step],
                         redetect: bool = False) -> str:
    """Lance le traitement dans un thread ; l'avancement est écrit dans job.json."""
    if is_running(project_id):
        raise JobAlreadyRunningError("Un traitement est déjà en cours pour ce projet.")
    project = project_service.open_project(project_id)
    ids = page_ids if page_ids is not None else [page.id for page in project.pages]
    wanted = [Step(step) for step in steps]
    status = JobStatus(job_id=new_id(), project_id=project_id, state="running", page_ids=ids,
                       steps=[step.value for step in wanted], started_at=utc_now())
    _write_job(status)
    cancel = threading.Event()

    def on_progress(event: ProgressEvent) -> None:
        status.done = event.page_position
        status.current_page_id = event.page_id
        status.current_step = event.step
        _write_job(status)

    def run() -> None:
        try:
            report = process_pages(project, ids, wanted, progress=on_progress, cancel=cancel, redetect=redetect)
            status.done = len(report.processed) + len(report.failed)
            status.errors = report.failed
            status.state = "cancelled" if report.cancelled else "done"
            status.message = (f"{len(report.processed)} page(s) traitée(s), {len(report.failed)} en erreur, "
                              f"en {report.duration_s:.0f} s.")
        except Exception as error:   # le thread ne doit jamais mourir sans laisser d'état lisible
            logger.exception("Traitement en arrière-plan interrompu")
            status.state = "failed"
            status.message = f"Traitement interrompu : {error}"
        status.finished_at = utc_now()
        status.current_step = None
        _write_job(status)

    thread = threading.Thread(target=run, name=f"bookocr-job-{project_id}", daemon=True)
    _jobs[project_id] = (thread, cancel)
    thread.start()
    return status.job_id


def cancel_job(project_id: str) -> bool:
    job = _jobs.get(project_id)
    if job is None or not job[0].is_alive():
        return False
    job[1].set()
    return True


def wait_for_job(project_id: str, timeout: float | None = None) -> JobStatus | None:
    job = _jobs.get(project_id)
    if job is not None:
        job[0].join(timeout)
    return job_status(project_id)


def pages_needing(project: Project, step: Step) -> list[str]:
    """Pages pour lesquelles l'étape reste à faire (reprise après erreur ou interruption)."""
    order = {PageStatus.IMPORTED: 0, PageStatus.ERROR: 0, PageStatus.CORRECTED: 1,
             PageStatus.OCR_DONE: 2, PageStatus.ANALYSED: 3}
    threshold = {Step.GEOMETRY: 1, Step.ENHANCE: 1, Step.OCR: 2, Step.LAYOUT: 3}[step]
    return [page.id for page in project.pages if order[page.status] < threshold or page.ocr_outdated]
