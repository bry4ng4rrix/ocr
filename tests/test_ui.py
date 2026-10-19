"""Interface Streamlit (AppTest) : chaque écran s'affiche avec et sans projet ; les interactions principales sont
vérifiées sur le projet RELU DEPUIS LE DISQUE ; un lancement réel répond sur /_stcore/health."""
from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pymupdf
import pytest
import streamlit as st
from docx import Document
from streamlit.testing.v1 import AppTest

from components import common
from models import PageStatus
from services import fonts, processing_service, project_service
from tests.conftest import ROOT, requires_languages, sha256
from tests.fixtures import synthetic

pytestmark = pytest.mark.ui

APP = str(ROOT / "app.py")
SCREENS = [common.SCREEN_DASHBOARD, common.SCREEN_PAGES, common.SCREEN_CORRECTION, common.SCREEN_PROCESSING,
           common.SCREEN_EDITOR, common.SCREEN_TYPOGRAPHY, common.SCREEN_REVIEW, common.SCREEN_EXPORT]
TIMEOUT = 180


@pytest.fixture(autouse=True)
def _fresh_caches():
    st.cache_data.clear()
    yield
    st.cache_data.clear()


def _app(**state) -> AppTest:
    at = AppTest.from_file(APP, default_timeout=TIMEOUT)
    for name, value in state.items():
        at.session_state[name] = value
    return at


def _open(screen: str, **state) -> AppTest:
    at = _app(**state).run()
    if screen != common.SCREEN_DASHBOARD:
        at.switch_page(screen).run()
    _assert_ok(at)
    return at


def _assert_ok(at: AppTest) -> None:
    assert not at.exception, [item.value for item in at.exception]


def _button(at: AppTest, label: str):
    matches = [button for button in at.button if button.label == label]
    assert matches, f"bouton « {label} » absent : {[button.label for button in at.button]}"
    return matches[0]


def _synthetic_files(folder: Path, pages) -> list[tuple[str, bytes]]:
    return [(path.name, path.read_bytes()) for path in (synthetic.save(page, folder, ".jpg") for page in pages)]


@pytest.fixture
def project_with_pages(data_dir, tmp_path):
    """Projet de deux pages synthétiques importées (non traitées)."""
    project = project_service.create_project("Livre d'essai", ["fra"])
    report = project_service.import_files(project, _synthetic_files(
        tmp_path / "src", [synthetic.text_page(), synthetic.photo_text_page()]))
    assert len(report.added_page_ids) == 2
    return project_service.open_project(project.id)


@pytest.fixture(scope="module")
def _analysed_template(tmp_path_factory):
    """Projet analysé une seule fois pour le module (OCR réel) ; copié dans le dossier de chaque test."""
    if "fra" not in synthetic_languages():
        pytest.skip("Tesseract ou la langue fra est absent")
    root = tmp_path_factory.mktemp("analysed")
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("BOOKOCR_DATA_DIR", str(root / "data"))
        project = project_service.create_project("Livre analysé", ["fra"])
        project_service.import_files(project, _synthetic_files(
            root / "src", [synthetic.text_page(), synthetic.photo_text_page()]))
        report = processing_service.process_pages(project_service.open_project(project.id), None,
                                                  processing_service.ALL_STEPS)
        assert report.failed == {}
    return root / "data" / "projects" / project.id


def synthetic_languages() -> set[str]:
    from tests.conftest import TESSERACT_LANGS
    return TESSERACT_LANGS


@pytest.fixture
def analysed_project(data_dir, _analysed_template):
    target = data_dir / "projects" / _analysed_template.name
    shutil.copytree(_analysed_template, target)
    project = project_service.open_project(_analysed_template.name)
    assert all(page.status == PageStatus.ANALYSED for page in project.pages)
    return project


# --------------------------------------------------------------------------- affichage


@pytest.mark.parametrize("screen", SCREENS)
def test_screen_without_project(data_dir, screen):
    at = _open(screen)
    if screen != common.SCREEN_DASHBOARD:
        assert any("Aucun projet ouvert" in info.value for info in at.info)


@pytest.mark.parametrize("screen", SCREENS)
def test_screen_with_imported_pages(project_with_pages, screen):
    project = project_with_pages
    at = _open(screen, project_id=project.id)
    assert not at.error, [error.value for error in at.error]
    assert any(project.name in item.value for item in at.sidebar.markdown)


