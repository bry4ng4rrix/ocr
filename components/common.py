"""Aides partagées par les écrans : état d'affichage, chargement du projet, libellés et messages.

Règles (AGENTS.md) : `st.session_state` ne garde que l'état d'affichage (projet ouvert, page et bloc
sélectionnés, messages à afficher) ; le projet est relu sur disque à chaque exécution et toute
modification passe par `project_service` sous `project_service.project_lock`.
"""
from __future__ import annotations

import base64
import importlib
import io
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

import numpy as np
import streamlit as st
from PIL import Image

import config
from models import (Alignment, BlockKind, ImageKind, Issue, Page, PageCategory, PageStatus, Project,
                    ReconstructionMode, Severity)
from models.errors import BookOcrError, ProcessingError
from services import image_service, processing_service, project_service

logger = logging.getLogger("bookocr.ui")

# Écrans (chemins relatifs à app.py) : utilisés par st.Page, st.page_link et st.switch_page.
SCREEN_DASHBOARD = "components/screens/dashboard.py"
SCREEN_PAGES = "components/screens/pages.py"
SCREEN_CORRECTION = "components/screens/correction.py"
SCREEN_PROCESSING = "components/screens/processing.py"
SCREEN_EDITOR = "components/screens/editor.py"
SCREEN_TYPOGRAPHY = "components/screens/typography.py"
SCREEN_REVIEW = "components/screens/review.py"
SCREEN_EXPORT = "components/screens/export.py"

STATE_DEFAULTS: dict[str, object] = {
    "project_id": None,       # projet ouvert
    "page_id": None,          # page sélectionnée (tous les écrans)
    "block_id": None,         # bloc sélectionné dans l'éditeur
    "flash": [],              # messages à afficher à la prochaine exécution [(niveau, texte)]
}

STATUS_LABELS = {
    PageStatus.IMPORTED: "Importée",
    PageStatus.CORRECTED: "Corrigée",
    PageStatus.OCR_DONE: "Texte reconnu",
    PageStatus.ANALYSED: "Analysée",
    PageStatus.ERROR: "En erreur",
}
STATUS_COLORS = {
    PageStatus.IMPORTED: "gray",
    PageStatus.CORRECTED: "blue",
    PageStatus.OCR_DONE: "violet",
    PageStatus.ANALYSED: "green",
    PageStatus.ERROR: "red",
}
CATEGORY_LABELS = {
    PageCategory.TEXT: "Texte seul (cas A)",
    PageCategory.GRAPHIC_BACKGROUND: "Texte sur fond graphique (cas B)",
    PageCategory.PHOTO_TEXT: "Photos et texte (cas C)",
    PageCategory.TEXT_ON_IMAGE: "Texte sur image",
    PageCategory.MIXED: "Mixte",
    PageCategory.UNKNOWN: "Inconnue",
}
BLOCK_KIND_LABELS = {
    BlockKind.HEADING_1: "Titre 1",
    BlockKind.HEADING_2: "Titre 2",
    BlockKind.PARAGRAPH: "Paragraphe",
    BlockKind.LIST_ITEM: "Élément de liste",
    BlockKind.CAPTION: "Légende",
    BlockKind.HEADER: "En-tête",
    BlockKind.FOOTER: "Pied de page",
    BlockKind.PAGE_NUMBER: "Numéro de page",
    BlockKind.NOTE: "Note",
    BlockKind.TABLE: "Tableau",
    BlockKind.SPECIAL: "Zone spéciale",
}
IMAGE_KIND_LABELS = {
    ImageKind.PHOTO: "Photo",
    ImageKind.ILLUSTRATION: "Illustration",
    ImageKind.DRAWING: "Dessin",
    ImageKind.TABLE_IMAGE: "Tableau (image)",
}
ALIGNMENT_LABELS = {
    Alignment.LEFT: "Gauche",
    Alignment.CENTER: "Centré",
    Alignment.RIGHT: "Droite",
    Alignment.JUSTIFY: "Justifié",
}
MODE_SHORT_LABELS = {
    ReconstructionMode.FIDELITY: "Fidélité",
    ReconstructionMode.EDITABLE: "Modifiable",
    ReconstructionMode.HYBRID: "Hybride",
}
STYLE_FIELD_LABELS = {
    "font_family": "Police", "font_size_pt": "Taille (pt)", "bold": "Gras", "italic": "Italique",
    "underline": "Souligné", "color": "Couleur", "alignment": "Alignement", "line_spacing": "Interligne",
    "space_before_pt": "Espace avant (pt)", "space_after_pt": "Espace après (pt)",
    "first_line_indent_pt": "Retrait 1re ligne (pt)", "left_indent_pt": "Retrait gauche (pt)",
    "right_indent_pt": "Retrait droit (pt)",
}
SEVERITY_LABELS = {Severity.INFO: "Information", Severity.WARNING: "Avertissement", Severity.ERROR: "Erreur"}


