"""Numérisation fidèle d'un livre : page redressée, améliorée prudemment, OCR invisible, un PDF par livre.

Orchestration seulement : la géométrie est celle du pipeline existant (`processing_service.run_geometry`,
OpenCV + OSD Tesseract), les réglages sont faits par `image_enhancement` (OpenCV/Pillow), les mesures par
`image_quality_service`, l'OCR par `ocr_service`, le PDF par `pdf_export`. Gemma (Ollama) ne fait que
proposer : sa réponse validée sert à choisir des corrections confirmées par les mesures, jamais à
transformer l'image ni à fixer des coordonnées.

Les originaux ne sont jamais réécrits. Chaque page garde ses paramètres et leur historique dans
`scan/<page_id>/record.json` ; l'image finale est rejouée depuis la page redressée et ces paramètres.
"""
from __future__ import annotations

import hashlib
import io
import logging
import time
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

import config
from models import OcrResult, Page, PageStatus, Project
from models.errors import BookOcrError, ExportError, ProcessingError
from models.geometry import Point
from models.reports import Issue, Severity
from models.scanning import (AdjustmentPlan, AdjustmentSource, AnalysisRecord, BookScanStats, OrientationIssue,
                             PageKind, QualityCheck, ScanAdjustments, ScanBookSettings, ScanPageRecord, ScanState, SearchablePageInput, SearchablePdfReport,
                             SearchablePdfValidation, utc_now)
from repositories import scan_repository
from services import (export_service, image_analysis_service, image_enhancement, image_quality_service,
                      image_service, ocr_service, pdf_export, processing_service, project_service)
from services.processing_service import Step

logger = logging.getLogger("bookocr.scanning")

COVER_PICTURE_SHARE = 0.6          # images sur plus de 60 % de la page : couverture / page illustrée
IMAGE_PICTURE_SHARE = 0.03         # au moins 3 % d'images : page « avec image(s) »
COVER_PAPER_MAX = 90.0             # pas de papier clair (fond sombre ou coloré pleine page) : couverture
PREVIEW_SIDE = 1400                # aperçu interactif (curseurs) : calcul sur une copie réduite
MIN_QUAD_SHARE = 0.05              # un recadrage manuel couvre au moins 5 % de la photo
MIN_QUAD_ANGLE = 45.0              # angles intérieurs admis pour un recadrage manuel (degrés)
MAX_QUAD_ANGLE = 135.0
_GEOMETRY_REVIEW_CODES = {
    "orientation_uncertain": "Orientation incertaine : la page n'a pas été tournée automatiquement, confirmez-la.",
    "quad_uncertain": "Contour de la feuille incertain : vérifiez le recadrage.",
    "quad_not_found": "Aucun contour de feuille fiable : recadrage manuel recommandé.",
    "quad_too_small": "Contour de feuille trop petit : recadrage manuel recommandé.",
    "dewarp_skipped": "Courbure mesurée mais non corrigée : vérifiez la lisibilité près de la reliure.",
}


# --------------------------------------------------------------------------- fichiers


def scan_dir(project: Project, page_id: str) -> Path:
    return project_service.project_dir(project) / "scan" / page_id


def analysis_cache_dir(project: Project) -> Path:
    return project_service.project_dir(project) / "scan" / "analysis"


def _relative(project: Project, path: Path) -> str:
    return path.relative_to(project_service.project_dir(project)).as_posix()


def _absolute(project: Project, relative: str) -> Path:
    return project_service.project_dir(project) / relative


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(data)
    temporary.replace(path)


def load_record(project: Project, page: Page) -> ScanPageRecord:
    path = scan_dir(project, page.id) / "record.json"
    if path.is_file():
        try:
            record = ScanPageRecord.model_validate_json(path.read_text(encoding="utf-8"))
            if record.source_sha256 == page.source_sha256:
                return record
            logger.info("Source remplacée pour la page %s : paramètres de numérisation réinitialisés", page.id)
        except (OSError, ValueError) as error:
            logger.warning("record.json illisible pour la page %s (%s) : réinitialisé", page.id, type(error).__name__)
    return ScanPageRecord(page_id=page.id, source_sha256=page.source_sha256)


def save_record(project: Project, record: ScanPageRecord) -> None:
    record.updated_at = utc_now()
    _atomic_write_bytes(scan_dir(project, record.page_id) / "record.json",
                        record.model_dump_json(indent=1).encode("utf-8"))


