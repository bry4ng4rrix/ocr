"""Projets persistants : création, ouverture, enregistrement atomique, import, ordre, corbeille, historique.

Format sur disque (ARCHITECTURE §3, TECHNICAL_DECISIONS D-09) :

    <projects_dir>/<project_id>/project.json      modèle Project complet, écriture atomique
                               /project.json.bak  version précédente (toujours un JSON valide)
                               /originals/<sha256[:16]><ext>   fichiers importés, écrits une seule fois
                               /thumbs/<page_id>.jpg, corrected/, enhanced/, ocr_input/, images/, backgrounds/
                               /history/<page_id>/<horodatage>_<id>.json   instantanés bornés (HISTORY_DEPTH)
                               /trash/<page_id>/...   pages supprimées et fichiers remplacés (récupérables)

Les originaux ne sont jamais réécrits : un fichier déjà présent est réutilisé tel quel. Ils ne quittent
`originals/` (vers `trash/`) que lorsqu'aucune page ne les référence plus.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import numpy as np
from pydantic import ValidationError

import config
from models.blocks import new_id
from models.errors import (BookOcrError, FileTooLargeError, ImageReadError, PageNotFoundError,
                           ProjectCorruptedError, ProjectNotFoundError, UnsupportedFileError)
from models.page import GeometryCorrection, Page, PageStatus
from models.project import SCHEMA_VERSION, Project, ProjectSummary, utc_now
from models.reports import ImportReport, Issue, Severity
from services import image_service

logger = logging.getLogger("bookocr.project")

PROJECT_FILE = "project.json"
BACKUP_FILE = "project.json.bak"
CORRUPTED_COPY = "project.json.corrupted"
PROJECT_SUBDIRS = ("originals", "corrected", "enhanced", "ocr_input", "images", "backgrounds", "thumbs",
                   "cache/ocr", "history", "exports", "trash")
TRASH_MANIFEST = "source.json"          # dans chaque lot de trash/<page_id>/<lot>/ : empreinte de l'original
_PROJECT_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_VERSION_ID = re.compile(r"^\d{8}T\d{12}Z_[0-9a-f]{8}$")
_TIMESTAMP = "%Y%m%dT%H%M%S%fZ"

# --------------------------------------------------------------------------- chemins et verrous

_project_locks: dict[str, threading.RLock] = {}
_locks_guard = threading.Lock()


def project_lock(project_id: str) -> threading.RLock:
    """Verrou (réentrant) des écritures de project.json d'un projet, partagé par l'interface et le traitement."""
    with _locks_guard:
        return _project_locks.setdefault(project_id, threading.RLock())


def _check_project_id(project_id: str) -> str:
    if not isinstance(project_id, str) or not _PROJECT_ID.match(project_id):
        raise ProjectNotFoundError("Identifiant de projet invalide.", detail=repr(project_id))
    return project_id


def project_dir(project_or_id: Project | str) -> Path:
    """Dossier du projet (config.projects_dir() relu à chaque appel)."""
    project_id = project_or_id if isinstance(project_or_id, str) else project_or_id.id
    return config.projects_dir() / _check_project_id(project_id)


@dataclass(frozen=True)
class PagePaths:
    """Chemins absolus des fichiers d'une page."""

    original: Path
    corrected: Path
    enhanced: Path
    ocr_input: Path
    thumbnail: Path
    images_dir: Path
    backgrounds_dir: Path
    history_dir: Path
    cache_dir: Path


def page_paths(project: Project, page: Page) -> PagePaths:
    root = project_dir(project)
    return PagePaths(
        original=root / page.source_file,
        corrected=root / "corrected" / f"{page.id}.png",
        enhanced=root / "enhanced" / f"{page.id}.png",
        ocr_input=root / "ocr_input" / f"{page.id}.png",
        thumbnail=root / "thumbs" / f"{page.id}.jpg",
        images_dir=root / "images",
        backgrounds_dir=root / "backgrounds",
        history_dir=root / "history" / page.id,
        cache_dir=root / "cache" / "ocr",
    )


def _timestamp(moment: datetime | None = None) -> str:
    return (moment or utc_now()).strftime(_TIMESTAMP)