# --------------------------------------------------------------------------- état d'affichage


def init_state() -> None:
    for name, default in STATE_DEFAULTS.items():
        if name not in st.session_state:
            st.session_state[name] = list(default) if isinstance(default, list) else default


def flash(level: str, message: str) -> None:
    """Message affiché en haut de la prochaine exécution (après un st.rerun ou un changement d'écran)."""
    st.session_state.setdefault("flash", []).append((level, message))


def show_flashes() -> None:
    messages = st.session_state.get("flash") or []
    st.session_state["flash"] = []
    for level, message in messages:
        {"success": st.success, "warning": st.warning, "error": st.error}.get(level, st.info)(message)


def select_page(page_id: str | None, block_id: str | None = None) -> None:
    st.session_state["page_id"] = page_id
    st.session_state["block_id"] = block_id


def sync_widgets(scope: str, values: dict[str, object], signature: str) -> None:
    """Valeurs initiales des widgets `<scope>_<champ>` (créés ensuite avec `key=` seulement).

    Quand l'état enregistré change (`signature` différente : autre page, restauration, enregistrement), tous les
    widgets sont réinitialisés ; sinon seules les clés absentes le sont, et les saisies en cours sont gardées.
    """
    base_key = f"_base_{scope}"
    changed = st.session_state.get(base_key) != signature
    for name, value in values.items():
        widget_key = f"{scope}_{name}"
        if changed or widget_key not in st.session_state:
            st.session_state[widget_key] = value
    st.session_state[base_key] = signature


def widget_values(scope: str, names: list[str]) -> dict[str, object]:
    return {name: st.session_state.get(f"{scope}_{name}") for name in names}


def forget_widgets(scope: str) -> None:
    """Oublie les saisies d'un groupe de widgets (à appeler avant leur création, ou dans un rappel)."""
    for widget_key in [key for key in st.session_state if str(key).startswith(f"{scope}_")]:
        del st.session_state[widget_key]
    st.session_state.pop(f"_base_{scope}", None)


def optional_service(name: str) -> ModuleType | None:
    """Service importé s'il est disponible ; None (journalisé) s'il n'est pas encore livré ou ne s'importe pas."""
    try:
        return importlib.import_module(f"services.{name}")
    except ImportError as error:
        logger.warning("Service %s indisponible : %s", name, error)
        return None


# --------------------------------------------------------------------------- projet


@st.cache_data(max_entries=8, show_spinner=False)
def _read_project(project_id: str, stamp: tuple[int, int]) -> Project:
    """Projet relu sur disque ; la clé `stamp` (date et taille de project.json) invalide le cache à chaque
    enregistrement. Chaque appel renvoie une copie indépendante."""
    return project_service.open_project(project_id)


def _project_stamp(project_id: str) -> tuple[int, int]:
    path = project_service.project_dir(project_id) / project_service.PROJECT_FILE
    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


def load_project() -> Project | None:
    """Projet ouvert, relu depuis le disque ; None si aucun projet n'est ouvert ou s'il est illisible."""
    project_id = st.session_state.get("project_id")
    if not project_id:
        return None
    try:
        return _read_project(project_id, _project_stamp(project_id))
    except BookOcrError as error:
        logger.warning("Projet %s impossible à ouvrir : %s", project_id, error)
        st.error(error.user_message)
        st.session_state["project_id"] = None
        return None


