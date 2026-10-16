"""Project folders on disk: safe paths, atomic JSON saves, listing, zip backup and restore.

Layout of a project folder (workspace/projects/<project id>/):
    project.json      versioned document (core.document_models.Project)
    project.json.bak  previous save
    originals/        imported files, never modified
    work/             page frames and OCR images (regenerable)
    thumbnails/       page thumbnails (regenerable)
    illustrations/    extracted / replaced illustration sources (lossless PNG)
    exports/          generated PDFs and their reports
"""
from __future__ import annotations

import io
import json
import logging
import os
import re
import shutil
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path, PurePosixPath

from pydantic import ValidationError

import config
from core.document_models import Project, new_id, utcnow
from core.errors import ProjectError

logger = logging.getLogger("livreocr.storage")

SUBDIRS = ("originals", "work", "thumbnails", "illustrations", "exports")
PROJECT_FILE = "project.json"
PROJECT_ID = re.compile(r"^prj_[0-9a-f]{12}$")
MAX_ZIP_BYTES = 4 * 1024 ** 3


def projects_dir() -> Path:
    # Read from config at call time so tests (and LIVREOCR_WORKSPACE) can redirect it.
    path = Path(config.PROJECTS_DIR)
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ProjectError(f"Dossier de travail inaccessible : {path} ({exc.strerror}).")
    return path


def check_project_id(project_id: str) -> str:
    if not PROJECT_ID.match(project_id or ""):
        raise ProjectError("Identifiant de projet invalide.")
    return project_id


class ProjectStorage:
    """Files of one project. Every relative path goes through `path()`, which refuses escapes."""

    def __init__(self, project_id: str):
        self.project_id = check_project_id(project_id)
        self.root = (projects_dir() / project_id).resolve()

    def ensure(self) -> "ProjectStorage":
        try:
            for sub in SUBDIRS:
                (self.root / sub).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise ProjectError(f"Impossible de créer le dossier du projet ({exc.strerror}).")
        return self

    def path(self, relative: str) -> Path:
        """Absolute path of a project-relative path; refuses absolute paths and '..'."""
        if not relative:
            raise ProjectError("Chemin de fichier vide.")
        pure = PurePosixPath(relative)
        if pure.is_absolute() or ".." in pure.parts or "\\" in relative:
            raise ProjectError(f"Chemin refusé : {relative}")
        full = (self.root / pure).resolve()
        if self.root != full and self.root not in full.parents:
            raise ProjectError(f"Chemin hors du projet : {relative}")
        return full

    def relative(self, path: Path) -> str:
        return Path(path).resolve().relative_to(self.root).as_posix()

    def exists(self, relative: str) -> bool:
        try:
            return bool(relative) and self.path(relative).exists()
        except ProjectError:
            return False

    def write_bytes(self, relative: str, data: bytes) -> Path:
        target = self.path(relative)
        target.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(target, data)
        return target

    def read_bytes(self, relative: str) -> bytes:
        try:
            return self.path(relative).read_bytes()
        except OSError as exc:
            raise ProjectError(f"Fichier du projet inaccessible : {relative} ({exc.strerror}).")

    def remove(self, relative: str) -> None:
        if not relative:
            return
        try:
            self.path(relative).unlink(missing_ok=True)
        except OSError:
            logger.warning("suppression impossible : %s", relative)


def _atomic_write(target: Path, data: bytes) -> None:
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".tmp_", suffix=target.suffix)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, target)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save_project(project: Project) -> Path:
    """Write project.json atomically, keeping the previous version as project.json.bak."""
    storage = ProjectStorage(project.id).ensure()
    project.updated_at = utcnow()
    target = storage.root / PROJECT_FILE
    if target.exists():
        shutil.copy2(target, storage.root / (PROJECT_FILE + ".bak"))
    try:
        _atomic_write(target, project.to_json().encode("utf-8"))
    except OSError as exc:
        raise ProjectError(f"Sauvegarde impossible ({exc.strerror}).")
    return target


