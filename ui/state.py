"""Session state of the interface: current project, history, selections, drafts, and helpers.

The disk is the source of truth: the `Project` object kept in `st.session_state` is reloaded from
`project.json` whenever the session is reset. Every validated change goes through `commit()`:
history snapshot, change, save.
"""
from __future__ import annotations

import hashlib
import importlib
import io
import logging
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Callable

import numpy as np
import streamlit as st
from PIL import Image

import config
from core.document_models import ImageAdjustments, Page, Project
from core.errors import LivreOCRError

logger = logging.getLogger("livreocr.ui")

SECTIONS = ["Vue d'ensemble", "Pages", "Traitement OCR", "Éditeur d'image", "Éditeur de blocs", "Export PDF"]

_DEFAULTS: dict[str, Any] = {
    "project_id": None,      # id of the open project
    "project": None,         # loaded Project (reloaded from disk when missing)
    "histories": {},         # project id -> services.history.History
    "nav": SECTIONS[0],
    "page_id": None,         # page selected in the editors
    "block_id": None,        # block selected in the block editor
    "local_id": None,        # local correction selected in the image editor
    "illus_id": None,        # illustration selected in the image editor
    "drafts": {},            # "<page>:<target>" -> ImageAdjustments dict (unsaved slider values)
    "geo_drafts": {},        # page id -> GeometrySettings dict
    "rev": 0,                # bumped after every external change: widget keys include it
    "box_nonces": [],        # box editor events already applied
    "flash": [],             # (kind, message) shown once after a rerun
}


def init() -> None:
    for key, value in _DEFAULTS.items():
        if key not in st.session_state:
            st.session_state[key] = value.copy() if isinstance(value, (dict, list)) else value


# --------------------------------------------------------------------------- lazy service imports

def svc(name: str):
    """Import a service or core module on demand (a module still being written by another
    agent must not stop the whole interface)."""
    return importlib.import_module(name)


def processing():
    return svc("services.processing_service")


def export():
    return svc("services.export_service")


def project_service():
    return svc("services.project_service")


def storage():
    return svc("services.storage_service")


# --------------------------------------------------------------------------- project and history

def project() -> Project | None:
    """The open project, reloaded from disk after a session reset."""
    pid = st.session_state.get("project_id")
    if not pid:
        return None
    current = st.session_state.get("project")
    if current is None or current.id != pid:
        try:
            current = storage().load_project(pid)
        except LivreOCRError as exc:
            st.session_state.project_id = None
            st.session_state.project = None
            flash("error", f"Projet impossible à ouvrir : {exc}")
            return None
        st.session_state.project = current
    return current


def open_project(project_id: str | None) -> None:
    st.session_state.project_id = project_id
    st.session_state.project = None
    st.session_state.page_id = None
    st.session_state.block_id = None
    st.session_state.local_id = None
    st.session_state.illus_id = None
    st.session_state.drafts = {}
    st.session_state.geo_drafts = {}
    bump()


def history():
    from services.history import History
    prj = project()
    histories = st.session_state.histories
    if prj is None:
        return History()
    if prj.id not in histories:
        histories[prj.id] = History(prj.id)
    return histories[prj.id]


def save(prj: Project | None = None) -> None:
    prj = prj or project()
    if prj is not None:
        storage().save_project(prj)


def bump() -> None:
    """Force every keyed widget to be re-created from the model on the next run."""
    st.session_state.rev = st.session_state.get("rev", 0) + 1


def rev() -> int:
    return st.session_state.get("rev", 0)


def k(*parts) -> str:
    """Widget key bound to the current revision."""
    return "_".join(str(p) for p in parts) + f"__{rev()}"


def commit(page: Page, label: str, change: Callable[[], Any]) -> Any:
    """history.record(page, label), then the change, then save. Returns what `change` returns."""
    prj = project()
    history().record(page, label)
    result = change()
    save(prj)
    bump()
    return result


def undo() -> str | None:
    prj = project()
    if prj is None:
        return None
    label = history().undo(prj)
    _after_history(prj)
    return label


def redo() -> str | None:
    prj = project()
    if prj is None:
        return None
    label = history().redo(prj)
    _after_history(prj)
    return label


def _after_history(prj: Project) -> None:
    save(prj)
    st.session_state.drafts = {}
    st.session_state.geo_drafts = {}
    bump()
    # A restored geometry may point to a frame file removed since: rebuild it (no detection).
    for page in prj.pages:
        if page.frame_path and not storage().ProjectStorage(prj.id).exists(page.frame_path):
            try:
                processing().ensure_frame(prj, page)
            except Exception:       # shown later as "page not prepared"
                logger.exception("cadre non reconstruit après annulation (page %s)", page.id)
                page.frame_path = ""
            save(prj)


# --------------------------------------------------------------------------- selections

def current_page(prj: Project) -> Page | None:
    if not prj.pages:
        return None
    page = prj.page(st.session_state.get("page_id") or "")
    if page is None:
        page = prj.pages[0]
        st.session_state.page_id = page.id
    return page