def require_project() -> Project:
    """Projet ouvert, sinon message et lien vers le tableau de bord (l'écran s'arrête là)."""
    project = load_project()
    if project is None:
        st.info("Aucun projet ouvert. Créez ou ouvrez un projet depuis le tableau de bord.",
                icon=":material/folder_open:")
        st.page_link(SCREEN_DASHBOARD, label="Aller au tableau de bord", icon=":material/dashboard:")
        st.stop()
    return project


def require_pages(project: Project) -> None:
    if not project.pages:
        st.info("Ce projet ne contient encore aucune page. Importez des photos depuis l'écran Pages.",
                icon=":material/photo_library:")
        st.page_link(SCREEN_PAGES, label="Aller à l'écran Pages", icon=":material/photo_library:")
        st.stop()


@contextmanager
def editing(project_id: str) -> Iterator[Project]:
    """Projet relu sur disque SOUS VERROU : à utiliser pour toute modification (le traitement en
    arrière-plan enregistre aussi, page par page, sous le même verrou)."""
    with project_service.project_lock(project_id):
        yield project_service.open_project(project_id)


class ActionResult:
    """Issue d'une action utilisateur : `ok` passe à True si aucune erreur n'a été levée."""

    def __init__(self) -> None:
        self.ok = False


@contextmanager
def user_action(success: str | None = None) -> Iterator[ActionResult]:
    """Exécute une action utilisateur : erreurs métier affichées en français ; en cas de succès, `result.ok`
    vaut True et le message `success` est affiché à la prochaine exécution (typiquement après st.rerun)."""
    result = ActionResult()
    try:
        yield result
    except BookOcrError as error:
        logger.warning("Action refusée : %s", error)
        st.error(error.user_message, icon=":material/error:")
        return
    except ValueError as error:          # validations des services et des modèles (message déjà explicite)
        logger.warning("Valeur refusée : %s", error)
        st.error(f"Valeur refusée : {error}", icon=":material/error:")
        return
    result.ok = True
    if success:
        flash("success", success)


def save_page(project_id: str, page: Page, reason: str) -> None:
    """Remplace la page (copie modifiée) dans le projet relu sous verrou ; instantané « reason »."""
    with editing(project_id) as fresh:
        fresh.page(page.id)                         # PageNotFoundError si supprimée entre-temps
        project_service.update_page(fresh, page, reason)


def process_now(project_id: str, page_ids: list[str], steps: list, *, redetect: bool = False,
                label: str = "Traitement de la page…"):
    """Traitement synchrone de quelques pages (après une rotation ou un réglage) ; ProcessingError si une page
    échoue (le message du service est repris)."""
    with st.spinner(label):
        report = processing_service.process_pages(project_service.open_project(project_id), page_ids, steps,
                                                  redetect=redetect)
    if report.failed:
        raise ProcessingError(" ".join(report.failed.values()))
    return report


def locked_page_ids(project_id: str) -> set[str]:
    """Pages encore à traiter par le traitement en arrière-plan en cours : leur édition est désactivée."""
    if not processing_service.is_running(project_id):
        return set()
    status = processing_service.job_status(project_id)
    if status is None:
        return set()
    return set(status.page_ids[max(0, status.done):])


def warn_if_locked(project_id: str, page_id: str) -> bool:
    """Affiche un avertissement et renvoie True si la page est en cours de traitement."""
    if page_id in locked_page_ids(project_id):
        st.warning("Cette page est en cours de traitement en arrière-plan : la modification est désactivée "
                   "jusqu'à la fin du traitement.", icon=":material/hourglass_top:")
        return True
    return False


# --------------------------------------------------------------------------- libellés


def page_label(project: Project, page: Page) -> str:
    number = project.page_number(page.id)
    name = page.original_name
    if page.source_index:
        name += f" (p. {page.source_index + 1})"
    return f"{number}. {name}"