# --------------------------------------------------------------------------- écriture atomique


def _atomic_write_text(path: Path, text: str) -> None:
    """Écrit dans un fichier temporaire du même dossier, force l'écriture disque, puis remplace (os.replace)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.parent / f".{path.name}.{new_id()}.tmp"
    try:
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _is_valid_json(path: Path) -> bool:
    try:
        json.loads(path.read_text(encoding="utf-8"))
        return True
    except (OSError, ValueError):
        return False


def _backup_current(folder: Path) -> None:
    """Copie project.json vers project.json.bak, seulement s'il est lisible (un .bak sain n'est jamais écrasé
    par un fichier corrompu)."""
    current = folder / PROJECT_FILE
    if not current.exists():
        return
    if not _is_valid_json(current):
        logger.warning("project.json illisible : la sauvegarde .bak existante est conservée (%s)", folder)
        return
    temporary = folder / f".{BACKUP_FILE}.{new_id()}.tmp"
    try:
        shutil.copyfile(current, temporary)
        os.replace(temporary, folder / BACKUP_FILE)
    finally:
        temporary.unlink(missing_ok=True)


# --------------------------------------------------------------------------- création, ouverture, sauvegarde


def create_project(name: str, languages: list[str] | None = None) -> Project:
    """Crée le dossier du projet (arborescence §3) et l'enregistre."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise ValueError("Le nom du projet ne peut pas être vide.")
    project = Project(name=clean_name, languages=list(languages or config.DEFAULT_LANGUAGES),
                      autosave_seconds=config.DEFAULT_AUTOSAVE_SECONDS)
    root = project_dir(project)
    for subdir in PROJECT_SUBDIRS:
        (root / subdir).mkdir(parents=True, exist_ok=True)
    save_project(project)
    logger.info("Projet créé : %s (%s)", project.name, project.id)
    return project


def save_project(project: Project) -> None:
    """Met à jour `updated_at` et écrit project.json de façon atomique ; l'ancienne version devient le .bak.

    Le contenu est revalidé avant écriture : un projet qui ne pourrait pas être rouvert n'est pas enregistré.
    """
    folder = project_dir(project)
    with project_lock(project.id):
        project.updated_at = utc_now()
        text = project.model_dump_json(indent=1)
        try:
            Project.model_validate_json(text)
        except ValidationError as error:
            raise ProjectCorruptedError("Le projet contient des valeurs invalides : il n'a pas été enregistré.",
                                        detail=str(error)) from error
        folder.mkdir(parents=True, exist_ok=True)
        _backup_current(folder)
        _atomic_write_text(folder / PROJECT_FILE, text)