def page_label(prj: Project, page: Page) -> str:
    index = prj.page_index(page.id) + 1
    return f"Page {index} — {page.original_filename or page.id}"


def page_selector(prj: Project, key: str = "page_select", label: str = "Page") -> Page | None:
    """Selectbox of the pages, bound to st.session_state.page_id."""
    if not prj.pages:
        return None
    page = current_page(prj)
    ids = [p.id for p in prj.pages]
    choice = st.selectbox(label, ids, index=ids.index(page.id), key=k(key),
                          format_func=lambda pid: page_label(prj, prj.page(pid)) + _status_suffix(prj.page(pid)))
    if choice != st.session_state.page_id:
        st.session_state.page_id = choice
        st.session_state.block_id = None
        st.session_state.local_id = None
        st.session_state.illus_id = None
    return prj.page(choice)


STATUS_LABELS = {"imported": "importée", "processed": "traitée", "error": "en erreur"}


def _status_suffix(page: Page) -> str:
    text = f" ({STATUS_LABELS.get(page.status.value, page.status.value)}"
    if page.ocr_outdated:
        text += ", OCR obsolète"
    return text + ")"


def status_text(page: Page) -> str:
    return _status_suffix(page).strip(" ()")


# --------------------------------------------------------------------------- adjustment drafts

def draft_key(page: Page, target: str) -> str:
    return f"{page.id}:{target}"


def get_draft(key: str, saved: ImageAdjustments) -> dict:
    drafts = st.session_state.drafts
    if key not in drafts:
        drafts[key] = saved.model_dump(mode="json")
    return drafts[key]


def drop_draft(key: str) -> None:
    st.session_state.drafts.pop(key, None)


# --------------------------------------------------------------------------- messages and errors

def flash(kind: str, message: str) -> None:
    st.session_state.setdefault("flash", []).append((kind, message))


def show_flash() -> None:
    for kind, message in st.session_state.get("flash", []):
        getattr(st, kind, st.info)(message)
    st.session_state.flash = []


@contextmanager
def guard(action: str = "Opération"):
    """LivreOCRError -> st.error(message); anything else is logged and shown cleanly."""
    try:
        yield
    except LivreOCRError as exc:
        st.error(str(exc))
    except Exception as exc:        # RerunException/StopException derive from BaseException
        logger.exception("%s : erreur inattendue", action)
        st.error(f"{action} : erreur inattendue ({exc.__class__.__name__} : {exc}). "
                 "Le détail est enregistré dans le journal (workspace/logs/livreocr.log).")


# --------------------------------------------------------------------------- images

class _LRU(OrderedDict):
    def __init__(self, size: int):
        super().__init__()
        self.size = size

    def get_or(self, key, make: Callable[[], Any]):
        if key in self:
            self.move_to_end(key)
            return self[key]
        value = make()
        self[key] = value
        while len(self) > self.size:
            self.popitem(last=False)
        return value


_cache = _LRU(24)


def cached(key, make: Callable[[], Any]):
    """Small in-process cache for previews (keys are strings or tuples of strings)."""
    return _cache.get_or(key, make)


def digest(*parts) -> str:
    return hashlib.sha1("|".join(str(p) for p in parts).encode()).hexdigest()[:16]


def frame_ready(prj: Project, page: Page) -> bool:
    """The page frame exists on disk for the current geometry (no rebuild needed)."""
    try:
        return bool(page.frame_path) and page.frame_path == processing().frame_rel_path(page) and \
            storage().ProjectStorage(prj.id).exists(page.frame_path)
    except Exception:
        return False


def small_frame(prj: Project, page: Page, max_side: int | None = None) -> np.ndarray:
    """The page frame reduced to `max_side` (config.PREVIEW_MAX_SIDE): base of every preview."""
    max_side = max_side or config.PREVIEW_MAX_SIDE
    ps = processing()
    key = ("frame", prj.id, ps.frame_key(page), max_side)

    def make():
        frame = ps.ensure_frame(prj, page)
        from core import image_preprocessing
        small, _ = image_preprocessing.downscale(frame, max_side)
        return small
    return cached(key, make)


def prepare_frame(prj: Project, page: Page, redetect: bool = False) -> list[str]:
    """Explicit action: build the frame (and its detections) and save the project."""
    warnings = processing().rebuild_frame(prj, page, redetect=redetect)
    save(prj)
    return warnings


def to_png(image) -> bytes:
    if isinstance(image, np.ndarray):
        image = Image.fromarray(image)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()


def to_jpeg_data_uri(image, quality: int = 85) -> str:
    import base64
    if isinstance(image, np.ndarray):
        image = Image.fromarray(image)
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


def project_file_image(prj: Project, relative: str) -> np.ndarray | None:
    """A stored image of the project (thumbnail, illustration), or None if missing."""
    try:
        st_ = storage().ProjectStorage(prj.id)
        if not st_.exists(relative):
            return None
        with Image.open(st_.path(relative)) as im:
            return np.array(im.convert("RGB"))
    except Exception:
        logger.exception("image illisible : %s", relative)
        return None