@requires_languages("fra")
@pytest.mark.parametrize("screen", SCREENS)
def test_screen_with_analysed_pages(analysed_project, screen):
    project = analysed_project
    page = project.pages[1]
    for block_id in (page.text_blocks[0].id, page.image_blocks[0].id if page.image_blocks else None):
        at = _open(screen, project_id=project.id, page_id=page.id, block_id=block_id)
        assert not at.error, [error.value for error in at.error]


def test_dashboard_shows_counts(analysed_project):
    at = _open(common.SCREEN_DASHBOARD, project_id=analysed_project.id)
    metrics = {metric.label: metric.value for metric in at.metric}
    assert metrics["Pages"] == "2"
    assert metrics["Pages analysées"] == "2"
    assert metrics["Pages en erreur"] == "0"


# --------------------------------------------------------------------------- interactions (effets relus sur disque)


def test_create_project(data_dir):
    at = _open(common.SCREEN_DASHBOARD)
    at.text_input[0].set_value("Mon livre")
    languages = ["fra"] if "fra" in synthetic_languages() else list(at.multiselect[0].options[:1])
    if not languages:
        pytest.skip("aucune langue Tesseract installée")
    at.multiselect[0].set_value(languages)
    at.form_submit_button[0].click().run()
    _assert_ok(at)
    summaries = project_service.list_projects()
    assert [summary.name for summary in summaries] == ["Mon livre"]
    assert at.session_state["project_id"] == summaries[0].id
    assert project_service.open_project(summaries[0].id).languages == languages


def test_import_pages(data_dir, tmp_path):
    project = project_service.create_project("Import", ["fra"])
    at = _open(common.SCREEN_PAGES, project_id=project.id)
    files = _synthetic_files(tmp_path, [synthetic.text_page(), synthetic.photo_text_page()])
    at.file_uploader[0].set_value([(name, data, "image/jpeg") for name, data in files])
    _button(at, "Importer").click().run()
    _assert_ok(at)
    reloaded = project_service.open_project(project.id)
    assert [page.original_name for page in reloaded.pages] == [name for name, _ in files]
    assert any("2 page(s) importée(s)" in item.value for item in at.success)
    for page in reloaded.pages:
        assert project_service.page_paths(reloaded, page).original.exists()


def test_manual_rotation(project_with_pages):
    project = project_with_pages
    page = project.pages[0]
    original = project_service.page_paths(project, page).original
    before = sha256(original)
    at = _open(common.SCREEN_PAGES, project_id=project.id, page_id=page.id)
    at.segmented_control(key=f"rotation_{page.id}").set_value(1).run()
    _button(at, "Appliquer la rotation").click().run()
    _assert_ok(at)
    assert not at.error, [error.value for error in at.error]
    reloaded = project_service.open_project(project.id).page(page.id)
    assert reloaded.geometry.manual_quarter_turns == 1
    assert reloaded.status == PageStatus.CORRECTED
    assert project_service.page_paths(project, reloaded).corrected.exists()
    assert sha256(original) == before                         # l'original n'est jamais modifié
    assert any(version.reason == "Rotation manuelle"
               for version in project_service.list_page_versions(project, page.id))


def test_reorder_pages(project_with_pages):
    project = project_with_pages
    first, second = (page.id for page in project.pages)
    at = _open(common.SCREEN_PAGES, project_id=project.id)
    at.selectbox(key="move_page").set_value(second).run()
    at.number_input(key="move_target").set_value(1).run()
    _button(at, "Déplacer").click().run()
    _assert_ok(at)
    assert [page.id for page in project_service.open_project(project.id).pages] == [second, first]


def test_delete_page_requires_confirmation(project_with_pages):
    project = project_with_pages
    page = project.pages[0]
    at = _open(common.SCREEN_PAGES, project_id=project.id, page_id=page.id)
    assert _button(at, "Supprimer cette page").disabled
    at.checkbox(key=f"confirm_delete_{page.id}").check().run()
    _button(at, "Supprimer cette page").click().run()
    _assert_ok(at)
    remaining = project_service.open_project(project.id).pages
    assert [item.id for item in remaining] == [project.pages[1].id]