def _load_project_file(path: Path) -> Project:
    """Lit un project.json. ProjectCorruptedError si illisible ; message explicite si schéma plus récent."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ProjectCorruptedError("Le fichier du projet est illisible.", detail=f"{path}: {error}") from error
    if not isinstance(data, dict):
        raise ProjectCorruptedError("Le fichier du projet est illisible.", detail=f"{path}: objet JSON attendu")
    version = data.get("schema_version")
    if isinstance(version, int) and version > SCHEMA_VERSION:
        raise _FutureSchemaError(
            f"Ce projet a été enregistré par une version plus récente de l'application (format {version}, "
            f"format pris en charge : {SCHEMA_VERSION}). Mettez l'application à jour pour l'ouvrir.",
            detail=str(path))
    try:
        return Project.model_validate(data)
    except ValidationError as error:
        raise ProjectCorruptedError("Le fichier du projet contient des données invalides.",
                                    detail=f"{path}: {error}") from error


class _FutureSchemaError(ProjectCorruptedError):
    """Schéma plus récent : pas de repli sur le .bak (il serait écrasé à la prochaine sauvegarde)."""


def open_project(project_id: str) -> Project:
    """Ouvre un projet. Repli sur project.json.bak si project.json est illisible (journalisé)."""
    folder = project_dir(project_id)
    main, backup = folder / PROJECT_FILE, folder / BACKUP_FILE
    if not folder.is_dir():
        raise ProjectNotFoundError("Projet introuvable.", detail=project_id)
    if not main.exists() and not backup.exists():
        raise ProjectNotFoundError("Projet introuvable : le dossier ne contient pas de fichier project.json.",
                                   detail=str(folder))
    try:
        project = _load_project_file(main)
    except _FutureSchemaError:
        raise
    except ProjectCorruptedError as main_error:
        if not backup.exists():
            raise
        try:
            project = _load_project_file(backup)
        except ProjectCorruptedError as backup_error:
            raise ProjectCorruptedError(
                "Le projet est endommagé et sa sauvegarde précédente est aussi illisible.",
                detail=f"{main_error.detail} | {backup_error.detail}") from backup_error
        logger.warning("project.json illisible, version précédente (.bak) utilisée : %s (%s)",
                       folder, main_error.detail)
        if main.exists():
            shutil.copyfile(main, folder / CORRUPTED_COPY)   # copie conservée pour diagnostic
    if project.id != project_id:
        logger.warning("Identifiant interne %s différent du dossier %s : le dossier fait foi", project.id, project_id)
        project.id = project_id
    return project


def list_projects() -> list[ProjectSummary]:
    """Résumé des projets, du plus récemment modifié au plus ancien. Un projet illisible est ignoré (journalisé)."""
    root = config.projects_dir()
    if not root.is_dir():
        return []
    summaries: list[ProjectSummary] = []
    for folder in sorted(root.iterdir()):
        if not folder.is_dir() or not _PROJECT_ID.match(folder.name):
            continue
        try:
            project = open_project(folder.name)
        except BookOcrError as error:
            logger.warning("Projet ignoré dans la liste (%s) : %s", folder.name, error)
            continue
        summaries.append(ProjectSummary(
            id=project.id, name=project.name, updated_at=project.updated_at, page_count=len(project.pages),
            analysed_count=sum(page.status == PageStatus.ANALYSED for page in project.pages),
            error_count=sum(page.status == PageStatus.ERROR for page in project.pages)))
    summaries.sort(key=lambda summary: summary.updated_at, reverse=True)
    return summaries


def should_autosave(project: Project, last_saved_monotonic: float, now_monotonic: float) -> bool:
    """Vrai si la sauvegarde automatique est active et que le délai est écoulé."""
    return project.autosave_seconds > 0 and now_monotonic - last_saved_monotonic >= project.autosave_seconds


# --------------------------------------------------------------------------- originaux et vignettes


def _store_original(project: Project, sha256: str, extension: str, data: bytes) -> str:
    """Stocke le fichier source une seule fois ; renvoie son chemin relatif au projet. N'écrase jamais rien."""
    root = project_dir(project)
    for page in project.pages:
        if page.source_sha256 == sha256 and (root / page.source_file).is_file():
            return page.source_file
    originals = root / "originals"
    originals.mkdir(parents=True, exist_ok=True)
    for existing in sorted(originals.glob(f"{sha256[:16]}.*")):
        if existing.is_file() and image_service.sha256_bytes(existing.read_bytes()) == sha256:
            return existing.relative_to(root).as_posix()
    target = originals / f"{sha256[:16]}{extension}"
    if target.exists():                      # collision de préfixe (contenu différent) : nom complet
        target = originals / f"{sha256}{extension}"
    temporary = originals / f".{target.name}.{new_id()}.part"
    try:
        with open(temporary, "xb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, target)       # échoue si la cible existe : jamais de réécriture
        except FileExistsError:
            logger.info("Original déjà présent, conservé tel quel : %s", target)
    finally:
        temporary.unlink(missing_ok=True)
    return target.relative_to(root).as_posix()


def _preview_dpi(width_at_import_dpi: int, height_at_import_dpi: int) -> int:
    """Résolution de rendu d'une page PDF suffisante pour sa vignette (deux fois THUMBNAIL_MAX_SIDE)."""
    longest = max(width_at_import_dpi, height_at_import_dpi, 1)
    wanted = config.PDF_IMPORT_DPI * 2 * config.THUMBNAIL_MAX_SIDE / longest
    return int(max(18, min(config.PDF_IMPORT_DPI, wanted + 1)))


def _decode_for_thumbnail(name: str, data: bytes, item: image_service.SourceItem) -> np.ndarray:
    """Décode la page `item` du fichier importé (PDF rendu à basse résolution) ; ImageReadError si illisible."""
    if item.kind == "pdf":
        try:
            return image_service.render_pdf_page(data, item.index, dpi=_preview_dpi(item.width, item.height))
        except (RuntimeError, ValueError, IndexError) as error:
            raise ImageReadError(f"« {name} » : la page {item.index + 1} du PDF est illisible.",
                                 detail=str(error)) from error
    return image_service.decode_image(data, item.index, name)


def _write_thumbnail(project: Project, page: Page, image: np.ndarray) -> None:
    image_service.write_jpeg(image_service.thumbnail(image, config.THUMBNAIL_MAX_SIDE),
                             page_paths(project, page).thumbnail)


def load_original(project: Project, page: Page) -> np.ndarray:
    """Page source décodée (RGB uint8, orientation EXIF appliquée) ; ImageReadError si absente ou illisible."""
    return image_service.read_image(page_paths(project, page).original, page.source_index)


def load_thumbnail(project: Project, page: Page) -> np.ndarray | None:
    """Vignette de la page ; régénérée depuis l'original si elle manque ; None si impossible (journalisé)."""
    path = page_paths(project, page).thumbnail
    try:
        if not path.exists():
            _write_thumbnail(project, page, load_original(project, page))
        return image_service.read_image(path)
    except (BookOcrError, OSError, RuntimeError, ValueError) as error:
        logger.warning("Vignette indisponible pour la page %s : %s", page.id, error)
        return None


# --------------------------------------------------------------------------- import


_IMPORT_ERROR_CODES: list[tuple[type[BookOcrError], str]] = [
    (FileTooLargeError, "import_too_large"),
    (UnsupportedFileError, "import_unsupported"),
    (ImageReadError, "import_unreadable"),
]


def _import_error_code(error: BookOcrError) -> str:
    for error_type, code in _IMPORT_ERROR_CODES:
        if isinstance(error, error_type):
            return code
    return "import_failed"


def _page_label(name: str, item: image_service.SourceItem, item_count: int) -> str:
    return f"« {name} »" if item_count == 1 else f"« {name} » (page {item.index + 1})"


def _import_one_file(project: Project, name: str, data: bytes, known: set[tuple[str, int]],
                     page_count: int) -> tuple[list[Page], list[Issue]]:
    """Pages nouvelles d'un fichier (non insérées dans le projet) et signalements. Lève une BookOcrError
    si le fichier entier est refusé."""
    items = image_service.inspect_upload(name, data)
    sha256 = image_service.sha256_bytes(data)
    issues: list[Issue] = []
    candidates = []
    for item in items:
        if (sha256, item.index) in known:
            issues.append(Issue(code="duplicate_page", severity=Severity.WARNING,
                                message=f"{_page_label(name, item, len(items))} est déjà dans le projet : ignoré."))
        else:
            candidates.append(item)
    room = max(0, config.MAX_PAGES_PER_PROJECT - page_count)
    if len(candidates) > room:
        issues.append(Issue(code="page_limit_reached", severity=Severity.ERROR,
                            message=f"Limite de {config.MAX_PAGES_PER_PROJECT} pages par projet atteinte : "
                                    f"{len(candidates) - room} page(s) de « {name} » non importée(s)."))
        candidates = candidates[:room]
    decoded: list[tuple[image_service.SourceItem, np.ndarray]] = []
    for item in candidates:
        try:
            decoded.append((item, image_service.thumbnail(_decode_for_thumbnail(name, data, item))))
        except ImageReadError as error:
            logger.warning("Page illisible à l'import : %s", error)
            issues.append(Issue(code="import_unreadable", severity=Severity.ERROR,
                                message=f"{_page_label(name, item, len(items))} est illisible : ignoré."))
    if not decoded:
        return [], issues
    source_file = _store_original(project, sha256, Path(name).suffix.lower(), data)
    pages = []
    for item, preview in decoded:
        page = Page(source_file=source_file, source_index=item.index, source_sha256=sha256, original_name=name,
                    source_width=item.width, source_height=item.height)
        _write_thumbnail(project, page, preview)
        known.add((sha256, item.index))
        pages.append(page)
    return pages, issues


def import_files(project: Project, files: list[tuple[str, bytes]], position: int | None = None) -> ImportReport:
    """Importe des fichiers (JPG, PNG, TIFF multi-pages, PDF). Une erreur sur un fichier n'arrête pas les autres.

    Les pages ajoutées sont insérées à `position` (None = à la fin) et le projet est enregistré.
    """
    report = ImportReport()
    known = {(page.source_sha256, page.source_index) for page in project.pages}
    added: list[Page] = []
    for raw_name, data in files:
        name = Path(raw_name).name or "fichier"
        try:
            pages, issues = _import_one_file(project, name, data, known, len(project.pages) + len(added))
        except BookOcrError as error:
            logger.warning("Fichier refusé à l'import : %s", error)
            report.issues.append(Issue(code=_import_error_code(error), message=error.user_message,
                                       severity=Severity.ERROR))
            continue
        except Exception:   # un fichier imprévisible ne doit pas bloquer les autres : journalisé et signalé
            logger.exception("Erreur inattendue à l'import de %s", name)
            report.issues.append(Issue(code="import_failed", severity=Severity.ERROR,
                                       message=f"« {name} » n'a pas pu être importé (erreur inattendue)."))
            continue
        added += pages
        report.issues += issues
    if added:
        index = len(project.pages) if position is None else max(0, min(position, len(project.pages)))
        project.pages[index:index] = added
        save_project(project)
    report.added_page_ids = [page.id for page in added]
    logger.info("Import dans %s : %d page(s) ajoutée(s), %d signalement(s)", project.id, len(added),
                len(report.issues))
    return report


# --------------------------------------------------------------------------- corbeille


def _unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    for counter in range(1, 10_000):
        candidate = path.with_name(f"{path.stem}.{counter}{path.suffix}")
        if not candidate.exists():
            return candidate
    raise OSError(f"impossible de trouver un nom libre pour {path}")


def _derived_files(project: Project, page: Page) -> list[Path]:
    """Fichiers régénérables propres à la page (existants), hors original et historique."""
    root = project_dir(project)
    paths = page_paths(project, page)
    candidates = [paths.corrected, paths.enhanced, paths.ocr_input, paths.thumbnail,
                  root / "ocr" / f"{page.id}.json"]          # résultat OCR écrit par processing_service
    for block in page.image_blocks:
        candidates += [root / name for name in (block.file, block.raw_file) if name]
    candidates += [root / name for name in page.background_files]
    candidates += list(paths.backgrounds_dir.glob(f"{page.id}_*.png"))
    candidates.append(paths.backgrounds_dir / f"{page.id}.png")
    unique = list(dict.fromkeys(path for path in candidates if path.is_file()))
    return [path for path in unique if "originals" not in path.relative_to(root).parts]


def _move_into(project: Project, source: Path, trash_root: Path) -> None:
    """Déplace `source` (fichier ou dossier du projet) sous `trash_root` en gardant son chemin relatif."""
    target = _unique_path(trash_root / source.relative_to(project_dir(project)))
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(source), str(target))