def encode_final_jpeg(rgb: np.ndarray) -> bytes:
    """Unique encodage avec perte de l'image finale (4:4:4, sans EXIF) ; inséré tel quel dans le PDF."""
    buffer = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb)).save(buffer, format="JPEG", quality=config.SCAN_JPEG_QUALITY,
                                                    subsampling=0, optimize=True)
    return buffer.getvalue()


def final_signature(page: Page, adjustments: ScanAdjustments) -> str:
    raw = f"{page.source_sha256}|{page.geometry.signature()}|{adjustments.model_dump_json()}"
    return hashlib.sha1(raw.encode()).hexdigest()[:16]


def is_current(page: Page, record: ScanPageRecord) -> bool:
    """Vrai si l'image finale et son OCR correspondent à la géométrie et aux réglages actuels."""
    signature = final_signature(page, record.adjustments)
    return (record.final_signature == signature and record.ocr_signature == signature
            and record.final_file is not None and record.ocr_file is not None)


def load_straightened(project: Project, page: Page) -> np.ndarray:
    path = project_service.page_paths(project, page).corrected
    if not path.is_file():
        raise ProcessingError("La page n'a pas encore été redressée.", detail=page.id)
    return image_service.read_image(path)


def load_final(project: Project, record: ScanPageRecord) -> np.ndarray | None:
    if not record.final_file:
        return None
    path = _absolute(project, record.final_file)
    return image_service.read_image(path) if path.is_file() else None


