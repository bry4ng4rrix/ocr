from pathlib import Path

import numpy as np
from PIL import Image
from streamlit.testing.v1 import AppTest

from core.document_models import ImageBlock, PageStatus, TextBlock
from services import processing_service
from services.project_service import create_project, import_files
from services.storage_service import ProjectStorage, save_project


ROOT = Path(__file__).resolve().parents[1]


def test_app_home_without_project(workspace):
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15).run()

    assert not app.exception
    assert any("LivreOCR" in heading.value for heading in app.title)
    assert any("Créez un projet" in message.value for message in app.info)


def test_project_can_open_block_and_export_screens(workspace):
    project = create_project("Test interface", ["fra"])
    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15)
    app.session_state["project_id"] = project.id
    app.session_state["project"] = project
    app.session_state["project_select__0"] = project.id
    app.session_state["nav"] = "Éditeur de blocs"
    app.run()

    assert not app.exception
    app.radio(key="nav").set_value("Éditeur de blocs").run()
    assert not app.exception
    assert any("Éditeur de blocs" in heading.value for heading in app.header)

    app.radio(key="nav").set_value("Export PDF").run()
    assert not app.exception
    assert any("Export PDF" in heading.value for heading in app.header)


def test_pages_screen_shows_processed_extraction(workspace):
    project = create_project("Aperçu extraction", ["fra"])
    rgb = np.full((120, 90, 3), 245, dtype=np.uint8)
    image_bytes = _png_bytes(rgb)
    import_files(project, [("page.png", image_bytes)])
    page = project.pages[0]
    page.geometry.auto_orientation = False
    page.geometry.auto_crop = False
    page.geometry.auto_deskew = False
    page.frame_path = processing_service.frame_rel_path(page)
    page.frame_width, page.frame_height = rgb.shape[1], rgb.shape[0]
    page.status = PageStatus.PROCESSED
    page.ocr_geometry_signature = page.geometry.signature()
    page.ocr_languages = ["fra"]
    page.ocr_mean_confidence = 88.0
    page.blocks.append(TextBlock(x=0.1, y=0.2, width=0.7, height=0.2,
                                 text="Texte reconnu dans l’aperçu", confidence=88.0))
    storage = ProjectStorage(project.id)
    storage.write_bytes(page.frame_path, image_bytes)
    save_project(project)

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15)
    app.session_state["project_id"] = project.id
    app.session_state["project"] = project
    app.session_state["project_select__0"] = project.id
    app.session_state["nav"] = "Pages"
    app.run()

    assert not app.exception
    assert any("Aperçu des résultats d’extraction" in heading.value for heading in app.subheader)
    assert any("Texte reconnu dans l’aperçu" in text.value for text in app.text)


def test_pages_screen_does_not_present_a_page_sized_drawing_as_ocr_output(workspace):
    project = create_project("Résultat OCR manquant", ["fra"])
    rgb = np.full((120, 90, 3), 245, dtype=np.uint8)
    image_bytes = _png_bytes(rgb)
    import_files(project, [("page.png", image_bytes)])
    page = project.pages[0]
    page.geometry.auto_orientation = False
    page.geometry.auto_crop = False
    page.geometry.auto_deskew = False
    page.frame_path = processing_service.frame_rel_path(page)
    page.frame_width, page.frame_height = rgb.shape[1], rgb.shape[0]
    page.status = PageStatus.PROCESSED
    page.ocr_geometry_signature = page.geometry.signature()
    page.blocks.append(ImageBlock(x=0, y=0, width=1, height=1, image_path="illustrations/page.png",
                                  detection="drawing"))
    storage = ProjectStorage(project.id)
    storage.write_bytes(page.frame_path, image_bytes)
    storage.write_bytes("illustrations/page.png", image_bytes)
    save_project(project)

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15)
    app.session_state["project_id"] = project.id
    app.session_state["project"] = project
    app.session_state["project_select__0"] = project.id
    app.session_state["nav"] = "Pages"
    app.run()

    assert not app.exception
    assert any("Aucun texte OCR n’a été reconnu" in warning.value for warning in app.warning)