def test_enhancement_applied_and_saved(project_with_pages):
    project = project_with_pages
    page = project.pages[0]
    processing_service.process_pages(project, [page.id], [processing_service.Step.GEOMETRY])
    at = _open(common.SCREEN_CORRECTION, project_id=project.id, page_id=page.id)
    at.slider(key=f"enh_{page.id}_contrast").set_value(30.0).run()
    _button(at, "Appliquer les réglages").click().run()
    _assert_ok(at)
    reloaded = project_service.open_project(project.id).page(page.id)
    assert reloaded.enhancement.contrast == 30.0
    assert project_service.page_paths(project, reloaded).enhanced.exists()


@requires_languages("fra")
def test_edit_text_block_and_style(analysed_project):
    project = analysed_project
    page = project.pages[0]
    block = next(item for item in page.text_blocks if item.kind.value == "paragraph")
    scope = f"blk_{block.id}"
    at = _open(common.SCREEN_EDITOR, project_id=project.id, page_id=page.id, block_id=block.id)
    at.text_area(key=f"{scope}_text").set_value("Texte corrigé à la main.").run()
    at.number_input(key=f"{scope}_font_size_pt").set_value(17.0).run()
    at.toggle(key=f"{scope}_bold").set_value(True).run()
    at.segmented_control(key=f"{scope}_alignment").set_value("center").run()
    at.button(key="save_text_block").click().run()
    _assert_ok(at)

    saved = project_service.open_project(project.id).page(page.id).text_block(block.id)
    assert saved.corrected_text == "Texte corrigé à la main."
    assert saved.ocr_text == block.ocr_text                       # le texte OCR d'origine reste consultable
    assert saved.style_override.font_size_pt == 17.0
    assert saved.style_override.bold is True
    assert saved.style_override.alignment == "center"
    assert saved.style_override.font_family is None               # champ non touché : toujours hérité
    assert any(version.reason == "Modification du bloc"
               for version in project_service.list_page_versions(project, page.id))

    # Nouvelle session (redémarrage) : la modification est toujours là.
    restarted = _open(common.SCREEN_EDITOR, project_id=project.id, page_id=page.id, block_id=block.id)
    assert restarted.text_area(key=f"{scope}_text").value == "Texte corrigé à la main."
    assert restarted.number_input(key=f"{scope}_font_size_pt").value == 17.0


@requires_languages("fra")
def test_autosave_of_pending_edit(analysed_project):
    project = analysed_project
    with project_service.project_lock(project.id):
        fresh = project_service.open_project(project.id)
        fresh.autosave_seconds = 1
        project_service.save_project(fresh)
    page = project.pages[0]
    block = page.text_blocks[0]
    scope = f"blk_{block.id}"
    at = _open(common.SCREEN_EDITOR, project_id=project.id, page_id=page.id, block_id=block.id)
    at.text_area(key=f"{scope}_text").set_value("Brouillon enregistré automatiquement.").run()
    assert project_service.open_project(project.id).page(page.id).text_block(block.id).corrected_text is None
    time.sleep(1.2)
    at.run()
    _assert_ok(at)
    saved = project_service.open_project(project.id).page(page.id).text_block(block.id)
    assert saved.corrected_text == "Brouillon enregistré automatiquement."
    assert any(version.reason == "Sauvegarde automatique"
               for version in project_service.list_page_versions(project, page.id))


@requires_languages("fra")
def test_edit_page_settings_and_reading_order(analysed_project):
    project = analysed_project
    page = project.pages[1]
    blocks = page.blocks_in_order()
    last = blocks[-1]
    at = _open(common.SCREEN_EDITOR, project_id=project.id, page_id=page.id, block_id=last.id)
    at.number_input(key=f"order_{last.id}").set_value(1).run()
    at.button(key="place_block").click().run()
    _assert_ok(at)
    reordered = project_service.open_project(project.id).page(page.id)
    assert reordered.blocks_in_order()[0].id == last.id

    at = _open(common.SCREEN_EDITOR, project_id=project.id, page_id=page.id)
    at.selectbox(key=f"pagecfg_{page.id}_format").set_value("A4")
    _button(at, "Enregistrer les réglages de la page").click().run()
    _assert_ok(at)
    assert project_service.open_project(project.id).page(page.id).format_override.name == "A4"


