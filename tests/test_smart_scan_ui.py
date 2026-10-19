"""Écran « Numérisation intelligente » : parcours complet comme un utilisateur (AppTest, projet sur disque).

Nouveau livre → dossier de photos → « Importer et traiter le livre » → galerie avant/après avec la liste des
modifications → export du PDF du livre validé.
"""
from __future__ import annotations

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

from components import common
from services import project_service
from tests.conftest import ROOT, requires_languages
from tests.fixtures import synthetic
from workers import scan_runner

pytestmark = [pytest.mark.ui, pytest.mark.slow, requires_languages("fra", "osd")]


def test_new_book_from_folder_is_processed_shown_and_exported(data_dir, tmp_path):
    st.cache_data.clear()
    folder = tmp_path / "photos"
    for seed, name in ((2, "p1"), (3, "p2")):
        page = synthetic.on_dark_background(synthetic.text_page(), seed=seed)
        page.name = name
        synthetic.save(page, folder, ".jpg")
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180).run()
    at.switch_page(common.SCREEN_SMART_SCAN).run()
    assert not at.exception
    at.text_input(key="scan_new_name").set_value("Livre UI")
    at.segmented_control(key="scan_source").set_value("Dossier sur ce PC").run()
    at.text_input(key="scan_folder").set_value(str(folder)).run()
    [button for button in at.button if button.label == "Importer et traiter le livre"][0].click().run()
    assert not at.exception and not at.error
    assert scan_runner.wait(600)
    at.run()
    assert not at.exception
    projects = project_service.list_projects()
    assert [project.name for project in projects] == ["Livre UI"] and projects[0].page_count == 2
    cards = [item.value for item in at.markdown if item.value.startswith("**Page ")]
    assert any("p1.jpg" in card for card in cards) and any("p2.jpg" in card for card in cards)
    changes = [item.value for item in at.markdown if item.value.startswith("- ")]
    assert any("Bords détectés" in change for change in changes)
    [button for button in at.button if button.label == "Exporter le PDF du livre"][0].click().run()
    assert not at.exception and not at.error
    assert any("Validation : réussie" in item.value for item in at.success)


def test_crop_event_corners_are_parsed_and_bounded():
    from components.crop_selector import corners_from_event

    corners = corners_from_event({"corners": [[-0.1, 0.05], [0.9, 0.05], [1.2, 0.95], [0.1, 0.95]], "nonce": "a"})
    assert [(p.x, p.y) for p in corners] == [(0.0, 0.05), (0.9, 0.05), (1.0, 0.95), (0.1, 0.95)]
    assert corners_from_event({"corners": [[0, 0], [1, 0]]}) is None
    assert corners_from_event({"corners": "rm -rf"}) is None
    assert corners_from_event({"corners": [["a", 0], [1, 0], [1, 1], [0, 1]]}) is None


def test_retouch_button_opens_the_dialog_on_that_page(data_dir, tmp_path):
    from services import scanning_service

    st.cache_data.clear()
    project = scanning_service.create_book("Livre", ["fra"])
    scanning_service.import_book_files(project, [
        (f"p{index}.png", synthetic.save(make(), tmp_path / str(index)).read_bytes())
        for index, make in ((1, synthetic.text_page), (2, synthetic.german_text_page))])     # deux pages distinctes
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=180)
    at.session_state["project_id"] = project.id
    at.run()
    at.switch_page(common.SCREEN_SMART_SCAN).run()
    second = project.pages[1].id
    at.button(key=f"retouch_{second}").click().run()
    assert not at.exception
    assert at.session_state["_scan_dialog_page"] == second
    assert any(item.value.startswith("**Page 2 / 2**") for item in at.markdown)
    at.segmented_control(key="retouch_tool").set_value("Recadrage").run()
    assert not at.exception
    at.button(key="retouch_close").click().run()
    assert "_scan_dialog_page" not in at.session_state