def confidence_label(page: Page) -> str:
    return f"{page.mean_confidence:.0f} %" if page.mean_confidence is not None else "—"


def page_selector(project: Project, *, key: str, label: str = "Page",
                  page_ids: list[str] | None = None) -> Page | None:
    """Liste déroulante des pages, synchronisée avec la page sélectionnée (partagée entre les écrans)."""
    ids = page_ids if page_ids is not None else [page.id for page in project.pages]
    if not ids:
        return None
    current = st.session_state.get("page_id")
    if current not in ids:
        current = ids[0]
        st.session_state["page_id"] = current
    widget_key = f"_page_selector_{key}"
    if st.session_state.get(widget_key) not in ids or st.session_state.get(widget_key) != current:
        st.session_state[widget_key] = current
    by_id = {page.id: page for page in project.pages}

    def remember() -> None:
        select_page(st.session_state[widget_key])

    st.selectbox(label, ids, key=widget_key, on_change=remember,
                 format_func=lambda page_id: f"{page_label(project, by_id[page_id])} — "
                                             f"{STATUS_LABELS[by_id[page_id].status]}")
    return by_id[current]


def show_issues(issues: list[Issue], *, empty: str | None = None) -> None:
    if not issues and empty:
        st.caption(empty)
    for issue in issues:
        if issue.severity == Severity.ERROR:
            st.error(issue.message, icon=":material/error:")
        elif issue.severity == Severity.WARNING:
            st.warning(issue.message, icon=":material/warning:")
        else:
            st.info(issue.message, icon=":material/info:")


# --------------------------------------------------------------------------- images


@st.cache_data(max_entries=32, show_spinner=False)
def _scaled_image(path: str, source_index: int, stamp: int, max_side: int) -> np.ndarray:
    image = image_service.read_image(Path(path), source_index)
    return image_service.downscale(image, max_side)[0]


def scaled_image(path: Path, max_side: int = config.PREVIEW_MAX_SIDE, source_index: int = 0) -> np.ndarray | None:
    """Copie réduite d'une image du projet (None si le fichier n'existe pas encore). ImageReadError possible."""
    if not path.exists():
        return None
    return _scaled_image(str(path), source_index, path.stat().st_mtime_ns, max_side)


def original_image(project: Project, page: Page, max_side: int = config.PREVIEW_MAX_SIDE) -> np.ndarray | None:
    paths = project_service.page_paths(project, page)
    return scaled_image(paths.original, max_side, page.source_index)


def corrected_image(project: Project, page: Page, max_side: int = config.PREVIEW_MAX_SIDE) -> np.ndarray | None:
    return scaled_image(project_service.page_paths(project, page).corrected, max_side)


def working_image(project: Project, page: Page, max_side: int = config.PREVIEW_MAX_SIDE) -> np.ndarray | None:
    """Image de travail (améliorée si des réglages sont appliqués, sinon corrigée), réduite."""
    return scaled_image(processing_service.working_image_path(project, page), max_side)


def jpeg_data_uri(image: np.ndarray | Image.Image, quality: int = 85) -> str:
    """Encodage d'affichage (data URI) pour les composants personnalisés."""
    picture = image if isinstance(image, Image.Image) else Image.fromarray(image)
    buffer = io.BytesIO()
    picture.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")


@st.cache_data(max_entries=12, show_spinner=False)
def _preview(project_json: str, page_id: str, mode: str, width: int) -> tuple[Image.Image, list[Issue]]:
    from services import export_service

    cached = Project.model_validate_json(project_json)
    image, _composed, issues = export_service.preview_page(cached, cached.page(page_id), ReconstructionMode(mode),
                                                          width_px=width)
    return image, issues


def preview_image(project: Project, page: Page, mode: ReconstructionMode | None = None,
                  width: int = 620) -> tuple[Image.Image, list[Issue]]:
    """Aperçu reconstruit (rendu PDF rasterisé de la composition, D-08), recalculé quand le projet enregistré
    change. Erreurs métier remontées (BookOcrError)."""
    return _preview(project.model_dump_json(), page.id, (mode or project.export.mode).value, width)