@requires_languages("fra")
def test_global_font_change(analysed_project):
    project = analysed_project
    families = sorted(fonts.available_families())
    target = next((name for name in families if name != project.typography.document_font), None)
    if target is None:
        pytest.skip("une seule famille de polices installée")
    at = _open(common.SCREEN_TYPOGRAPHY, project_id=project.id)
    at.selectbox(key="typo_document_font").set_value(target)
    at.number_input(key="typo_body_size").set_value(12.5)
    _button(at, "Appliquer et enregistrer").click().run()
    _assert_ok(at)
    assert not at.error, [error.value for error in at.error]
    reloaded = project_service.open_project(project.id)
    assert reloaded.typography.document_font == target
    assert any(style.font_size_pt == 12.5 for style in reloaded.typography.kind_styles.values())


@requires_languages("fra")
def test_export_docx_and_pdf(analysed_project):
    project = analysed_project
    expected_words = {"lendemain", "vallée"}
    at = _open(common.SCREEN_EXPORT, project_id=project.id)
    at.button(key="export_docx").click().run()
    _assert_ok(at)
    assert not at.error, [error.value for error in at.error]
    docx_files = sorted((project_service.project_dir(project) / "exports").glob("*.docx"))
    assert len(docx_files) == 1
    document = Document(str(docx_files[0]))
    text = " ".join(paragraph.text for paragraph in document.paragraphs)
    assert all(word in text for word in expected_words), text[:300]
    assert len(document.sections) == len(project.pages)

    at.button(key="export_pdf").click().run()
    _assert_ok(at)
    assert not at.error, [error.value for error in at.error]
    pdf_files = sorted((project_service.project_dir(project) / "exports").glob("*.pdf"))
    assert len(pdf_files) == 1
    with pymupdf.open(pdf_files[0]) as pdf:
        assert pdf.page_count == len(project.pages)
        pdf_text = " ".join(page.get_text() for page in pdf)
    assert all(word in pdf_text for word in expected_words), pdf_text[:300]
    assert at.download_button, "bouton de téléchargement absent"
    assert at.image, "aperçu des pages du PDF absent"
    assert _button(at, "PDF via LibreOffice (DOCX → PDF)").disabled == (shutil.which("soffice") is None)


@requires_languages("fra")
def test_restore_previous_version(analysed_project):
    project = analysed_project
    page = project.pages[0]
    block = page.text_blocks[0]
    edited = page.model_copy(deep=True)
    edited.text_block(block.id).corrected_text = "Version fautive"
    project_service.update_page(project, edited, "Essai de restauration")
    at = _open(common.SCREEN_REVIEW, project_id=project.id, page_id=page.id)
    version = project_service.list_page_versions(project, page.id)[0]
    at.selectbox(key=f"version_{page.id}").set_value(version.version_id).run()
    at.checkbox(key=f"confirm_restore_{page.id}").check().run()
    _button(at, "Restaurer cette version").click().run()
    _assert_ok(at)
    restored = project_service.open_project(project.id).page(page.id).text_block(block.id)
    assert restored.corrected_text == block.corrected_text


@requires_languages("fra")
def test_processing_screen_runs_background_job(project_with_pages):
    project = project_with_pages
    at = _open(common.SCREEN_PROCESSING, project_id=project.id)
    at.pills(key="ocr_steps").set_value([processing_service.Step.GEOMETRY]).run()
    _button(at, "Lancer le traitement").click().run()
    _assert_ok(at)
    status = processing_service.wait_for_job(project.id, timeout=TIMEOUT)
    assert status is not None and status.state == "done", status
    reloaded = project_service.open_project(project.id)
    assert all(page.status == PageStatus.CORRECTED for page in reloaded.pages)
    at.run()
    _assert_ok(at)
    assert any("Terminé" in item.value for item in at.markdown)


# --------------------------------------------------------------------------- lancement réel


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def test_real_launch_health(data_dir):
    port = _free_port()
    streamlit_bin = Path(sys.executable).with_name("streamlit")
    server = subprocess.Popen([str(streamlit_bin), "run", APP, "--server.headless", "true", "--server.port", str(port)],
                              cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    try:
        body = None
        for _ in range(60):
            try:
                with urllib.request.urlopen(f"http://localhost:{port}/_stcore/health", timeout=2) as response:
                    body = response.read().decode()
                    break
            except OSError:
                time.sleep(0.5)
        assert body == "ok"
    finally:
        server.terminate()
        server.wait(timeout=30)