def test_default_ocr_scope_includes_processed_pages_without_text(workspace):
    project = create_project("Relancer OCR", ["fra"])
    rgb = np.full((120, 90, 3), 245, dtype=np.uint8)
    image_bytes = _png_bytes(rgb)
    import_files(project, [("page.png", image_bytes)])
    page = project.pages[0]
    page.status = PageStatus.PROCESSED
    page.blocks.append(ImageBlock(x=0, y=0, width=1, height=1, image_path="illustrations/page.png",
                                  detection="drawing"))
    save_project(project)

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15)
    app.session_state["project_id"] = project.id
    app.session_state["project"] = project
    app.session_state["project_select__0"] = project.id
    app.session_state["nav"] = "Traitement OCR"
    app.run()

    assert not app.exception
    assert any(button.label == "Lancer l'OCR (1 page(s))" for button in app.button)


def test_block_editor_shows_original_and_live_reconstructed_pdf(workspace):
    project = create_project("Aperçu Django porté", ["fra"])
    rgb = np.full((120, 90, 3), 245, dtype=np.uint8)
    image_bytes = _png_bytes(rgb)
    import_files(project, [("page.png", image_bytes)])
    page = project.pages[0]
    page.geometry.auto_orientation = False
    page.geometry.auto_crop = False
    page.geometry.auto_deskew = False
    page.frame_path = processing_service.frame_rel_path(page)
    page.frame_width, page.frame_height = rgb.shape[1], rgb.shape[0]
    page.status = PageStatus.PROCESSED
    page.ocr_geometry_signature = page.geometry.signature()
    page.blocks.append(TextBlock(x=0.1, y=0.2, width=0.7, height=0.2,
                                 text="Texte reconstruit", ocr_text="Texte reconstruit"))
    storage = ProjectStorage(project.id)
    storage.write_bytes(page.frame_path, image_bytes)
    save_project(project)

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15)
    app.session_state["project_id"] = project.id
    app.session_state["project"] = project
    app.session_state["project_select__0"] = project.id
    app.session_state["nav"] = "Éditeur de blocs"
    app.run()

    assert not app.exception
    assert any("Aperçu de la page reconstruite" in heading.value for heading in app.subheader)
    assert any("Image originale" in caption.value for caption in app.caption)
    assert any("Résultat reconstruit — fond blanc" in caption.value for caption in app.caption)
    assert any(button.label == "Relire cette zone" for button in app.button)
    assert any(button.label == "Restaurer le texte OCR" for button in app.button)


def test_completed_ocr_offers_reconstructed_pdf_download(workspace):
    project = create_project("Téléchargement OCR", ["fra"])
    rgb = np.full((120, 90, 3), 245, dtype=np.uint8)
    image_bytes = _png_bytes(rgb)
    import_files(project, [("page.png", image_bytes)])
    page = project.pages[0]
    page.geometry.auto_orientation = False
    page.geometry.auto_crop = False
    page.geometry.auto_deskew = False
    page.frame_path = processing_service.frame_rel_path(page)
    page.frame_width, page.frame_height = rgb.shape[1], rgb.shape[0]
    page.status = PageStatus.PROCESSED
    page.ocr_geometry_signature = page.geometry.signature()
    page.blocks.append(TextBlock(x=0.1, y=0.2, width=0.7, height=0.2,
                                 text="Texte à exporter", ocr_text="Texte à exporter"))
    storage = ProjectStorage(project.id)
    storage.write_bytes(page.frame_path, image_bytes)
    save_project(project)

    app = AppTest.from_file(str(ROOT / "app.py"), default_timeout=15)
    app.session_state["project_id"] = project.id
    app.session_state["project"] = project
    app.session_state["project_select__0"] = project.id
    app.session_state["nav"] = "Traitement OCR"
    app.session_state["ocr_results"] = (project.id, [{
        "page_id": page.id, "ok": True, "message": "", "text_blocks": 1,
        "image_blocks": 0, "seconds": 0.1, "warnings": [],
    }])
    app.run()

    export_button = next(button for button in app.button
                         if button.label == "Créer ou actualiser le PDF reconstruit")
    export_button.click().run()

    assert not app.exception
    assert (storage.root / "exports").exists()
    assert any(button.label == "Télécharger le PDF du livre" for button in app.get("download_button"))


def _png_bytes(rgb: np.ndarray) -> bytes:
    import io

    buffer = io.BytesIO()
    Image.fromarray(rgb).save(buffer, format="PNG")
    return buffer.getvalue()