def load_ocr(project: Project, record: ScanPageRecord) -> OcrResult | None:
    if not record.ocr_file:
        return None
    path = _absolute(project, record.ocr_file)
    if not path.is_file():
        return None
    return OcrResult.model_validate_json(path.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- import d'un livre


def create_book(name: str, languages: list[str]) -> Project:
    """Nouveau livre (projet) pour la numérisation."""
    if not name.strip():
        raise ProcessingError("Donnez un nom au livre.")
    return project_service.create_project(name.strip(), languages=languages or ["fra"])


def import_book_files(project: Project, files: list[tuple[str, bytes]]):
    """Importe des photos téléversées dans l'ordre naturel des noms (p2 avant p10) puis met la file à jour."""
    ordered = sorted(files, key=lambda item: project_service._natural_key(Path(item[0])))
    report = project_service.import_files(project, ordered)
    sync_queue(project)
    return report


def import_book_folder(project: Project, folder: str | Path):
    """Importe toutes les photos d'un dossier local (une à la fois, ordre naturel) puis met la file à jour."""
    paths = project_service.collect_import_paths(Path(folder).expanduser())
    if not paths:
        raise ProcessingError("Aucune photo importable dans ce dossier.", detail=str(folder))
    report = project_service.import_paths(project, paths)
    sync_queue(project)
    return report


# --------------------------------------------------------------------------- réglages du livre


def load_book_settings(project: Project) -> ScanBookSettings:
    path = project_service.project_dir(project) / "scan" / "settings.json"
    if path.is_file():
        try:
            return ScanBookSettings.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            logger.warning("Réglages de numérisation illisibles (%s) : valeurs par défaut", type(error).__name__)
    return ScanBookSettings()


def save_book_settings(project: Project, settings: ScanBookSettings) -> None:
    _atomic_write_bytes(project_service.project_dir(project) / "scan" / "settings.json",
                        settings.model_dump_json(indent=1).encode("utf-8"))


def requeue_book(project: Project) -> int:
    """Remet toutes les pages en file (après un changement des réglages du livre) ; réglages manuels gardés."""
    sync_queue(project)
    return scan_repository.mark_pending(project.id, [page.id for page in project.pages], reset_attempts=True)


# --------------------------------------------------------------------------- file


def sync_queue(project: Project) -> None:
    """Une ligne de file par page, dans l'ordre du livre."""
    scan_repository.sync_book(project.id, [page.id for page in project.pages])


def set_state(project: Project, page_id: str, state: ScanState, error: str | None = None) -> None:
    scan_repository.set_state(project.id, page_id, state, error=error)


# --------------------------------------------------------------------------- étapes


def run_geometry(project: Project, page_id: str, redetect: bool = False,
                 force: bool = False) -> tuple[Page, list[Issue]]:
    """Redresse la page depuis l'original (OpenCV + OSD) si nécessaire et enregistre la géométrie.

    La page redressée existante est réutilisée (partagée avec le pipeline DOCX) quand la détection est déjà
    faite et que rien ne demande de la refaire. Si la géométrie ne change pas, le statut d'analyse de la page
    (OCR, mise en page du pipeline DOCX) est conservé.
    """
    page = project.page(page_id)
    corrected = project_service.page_paths(project, page).corrected
    if not (redetect or force) and page.geometry.detection_done and corrected.is_file():
        return page, list(load_record(project, page).geometry_issues)
    previous_status, previous_signature = page.status, page.geometry.signature()
    updated, issues = processing_service.process_page(project, page_id, [Step.GEOMETRY], redetect=redetect)
    if updated.geometry.signature() == previous_signature and previous_status in (PageStatus.OCR_DONE,
                                                                                  PageStatus.ANALYSED):
        updated.status = previous_status
    processing_service.store_page(project, updated)
    return project.page(page_id), issues


def geometry_review_reasons(issues: list[Issue]) -> list[str]:
    return [_GEOMETRY_REVIEW_CODES[issue.code] for issue in issues if issue.code in _GEOMETRY_REVIEW_CODES]


def orientation_conflict(page: Page, analysis: AnalysisRecord | None) -> str | None:
    """Gemma voit encore une page tournée après la correction : on ne tourne pas, on demande une vérification."""
    if analysis is None or not analysis.usable:
        return None
    issue = analysis.analysis.orientation_issue  # type: ignore[union-attr]
    if issue in (OrientationIssue.NONE, OrientationIssue.UNCERTAIN):
        return None
    labels = {OrientationIssue.UPSIDE_DOWN: "à l'envers", OrientationIssue.ROTATED_90_CW: "tournée de 90°",
              OrientationIssue.ROTATED_90_CCW: "tournée de 90°"}
    method = page.geometry.orientation_method or "aucune"
    return (f"Gemma voit la page {labels[issue]} alors que l'orientation retenue (méthode : {method}) ne l'est "
            "pas : confirmez la rotation (aucune rotation n'est appliquée sur la seule foi de Gemma).")


def analyse(project: Project, page_id: str, force: bool = False) -> ScanPageRecord:
    """Analyse Gemma de la page redressée (copie réduite, cache) ; ne bloque jamais le traitement."""
    page = project.page(page_id)
    record = load_record(project, page)
    straightened = load_straightened(project, page)
    started = time.monotonic()
    record.analysis = image_analysis_service.analyse_page(straightened, analysis_cache_dir(project), force=force)
    record.timings_s["analysis"] = round(time.monotonic() - started, 2)
    metrics = image_quality_service.measure(straightened)
    record.proposed_plan = _proposal(metrics, record.analysis, load_book_settings(project))
    save_record(project, record)
    return record


def _proposal(metrics, analysis: AnalysisRecord | None, settings: ScanBookSettings) -> AdjustmentPlan | None:
    if analysis is None or not analysis.usable:
        return None
    return image_enhancement.plan_adjustments(metrics, analysis.analysis,
                                              remove_background=settings.remove_background)


def _render(project: Project, page: Page, record: ScanPageRecord, straightened: np.ndarray,
            adjustments: ScanAdjustments, source: AdjustmentSource, metrics=None) -> np.ndarray:
    """Applique les réglages, contrôle avant/après, écrit final.jpg (ou garde la page redressée si dégradée)."""
    started = time.monotonic()
    if adjustments != record.adjustments or source != record.adjustments_source:
        record.remember(source, adjustments)
    enhanced = image_enhancement.apply_adjustments(straightened, adjustments)
    removed = adjustments.background_removal > 0
    # Le contrôle compare des images de même cadrage : la page redressée rognée comme l'image améliorée.
    reference = image_enhancement.crop_edges(straightened, adjustments) if adjustments.has_crop else straightened
    check = image_quality_service.compare(reference, enhanced, None if adjustments.has_crop else metrics,
                                          background_removed=removed)
    final, final_source = enhanced, "enhanced"
    if check.enhancement_degraded:
        cropped = image_enhancement.crop_edges(straightened, adjustments) if adjustments.has_crop else straightened
        final, final_source = cropped, "straightened"
        check.reasons.insert(0, "Amélioration écartée (dégradation mesurée) : page redressée conservée.")
    path = scan_dir(project, page.id) / "final.jpg"
    _atomic_write_bytes(path, encode_final_jpeg(final))
    record.final_file = _relative(project, path)
    record.final_source = final_source
    record.final_signature = final_signature(page, adjustments)
    record.straightened_file = _relative(project, project_service.page_paths(project, page).corrected)
    record.quality = check
    # Bloc de contenu à centrer dans le PDF : seulement sur fond blanc (sinon le bord de la photo se verrait).
    record.content_box = image_quality_service.content_box(final) if removed and final_source == "enhanced" else None
    record.page_kind, record.picture_share = classify_page(final)
    record.timings_s["render"] = round(time.monotonic() - started, 2)
    return final


def _ocr(project: Project, page: Page, record: ScanPageRecord, final: np.ndarray) -> OcrResult:
    """OCR Tesseract de l'image finale : les boîtes sont normalisées sur final.jpg (alignement du PDF)."""
    started = time.monotonic()
    ocr_service.check_languages(project.languages)
    gray = image_service.prepare_ocr_image(final)
    cache = project_service.page_paths(project, page).cache_dir
    result = ocr_service.run_ocr(gray, project.languages, psm=project.ocr_psm, cache_dir=cache)
    path = scan_dir(project, page.id) / "ocr.json"
    _atomic_write_bytes(path, result.model_dump_json().encode("utf-8"))
    record.ocr_file = _relative(project, path)
    record.ocr_signature = record.final_signature
    if record.quality is not None:
        record.quality.ocr_mean_confidence = round(result.mean_confidence, 1) if result.words else None
        record.quality.ocr_word_count = len(result.words)
    record.timings_s["ocr"] = round(time.monotonic() - started, 2)
    return result


def _review_reasons(record: ScanPageRecord, geometry_reasons: list[str], plan: AdjustmentPlan | None,
                    conflict: str | None, ocr: OcrResult | None) -> list[str]:
    reasons = list(geometry_reasons)
    if conflict:
        reasons.append(conflict)
    if plan is not None:
        reasons += plan.review_reasons
    quality = record.quality
    if quality is not None and not quality.passed:
        reasons += [reason for reason in quality.reasons if reason != "Aucune dégradation mesurée."]
    if ocr is not None and not ocr.words:
        reasons.append("Aucun texte reconnu : page blanche, illustration seule ou orientation à vérifier.")
    return list(dict.fromkeys(reasons))


def _final_state(record: ScanPageRecord) -> ScanState:
    if record.error:
        return ScanState.FAILED
    if record.review_reasons and not record.validated:
        return ScanState.REVIEW_REQUIRED
    return ScanState.COMPLETED


def process_page(project: Project, page_id: str, *, use_ai: bool | None = None, auto_apply: bool | None = None,
                 redetect: bool = False, reanalyse: bool = False, regeometry: bool = False) -> ScanPageRecord:
    """Traitement complet d'une page ; enregistre le résultat et l'état de file après chaque étape importante.

    use_ai=None : selon la configuration (OLLAMA_ENABLED et OLLAMA_ANALYSIS_ENABLED).
    auto_apply=None : selon OLLAMA_AUTO_APPLY. Faux : le plan mesuré est appliqué et le plan Gemma est
    gardé en proposition. Des réglages manuels déjà enregistrés sont toujours conservés.
    """
    use_ai = image_analysis_service.analysis_enabled() if use_ai is None else use_ai
    auto_apply = config.OLLAMA_AUTO_APPLY if auto_apply is None else auto_apply
    page = project.page(page_id)
    scan_repository.set_state(project.id, page_id, ScanState.PROCESSING, step="géométrie", count_attempt=True)
    record = load_record(project, page)
    record.error = None
    # Géométrie changée ailleurs (écran Correction) : l'image finale ne correspond plus, on la recalcule.
    stale = record.final_signature is not None and record.final_signature != final_signature(page, record.adjustments)
    try:
        started = time.monotonic()
        page, issues = run_geometry(project, page_id, redetect=redetect, force=regeometry or stale)
        record.geometry_issues = issues
        record.timings_s["geometry"] = round(time.monotonic() - started, 2)
        save_record(project, record)
        straightened = load_straightened(project, page)
        metrics = image_quality_service.measure(straightened)

        if use_ai:
            scan_repository.set_state(project.id, page_id, ScanState.PROCESSING, step="analyse Gemma")
            started = time.monotonic()
            record.analysis = image_analysis_service.analyse_page(
                straightened, analysis_cache_dir(project), force=reanalyse)
            record.timings_s["analysis"] = round(time.monotonic() - started, 2)
            save_record(project, record)
        settings = load_book_settings(project)
        measured = image_enhancement.plan_adjustments(metrics, None, remove_background=settings.remove_background)
        combined = _proposal(metrics, record.analysis if use_ai else None, settings)
        if record.adjustments_source == "manual":
            plan, adjustments, source = None, record.adjustments, "manual"
        elif auto_apply and combined is not None:
            plan, adjustments, source = combined, combined.adjustments, combined.source
        else:
            plan, adjustments, source = measured, measured.adjustments, measured.source
        record.applied_plan = plan
        record.proposed_plan = combined if (combined is not None and not auto_apply
                                             and combined.adjustments != adjustments) else None

        scan_repository.set_state(project.id, page_id, ScanState.PROCESSING, step="amélioration")
        final = _render(project, page, record, straightened, adjustments, source, metrics)
        save_record(project, record)
        scan_repository.set_state(project.id, page_id, ScanState.PROCESSING, step="OCR")
        ocr = _ocr(project, page, record, final)
        conflict = orientation_conflict(page, record.analysis)
        # Mode proposition : les signalements de Gemma restent dans la proposition ; seuls les plans
        # réellement appliqués (et un conflit d'orientation) changent l'état de la page.
        review_plan = plan
        record.review_reasons = _review_reasons(record, geometry_review_reasons(issues), review_plan, conflict, ocr)
        record.validated = False
    except BookOcrError as error:
        record.error = error.user_message
        logger.warning("Numérisation de la page %s en échec : %s", page_id, error)
    except (OSError, ValueError, cv2.error) as error:
        record.error = f"Erreur inattendue : {error}"
        logger.exception("Erreur imprévue pendant la numérisation de la page %s", page_id)
    save_record(project, record)
    scan_repository.set_state(project.id, page_id, _final_state(record), error=record.error)
    return record


# --------------------------------------------------------------------------- actions de l'interface


def preview(project: Project, page_id: str, adjustments: ScanAdjustments,
            max_side: int = PREVIEW_SIDE) -> tuple[np.ndarray, np.ndarray, QualityCheck]:
    """Aperçu rapide (copie réduite) des réglages, sans Gemma ni écriture : (avant, après, contrôle)."""
    page = project.page(page_id)
    straightened = image_service.downscale(load_straightened(project, page), max_side)[0]
    enhanced = image_enhancement.apply_adjustments(straightened, adjustments)
    reference = image_enhancement.crop_edges(straightened, adjustments) if adjustments.has_crop else straightened
    return straightened, enhanced, image_quality_service.compare(
        reference, enhanced, background_removed=adjustments.background_removal > 0)


def render_final(project: Project, page_id: str, adjustments: ScanAdjustments,
                 source: AdjustmentSource = "manual") -> ScanPageRecord:
    """Applique des réglages en pleine résolution (image finale + OCR) et les enregistre."""
    page = project.page(page_id)
    record = load_record(project, page)
    straightened = load_straightened(project, page)
    final = _render(project, page, record, straightened, adjustments, source)
    ocr = _ocr(project, page, record, final)
    geometry = geometry_review_reasons(record.geometry_issues)
    record.review_reasons = _review_reasons(record, geometry, None, orientation_conflict(page, record.analysis), ocr)
    record.validated = False
    record.error = None
    save_record(project, record)
    scan_repository.set_state(project.id, page_id, _final_state(record))
    return record


def auto_enhance(project: Project, page_id: str) -> ScanPageRecord:
    """« Améliorer automatiquement » : plan mesuré (sans Gemma), appliqué en pleine résolution."""
    page = project.page(page_id)
    metrics = image_quality_service.measure(load_straightened(project, page))
    plan = image_enhancement.plan_adjustments(metrics, None,
                                              remove_background=load_book_settings(project).remove_background)
    record = render_final(project, page_id, plan.adjustments, source=plan.source)
    record.applied_plan = plan
    save_record(project, record)
    return record


def redetect_geometry(project: Project, page_id: str) -> ScanPageRecord:
    """Nouvelle détection des bords, de la perspective, de l'orientation et de l'inclinaison (OpenCV/OSD)."""
    return process_page(project, page_id, use_ai=False, redetect=True)


def apply_proposal(project: Project, page_id: str) -> ScanPageRecord:
    """L'utilisateur accepte la proposition (mesures + Gemma) après aperçu."""
    record = load_record(project, project.page(page_id))
    if record.proposed_plan is None:
        raise ProcessingError("Aucune proposition en attente pour cette page.")
    plan = record.proposed_plan
    updated = render_final(project, page_id, plan.adjustments, source=plan.source)
    updated.applied_plan, updated.proposed_plan = plan, None
    save_record(project, updated)
    return updated


def restore_previous(project: Project, page_id: str) -> ScanPageRecord:
    """Revient aux réglages précédents (historique)."""
    record = load_record(project, project.page(page_id))
    if not record.history:
        raise ProcessingError("Aucun réglage précédent enregistré pour cette page.")
    previous = record.history.pop()
    save_record(project, record)
    updated = render_final(project, page_id, previous.adjustments, source=previous.source)
    if updated.history:
        updated.history.pop()        # le retour arrière ne s'empile pas lui-même dans l'historique
    save_record(project, updated)
    return updated


def reset_to_original(project: Project, page_id: str) -> ScanPageRecord:
    """Aucune amélioration : l'image finale redevient la page redressée telle quelle."""
    return render_final(project, page_id, ScanAdjustments(), source="neutral")


def validate_quad(corners: list[Point]) -> list[str]:
    """Contrôle géométrique d'un recadrage : 4 coins, convexe, assez grand, angles plausibles."""
    if len(corners) != 4:
        return ["Un recadrage a exactement quatre coins."]
    points = np.array([[p.x, p.y] for p in corners], np.float32)
    problems: list[str] = []
    if not cv2.isContourConvex(points.reshape(-1, 1, 2)):
        problems.append("Le quadrilatère n'est pas convexe (coins croisés ou mal ordonnés).")
    if cv2.contourArea(points) < MIN_QUAD_SHARE:
        problems.append("Le recadrage est trop petit (moins de 5 % de la photographie).")
    for index in range(4):
        before, here, after = points[index - 1], points[index], points[(index + 1) % 4]
        first, second = before - here, after - here
        norm = float(np.linalg.norm(first) * np.linalg.norm(second))
        if norm == 0:
            problems.append("Deux coins sont confondus.")
            break
        angle = float(np.degrees(np.arccos(np.clip(first @ second / norm, -1, 1))))
        if not MIN_QUAD_ANGLE <= angle <= MAX_QUAD_ANGLE:
            problems.append(f"Angle de {angle:.0f}° au coin {index + 1} : forme invraisemblable pour une page.")
            break
    return problems


def set_manual_quad(project: Project, page_id: str, corners: list[Point] | None) -> ScanPageRecord:
    """Recadrage manuel (coins tl, tr, br, bl normalisés sur l'original) ; None = revenir au contour auto."""
    if corners is not None:
        problems = validate_quad(corners)
        if problems:
            raise ProcessingError("Recadrage refusé : " + " ".join(problems))
    with project_service.project_lock(project.id):
        page = project.page(page_id).model_copy(deep=True)
        page.geometry.manual_quad = list(corners) if corners is not None else None
        project_service.update_page(project, page, reason="Recadrage manuel (numérisation)")
    return process_page(project, page_id, use_ai=False, regeometry=True)


def set_rotation(project: Project, page_id: str, quarter_turns: int) -> ScanPageRecord:
    """Rotation confirmée par l'utilisateur (quarts de tour horaires après l'orientation automatique)."""
    project_service.set_manual_rotation(project, page_id, quarter_turns)
    return process_page(project, page_id, use_ai=False, regeometry=True)


def validate_page(project: Project, page_id: str, validated: bool = True) -> ScanPageRecord:
    """Validation explicite : une page vérifiée par l'utilisateur sort de « à vérifier »."""
    page = project.page(page_id)
    record = load_record(project, page)
    if validated and not is_current(page, record):
        raise ProcessingError("La page doit être traitée (image finale et OCR à jour) avant d'être validée.")
    record.validated = validated
    save_record(project, record)
    scan_repository.set_state(project.id, page_id, _final_state(record), error=record.error)
    return record


# --------------------------------------------------------------------------- export


def export_inputs(project: Project, *, include_unvalidated: bool = True,
                  center_content: bool = True) -> tuple[list[SearchablePageInput], list[str]]:
    """Pages dans l'ordre du livre ; problèmes bloquants (page non traitée, à vérifier non validée).

    center_content : le bloc de contenu des pages à fond supprimé est transmis pour être centré dans le PDF.
    """
    inputs: list[SearchablePageInput] = []
    problems: list[str] = []
    for number, page in enumerate(project.pages, start=1):
        record = load_record(project, page)
        if record.error or not is_current(page, record):
            problems.append(f"Page {number} : pas encore traitée ou obsolète.")
            continue
        if record.review_reasons and not record.validated and not include_unvalidated:
            problems.append(f"Page {number} : à vérifier, non validée.")
            continue
        inputs.append(SearchablePageInput(page_id=page.id, label=f"{number}",
                                          image_path=str(_absolute(project, record.final_file or "")),
                                          ocr_path=str(_absolute(project, record.ocr_file or "")),
                                          content_box=record.content_box if center_content else None))
    return inputs, problems


def export_book_pdf(project: Project, *, include_unvalidated: bool = True, uniform: bool = True,
                    center_content: bool | None = None) -> tuple[SearchablePdfReport, SearchablePdfValidation]:
    """Un PDF pour le livre : toutes les pages, dans l'ordre, sinon rien. Rapport de validation écrit à côté.

    uniform=True : pages PDF de même format, chaque page centrée (marge blanche). center_content (défaut :
    réglage du livre) : sur les pages à fond supprimé, c'est le bloc de texte et d'images qui est centré.
    """
    if center_content is None:
        center_content = load_book_settings(project).center_content
    if not project.pages:
        raise ExportError("Le livre ne contient aucune page.")
    inputs, problems = export_inputs(project, include_unvalidated=include_unvalidated,
                                     center_content=center_content and uniform)
    if problems:
        shown = " ".join(problems[:5]) + (f" (+{len(problems) - 5} autres)" if len(problems) > 5 else "")
        raise ExportError("Export impossible : toutes les pages doivent être prêtes. " + shown)
    output = export_service.export_path(project, ".pdf", "numerisation")
    report = pdf_export.build_searchable_pdf(inputs, output, uniform=uniform)
    validation = pdf_export.validate_searchable_pdf(output, inputs)
    if report.page_count != len(project.pages):
        validation.issues.append(Issue(code="scan_pdf_page_count", severity=Severity.ERROR,
                                       message="Le PDF n'a pas le nombre de pages du livre."))
        validation.passed = False
    output.with_suffix(".validation.json").write_text(
        validation.model_dump_json(indent=1), encoding="utf-8")
    return report, validation


# --------------------------------------------------------------------------- vue d'ensemble


def book_overview(project: Project) -> list[dict]:
    """Une ligne par page (ordre du livre) pour l'interface : état de file, source des réglages, motifs."""
    sync_queue(project)
    states = {task.page_id: task for task in scan_repository.tasks(project.id)}
    rows = []
    for number, page in enumerate(project.pages, start=1):
        record = load_record(project, page)
        task = states.get(page.id)
        rows.append({
            "page_id": page.id, "numéro": number, "fichier": page.original_name,
            "état": task.state.value if task else ScanState.PENDING.value,
            "à jour": is_current(page, record), "validée": record.validated,
            "réglages": record.adjustments_source,
            "analyse": record.analysis.status if record.analysis else "—",
            "proposition": record.proposed_plan is not None,
            "OCR (mots)": record.quality.ocr_word_count if record.quality else None,
            "motifs": " ; ".join(record.review_reasons or ([record.error] if record.error else [])),
        })
    return rows


_ADJUSTMENT_LABELS = {
    "background_removal": "Fond supprimé (papier blanc), images conservées",
    "paper_whitening": "Fond nettoyé (papier éclairci et uniformisé)",
    "shadow_correction": "Ombres atténuées",
    "local_contrast": "Contraste local renforcé",
    "denoise": "Bruit réduit",
    "sharpness": "Netteté renforcée",
}


def describe_changes(project: Project, page: Page, record: ScanPageRecord) -> list[str]:
    """Modifications réellement appliquées à la page, en phrases courtes (galerie de l'écran)."""
    changes: list[str] = []
    geometry = page.geometry
    codes = {issue.code for issue in record.geometry_issues}
    if geometry.manual_quad is not None:
        changes.append("Recadrage manuel et perspective corrigée")
    elif geometry.detected_quad is not None and geometry.auto_crop:
        method = " (bords trouvés par la couleur du papier)" if "quad_from_paper" in codes else ""
        changes.append(f"Bords détectés, fond retiré, perspective corrigée{method}")
    else:
        changes.append("Aucun bord fiable : photo entière conservée")
    turns = geometry.effective_quarter_turns()
    if turns:
        changes.append(f"Rotation de {turns * 90}°")
    if geometry.orientation_uncertain and not geometry.manual_quarter_turns:
        changes.append("Orientation incertaine (non tournée)")
    skew = geometry.effective_skew()
    if abs(skew) >= 0.1:
        changes.append(f"Inclinaison corrigée ({skew:+.1f}°)")
    if geometry.dewarp_applied:
        changes.append("Courbure des lignes corrigée")
    adjustments = record.adjustments
    if adjustments.has_crop:
        changes.append("Bandes de fond rognées aux bords")
    for field, label in _ADJUSTMENT_LABELS.items():
        if getattr(adjustments, field):
            changes.append(label)
    if adjustments.exposure:
        changes.append(f"Exposition ×{2 ** (adjustments.exposure / 50):.2f}")
    if adjustments.contrast:
        changes.append(f"Contraste {adjustments.contrast:+.0f}")
    if adjustments.gamma != 1.0 or adjustments.brightness:
        changes.append("Luminosité ajustée")
    if adjustments.white_balance:
        changes.append("Balance des blancs")
    if record.final_source == "straightened":
        changes.append("Amélioration écartée (dégradation mesurée)")
    if record.quality is not None and record.quality.ocr_word_count is not None:
        changes.append(f"Texte reconnu : {record.quality.ocr_word_count} mots (couche invisible)")
    return changes



# --------------------------------------------------------------------------- types de page et statistiques


def classify_page(final: np.ndarray) -> tuple[PageKind, float]:
    """Type de la page finale : couverture (images sur la plupart de la page ou pas de papier clair), avec
    image(s) (au moins 3 % d'images : photos, illustrations, aplats colorés, rectangles encadrés), texte seul."""
    small = image_service.downscale(final, 900)[0]
    share = float(image_enhancement.image_regions(small).mean())
    paper = float(np.percentile(image_service.to_gray(small), 90))
    if paper < COVER_PAPER_MAX or share >= COVER_PICTURE_SHARE:
        return PageKind.COVER, round(share, 4)
    if share >= IMAGE_PICTURE_SHARE:
        return PageKind.IMAGE, round(share, 4)
    return PageKind.TEXT, round(share, 4)


def ensure_page_kind(project: Project, page: Page, record: ScanPageRecord) -> ScanPageRecord:
    """Classe une page déjà traitée avant l'existence du classement (lecture de final.jpg, une fois)."""
    if record.page_kind is None and record.final_file:
        final = load_final(project, record)
        if final is not None:
            record.page_kind, record.picture_share = classify_page(final)
            save_record(project, record)
    return record


def set_page_kind(project: Project, page_id: str, kind: PageKind | None) -> ScanPageRecord:
    """Type choisi par l'utilisateur (None = revenir au type détecté)."""
    record = load_record(project, project.page(page_id))
    record.page_kind_override = kind
    save_record(project, record)
    return record


def book_statistics(project: Project) -> BookScanStats:
    """Pages du livre par état, et pages terminées par type effectif (texte seul, avec image, couverture)."""
    sync_queue(project)
    states = {task.page_id: task.state for task in scan_repository.tasks(project.id)}
    stats = BookScanStats(book_id=project.id, book_name=project.name, total_pages=len(project.pages),
                          by_kind={kind: 0 for kind in PageKind})
    for page in project.pages:
        state = states.get(page.id, ScanState.PENDING)
        if state == ScanState.COMPLETED:
            stats.completed_pages += 1
            record = ensure_page_kind(project, page, load_record(project, page))
            if record.effective_kind is not None:
                stats.by_kind[record.effective_kind] += 1
        elif state == ScanState.REVIEW_REQUIRED:
            stats.review_pages += 1
        elif state == ScanState.FAILED:
            stats.failed_pages += 1
        else:
            stats.pending_pages += 1
    return stats


# --------------------------------------------------------------------------- livres : suppression et restauration


def delete_book(project_id: str) -> str:
    """Met un livre à la corbeille (restaurable) et le retire de la file. Refusé pendant un traitement."""
    from workers import scan_runner

    if processing_service.is_running(project_id) or (
            scan_runner.is_running() and project_id in scan_runner.status().book_ids):
        raise ProcessingError("Un traitement est en cours sur ce livre : arrêtez-le avant de le supprimer.")
    trash_name = project_service.trash_project(project_id)
    scan_repository.delete_book(project_id)
    return trash_name


def restore_book(trash_name: str) -> Project:
    """Restaure un livre de la corbeille et reconstruit l'état de sa file depuis les résultats enregistrés."""
    project = project_service.restore_project(trash_name)
    sync_queue(project)
    for page in project.pages:
        record = load_record(project, page)
        if is_current(page, record) or record.error:
            scan_repository.set_state(project.id, page.id, _final_state(record), error=record.error)
    return project