def _write_trash_manifest(trash_root: Path, page: Page) -> None:
    _atomic_write_text(trash_root / TRASH_MANIFEST, json.dumps(
        {"page_id": page.id, "source_file": page.source_file, "source_sha256": page.source_sha256}))


def _trash_batch(project: Project, page: Page, reason: str) -> Path:
    """Nouveau lot trash/<page_id>/<horodatage>-<raison>/ pour les fichiers d'une page."""
    batch = _unique_path(project_dir(project) / "trash" / page.id / f"{_timestamp()}-{reason}")
    batch.mkdir(parents=True)
    _write_trash_manifest(batch, page)
    return batch


def _recover_from_trash(project: Project, page: Page) -> list[str]:
    """Ramène depuis trash/ les fichiers référencés par `page` qui manquent ; renvoie ceux restés absents.

    Original : cherché dans toute la corbeille, empreinte SHA-256 vérifiée. Fichiers dérivés : cherchés dans
    les lots de trash/<page_id>/ dont l'original correspond, du plus récent au plus ancien.
    """
    root = project_dir(project)
    trash = root / "trash"
    missing: list[str] = []
    original = root / page.source_file
    if not original.is_file():
        for candidate in sorted(trash.glob(f"**/{page.source_file}")):
            if candidate.is_file() and image_service.sha256_bytes(candidate.read_bytes()) == page.source_sha256:
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(candidate), str(original))
                logger.info("Original récupéré depuis la corbeille : %s", page.source_file)
                break
        else:
            missing.append(page.source_file)
    batches = []
    page_trash = trash / page.id
    if page_trash.is_dir():
        for batch in sorted(page_trash.iterdir(), reverse=True):
            manifest = batch / TRASH_MANIFEST
            try:
                if json.loads(manifest.read_text(encoding="utf-8")).get("source_sha256") == page.source_sha256:
                    batches.append(batch)
            except (OSError, ValueError):
                continue
    paths = page_paths(project, page)
    wanted = [paths.corrected, paths.enhanced, paths.ocr_input, paths.thumbnail, root / "ocr" / f"{page.id}.json"]
    wanted += [root / name for block in page.image_blocks for name in (block.file, block.raw_file) if name]
    wanted += [root / name for name in page.background_files]
    for path in wanted:
        if path.exists():
            continue
        relative = path.relative_to(root)
        for batch in batches:
            if (batch / relative).is_file():
                path.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(batch / relative), str(path))
                break
    referenced = [root / name for block in page.image_blocks for name in (block.file, block.raw_file) if name]
    referenced += [root / name for name in page.background_files]
    missing += [path.relative_to(root).as_posix() for path in referenced if not path.exists()]
    return missing