def parse_project(raw: bytes | str, source: str = "project.json") -> Project:
    """Validate a project JSON; errors are summarised for the user."""
    try:
        project = Project.from_json(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(p) for p in first.get("loc", ()))
        raise ProjectError(f"{source} invalide ({exc.error_count()} erreur(s)) ; première : {where} — {first.get('msg')}")
    except ValueError as exc:
        raise ProjectError(f"{source} illisible : {exc}")
    storage = ProjectStorage(project.id)
    for page in project.pages:      # every stored path must stay inside the project
        paths = [page.original_path, page.frame_path, page.ocr_image_path, page.thumbnail_path]
        paths += [p for b in page.image_blocks for p in (b.image_path, b.original_image_path)]
        for rel in filter(None, paths):
            storage.path(rel)
    return project


def load_project(project_id: str) -> Project:
    storage = ProjectStorage(project_id)
    target = storage.root / PROJECT_FILE
    if not target.exists():
        raise ProjectError("Projet introuvable.")
    try:
        return parse_project(target.read_bytes())
    except ProjectError:
        backup = storage.root / (PROJECT_FILE + ".bak")
        if backup.exists():
            logger.warning("project.json invalide pour %s, lecture de la sauvegarde précédente", project_id)
            return parse_project(backup.read_bytes(), "project.json.bak")
        raise


@dataclass
class ProjectSummary:
    id: str
    name: str
    updated_at: datetime
    pages: int
    error: str = ""


def list_projects() -> list[ProjectSummary]:
    found = []
    for folder in sorted(projects_dir().iterdir()):
        if not folder.is_dir() or not PROJECT_ID.match(folder.name) or not (folder / PROJECT_FILE).exists():
            continue
        try:
            data = json.loads((folder / PROJECT_FILE).read_text("utf-8"))
            found.append(ProjectSummary(folder.name, str(data.get("name", folder.name)),
                                        datetime.fromisoformat(data["updated_at"]), len(data.get("pages", []))))
        except (OSError, ValueError, KeyError, TypeError) as exc:
            found.append(ProjectSummary(folder.name, folder.name, datetime.fromtimestamp(folder.stat().st_mtime),
                                        0, error=f"illisible ({exc.__class__.__name__})"))
    return sorted(found, key=lambda s: s.updated_at, reverse=True)


def delete_project(project_id: str) -> None:
    storage = ProjectStorage(project_id)
    if storage.root.exists():
        shutil.rmtree(storage.root)


def export_zip(project: Project, include_work: bool = False) -> bytes:
    """A self-contained backup: project.json, originals, illustrations (and optionally work files).
    Regenerable files (frames, OCR images, thumbnails) are left out by default."""
    save_project(project)
    storage = ProjectStorage(project.id)
    keep = {"originals", "illustrations", "exports"} | ({"work", "thumbnails"} if include_work else set())
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(storage.root / PROJECT_FILE, PROJECT_FILE)
        for sub in sorted(keep):
            folder = storage.root / sub
            if folder.exists():
                for file in sorted(folder.rglob("*")):
                    if file.is_file() and not file.name.startswith(".tmp_"):
                        archive.write(file, storage.relative(file))
    return buffer.getvalue()


def import_zip(data: bytes) -> Project:
    """Restore a backup made by export_zip, as a new project (new id when the id already exists)."""
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise ProjectError("Archive ZIP invalide.")
    with archive:
        names = archive.namelist()
        if PROJECT_FILE not in names:
            raise ProjectError("L'archive ne contient pas de project.json.")
        if sum(i.file_size for i in archive.infolist()) > MAX_ZIP_BYTES:
            raise ProjectError("Archive trop volumineuse une fois décompressée.")
        project = parse_project(archive.read(PROJECT_FILE), "project.json de l'archive")
        if (projects_dir() / project.id).exists():
            project.id = new_id("prj_")
        storage = ProjectStorage(project.id).ensure()
        for info in archive.infolist():
            if info.is_dir() or info.filename == PROJECT_FILE:
                continue
            top = PurePosixPath(info.filename).parts[0] if PurePosixPath(info.filename).parts else ""
            if top not in SUBDIRS:
                continue
            target = storage.path(info.filename)       # refuses absolute paths and '..' (zip slip)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst)
    missing = [p.original_path for p in project.pages if not storage.exists(p.original_path)]
    if missing:
        logger.warning("archive importée sans %d original(aux)", len(missing))
    save_project(project)
    return project