# --------------------------------------------------------------------------- historique


@dataclass(frozen=True)
class PageVersion:
    version_id: str
    created_at: datetime
    reason: str


def _history_dir(project: Project, page_id: str) -> Path:
    return project_dir(project) / "history" / page_id


def _write_snapshot(project: Project, page: Page, reason: str) -> str:
    created_at = utc_now()
    version_id = f"{_timestamp(created_at)}_{new_id()[:8]}"
    folder = _history_dir(project, page.id)
    payload = {"version_id": version_id, "reason": reason, "created_at": created_at.isoformat(),
               "page": page.model_dump(mode="json")}
    _atomic_write_text(folder / f"{version_id}.json", json.dumps(payload, ensure_ascii=False))
    _prune_history(folder)
    return version_id


def _prune_history(folder: Path) -> None:
    depth = max(1, config.HISTORY_DEPTH)
    versions = sorted(path for path in folder.glob("*.json") if _VERSION_ID.match(path.stem))
    for old in versions[:-depth]:
        old.unlink(missing_ok=True)


def snapshot_page(project: Project, page_id: str, reason: str) -> str:
    """Instantané de l'état de la page en mémoire ; renvoie l'identifiant de version. Garde HISTORY_DEPTH versions."""
    return _write_snapshot(project, project.page(page_id), reason)


def list_page_versions(project: Project, page_id: str) -> list[PageVersion]:
    """Versions enregistrées de la page, la plus récente d'abord (fichiers illisibles ignorés et journalisés)."""
    versions = []
    for path in _history_dir(project, page_id).glob("*.json"):
        if not _VERSION_ID.match(path.stem):
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            versions.append(PageVersion(version_id=path.stem, created_at=datetime.fromisoformat(data["created_at"]),
                                        reason=str(data.get("reason", ""))))
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.warning("Version d'historique illisible ignorée : %s (%s)", path, error)
    versions.sort(key=lambda version: (version.created_at, version.version_id), reverse=True)
    return versions


def _load_version(project: Project, page_id: str, version_id: str) -> Page:
    if not isinstance(version_id, str) or not _VERSION_ID.match(version_id):
        raise PageNotFoundError("Version de page introuvable.", detail=repr(version_id))
    path = _history_dir(project, page_id) / f"{version_id}.json"
    if not path.is_file():
        raise PageNotFoundError("Version de page introuvable.", detail=version_id)
    try:
        return Page.model_validate(json.loads(path.read_text(encoding="utf-8"))["page"])
    except (OSError, ValueError, KeyError, TypeError) as error:
        raise ProjectCorruptedError("Cette version de la page est illisible.", detail=f"{path}: {error}") from error


def _page_index(project: Project, page_id: str) -> int:
    return project.page_number(page_id) - 1


def _mark_missing_files(page: Page, missing: list[str]) -> None:
    page.issues = [issue for issue in page.issues if issue.code != "files_missing"]
    if missing:
        page.issues.append(Issue(code="files_missing", severity=Severity.WARNING, page_id=page.id,
                                 message=f"{len(missing)} fichier(s) de cette version sont introuvables : "
                                         "relancez le traitement de la page pour les régénérer."))
        logger.warning("Page %s restaurée avec des fichiers manquants : %s", page.id, missing)


def restore_page_version(project: Project, page_id: str, version_id: str) -> Page:
    """Restaure une version (à la même position) après un instantané « Avant restauration » ; enregistre."""
    index = _page_index(project, page_id)
    current = project.pages[index]
    restored = _load_version(project, page_id, version_id).model_copy(update={"id": page_id})
    _write_snapshot(project, current, "Avant restauration")
    if restored.source_sha256 != current.source_sha256 or restored.source_file != current.source_file:
        batch = _trash_batch(project, current, "restored")
        for path in _derived_files(project, current):
            _move_into(project, path, batch)
    missing = _recover_from_trash(project, restored)
    if not page_paths(project, restored).corrected.exists() and restored.status != PageStatus.IMPORTED:
        restored.status = PageStatus.IMPORTED       # image corrigée à régénérer : la page repasse au traitement
    if not page_paths(project, restored).thumbnail.exists() and restored.source_file not in missing:
        try:
            _write_thumbnail(project, restored, load_original(project, restored))
        except BookOcrError as error:
            logger.warning("Vignette non régénérée pour %s : %s", page_id, error)
    _mark_missing_files(restored, missing)
    project.pages[index] = restored
    save_project(project)
    return restored


# --------------------------------------------------------------------------- modifications des pages


def _saved_page(project: Project, page_id: str) -> Page | None:
    """Dernier état enregistré sur disque de la page (None si absent ou illisible)."""
    try:
        return _load_project_file(project_dir(project) / PROJECT_FILE).page(page_id)
    except BookOcrError:
        return None


def update_page(project: Project, page: Page, reason: str | None = None) -> None:
    """Remplace la page de même id et enregistre. Avec `reason`, l'ancienne version est d'abord historisée.

    Si `page` est l'objet même du projet (modifié sur place), l'ancienne version est lue sur disque.
    """
    index = _page_index(project, page.id)
    if reason:
        previous = project.pages[index]
        if previous is page:
            previous = _saved_page(project, page.id) or page
        _write_snapshot(project, previous, reason)
    project.pages[index] = page
    save_project(project)


def set_manual_rotation(project: Project, page_id: str, quarter_turns: int) -> Page:
    """Quart(s) de tour manuel(s) horaire(s) 0..3 appliqué(s) après l'orientation automatique ; enregistre.

    L'image corrigée n'est pas recalculée ici (processing_service.run_geometry).
    """
    if isinstance(quarter_turns, bool) or not isinstance(quarter_turns, int) or not 0 <= quarter_turns <= 3:
        raise ValueError("La rotation manuelle doit être un nombre de quarts de tour entre 0 et 3.")
    page = project.page(page_id)
    if page.geometry.manual_quarter_turns == quarter_turns:
        return page
    _write_snapshot(project, page, "Rotation manuelle")
    updated = page.model_copy(deep=True)
    updated.geometry.manual_quarter_turns = quarter_turns
    project.pages[_page_index(project, page_id)] = updated
    save_project(project)
    return updated


def reorder_pages(project: Project, ordered_ids: list[str]) -> None:
    """Nouvel ordre du livre : `ordered_ids` doit être une permutation exacte des pages ; enregistre."""
    current = [page.id for page in project.pages]
    if len(ordered_ids) != len(current) or set(ordered_ids) != set(current) or len(set(ordered_ids)) != len(current):
        raise ValueError("Le nouvel ordre doit contenir chaque page du projet exactement une fois.")
    by_id = {page.id: page for page in project.pages}
    project.pages = [by_id[page_id] for page_id in ordered_ids]
    save_project(project)


def replace_page_source(project: Project, page_id: str, name: str, data: bytes) -> Page:
    """Remplace la photo d'une page : même id, même position ; résultats de cette page seulement réinitialisés.

    Conservés : réglages d'amélioration, interrupteurs automatiques de géométrie, style_rules, format_override,
    category_override. Réinitialisés : géométrie détectée et manuelle (liée à l'ancienne photo), blocs, fonds,
    statut. L'ancien original reste dans originals/ (l'historique peut y revenir) ; les fichiers dérivés vont
    dans trash/<page_id>/<horodatage>-replaced/.
    """
    index = _page_index(project, page_id)
    old = project.pages[index]
    name = Path(name).name or "fichier"
    items = image_service.inspect_upload(name, data)            # erreurs remontées : rien n'a changé
    item = items[0]
    preview = _decode_for_thumbnail(name, data, item)
    sha256 = image_service.sha256_bytes(data)
    _write_snapshot(project, old, "Avant remplacement de la photo")
    batch = _trash_batch(project, old, "replaced")
    for path in _derived_files(project, old):
        _move_into(project, path, batch)
    source_file = _store_original(project, sha256, Path(name).suffix.lower(), data)
    geometry = GeometryCorrection(auto_crop=old.geometry.auto_crop, auto_orientation=old.geometry.auto_orientation,
                                  auto_deskew=old.geometry.auto_deskew, auto_dewarp=old.geometry.auto_dewarp)
    page = Page(id=old.id, source_file=source_file, source_index=item.index, source_sha256=sha256,
                original_name=name, source_width=item.width, source_height=item.height, geometry=geometry,
                enhancement=old.enhancement.model_copy(deep=True), category_override=old.category_override,
                format_override=old.format_override,
                style_rules={kind: style.model_copy(deep=True) for kind, style in old.style_rules.items()})
    if len(items) > 1:
        page.issues.append(Issue(code="replace_first_page_only", severity=Severity.INFO, page_id=page.id,
                                 message=f"« {name} » contient {len(items)} pages : seule la première est utilisée."))
    if any(other.id != page_id and (other.source_sha256, other.source_index) == (sha256, item.index)
           for other in project.pages):
        page.issues.append(Issue(code="duplicate_page", severity=Severity.WARNING, page_id=page.id,
                                 message="Cette photo est déjà utilisée par une autre page du projet."))
    _write_thumbnail(project, page, preview)
    project.pages[index] = page
    save_project(project)
    logger.info("Photo de la page %s remplacée par %s", page_id, name)
    return page


def delete_pages(project: Project, page_ids: list[str]) -> None:
    """Retire des pages : instantané dans trash/<page_id>/page.json, fichiers dérivés et historique déplacés
    dans trash/<page_id>/ ; un original n'y est déplacé que si plus aucune page ne le référence. Enregistre."""
    wanted = list(dict.fromkeys(page_ids))
    if not wanted:
        return
    positions = {page_id: _page_index(project, page_id) for page_id in wanted}   # PageNotFoundError avant tout
    removed = set(wanted)
    remaining = [page for page in project.pages if page.id not in removed]
    still_used = {page.source_file for page in remaining}
    root = project_dir(project)
    moved_sources: set[str] = set()
    for page_id in wanted:
        page = project.pages[positions[page_id]]
        trash_root = root / "trash" / page_id
        trash_root.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(_unique_path(trash_root / "page.json"), json.dumps(
            {"reason": "Suppression", "created_at": utc_now().isoformat(), "position": positions[page_id],
             "page": page.model_dump(mode="json")}, ensure_ascii=False))
        for path in _derived_files(project, page):
            _move_into(project, path, trash_root)
        history = _history_dir(project, page_id)
        if history.is_dir():
            _move_into(project, history, trash_root)
        original = root / page.source_file
        if page.source_file not in still_used and page.source_file not in moved_sources and original.is_file():
            _move_into(project, original, trash_root)
            moved_sources.add(page.source_file)
    project.pages = remaining
    save_project(project)
    logger.info("Page(s) supprimée(s) de %s vers la corbeille : %s", project.id, wanted)
