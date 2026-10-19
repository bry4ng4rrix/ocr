"""Gestion des projets (modifier, corbeille, restaurer), types de page, statistiques et prix."""
from __future__ import annotations

import numpy as np
import pytest
from streamlit.testing.v1 import AppTest

from components import common
from models.errors import ProcessingError, ProjectCorruptedError
from models.pricing import PricingSettings
from models.scanning import BookScanStats, PageKind, ScanState
from repositories import scan_repository
from services import pricing_service, project_service, scanning_service
from tests.conftest import ROOT
from tests.fixtures import synthetic


def test_update_project_details(data_dir):
    project = project_service.create_project("Ancien nom", ["fra"])
    updated = project_service.update_project_details(project.id, name="  Nouveau nom ", author="A. Auteur",
                                                     expected_page_count=120, languages=["fra", "deu"])
    reopened = project_service.open_project(project.id)
    assert (reopened.name, reopened.author, reopened.expected_page_count, reopened.languages) == (
        "Nouveau nom", "A. Auteur", 120, ["fra", "deu"]) == (updated.name, updated.author,
                                                               updated.expected_page_count, updated.languages)
    with pytest.raises(ValueError):
        project_service.update_project_details(project.id, name=" ")
    with pytest.raises(ValueError):
        project_service.update_project_details(project.id, name="X", languages=[])


def test_trash_and_restore_keep_everything(data_dir, tmp_path):
    project = project_service.create_project("Livre", ["fra"])
    project_service.import_paths(project, [synthetic.save(synthetic.text_page(), tmp_path)])
    scanning_service.sync_queue(project)
    original = project_service.page_paths(project, project.pages[0]).original
    content = original.read_bytes()
    trash_name = scanning_service.delete_book(project.id)
    assert project_service.list_projects() == [] and not original.exists()
    assert scan_repository.tasks(project.id) == []
    assert [item.trash_name for item in project_service.list_trashed_projects()] == [trash_name]
    restored = scanning_service.restore_book(trash_name)
    assert restored.id == project.id and len(restored.pages) == 1
    assert original.read_bytes() == content                                      # rien n'a été perdu
    assert [task.page_id for task in scan_repository.tasks(project.id)] == [restored.pages[0].id]
    assert project_service.list_trashed_projects() == []


def test_restore_refused_when_project_exists_again(data_dir):
    project = project_service.create_project("Livre", ["fra"])
    trash_name = project_service.trash_project(project.id)
    project_service.project_dir(project.id).mkdir(parents=True)
    with pytest.raises(ProjectCorruptedError):
        project_service.restore_project(trash_name)


def test_delete_refused_while_processing(data_dir, monkeypatch):
    from workers import scan_runner

    project = project_service.create_project("Livre", ["fra"])
    monkeypatch.setattr(scan_runner, "is_running", lambda: True)
    monkeypatch.setattr(scan_runner, "status", lambda: scan_runner.RunnerStatus(running=True, book_ids=[project.id]))
    with pytest.raises(ProcessingError):
        scanning_service.delete_book(project.id)
    assert project_service.list_projects()


def test_page_classification():
    text = synthetic.text_page().image
    assert scanning_service.classify_page(text)[0] == PageKind.TEXT
    assert scanning_service.classify_page(synthetic.photo_text_page().image)[0] == PageKind.IMAGE
    rng = np.random.default_rng(2)
    cover = np.clip(rng.normal((70, 45, 25), 12, (1400, 1000, 3)), 0, 255).astype(np.uint8)   # couverture sombre
    assert scanning_service.classify_page(cover)[0] == PageKind.COVER


def test_statistics_count_completed_pages_by_effective_kind(data_dir, tmp_path):
    pages = [synthetic.text_page(), synthetic.photo_text_page(), synthetic.german_text_page()]
    project = project_service.create_project("Livre", ["fra"])
    project_service.import_paths(project, [synthetic.save(page, tmp_path) for page in pages])
    scanning_service.sync_queue(project)
    first, second, third = project.pages
    for page, kind in ((first, PageKind.TEXT), (second, PageKind.IMAGE), (third, PageKind.TEXT)):
        record = scanning_service.load_record(project, page)
        record.page_kind = kind
        scanning_service.save_record(project, record)
    scan_repository.set_state(project.id, first.id, ScanState.COMPLETED)
    scan_repository.set_state(project.id, second.id, ScanState.COMPLETED)
    scan_repository.set_state(project.id, third.id, ScanState.REVIEW_REQUIRED)       # pas terminée : pas comptée
    scanning_service.set_page_kind(project, first.id, PageKind.COVER)                  # correction manuelle
    stats = scanning_service.book_statistics(project)
    assert (stats.total_pages, stats.completed_pages, stats.review_pages) == (3, 2, 1)
    assert stats.by_kind == {PageKind.TEXT: 0, PageKind.IMAGE: 1, PageKind.COVER: 1}
    scanning_service.set_page_kind(project, first.id, None)                            # retour à l'automatique
    assert scanning_service.book_statistics(project).by_kind[PageKind.TEXT] == 1


def test_prices_are_saved_and_estimated(data_dir):
    assert pricing_service.load_prices() == PricingSettings()
    prices = PricingSettings(currency="Ar", text_page=500, image_page=1000, cover_page=2500)
    pricing_service.save_prices(prices)
    assert pricing_service.load_prices() == prices
    stats = BookScanStats(book_id="b", book_name="Livre", total_pages=10, completed_pages=7,
                          by_kind={PageKind.TEXT: 4, PageKind.IMAGE: 2, PageKind.COVER: 1})
    estimate = pricing_service.estimate(stats, prices)
    assert {line.kind: line.amount for line in estimate.lines} == {PageKind.TEXT: 2000, PageKind.IMAGE: 2000,
                                                                    PageKind.COVER: 2500}
    assert estimate.total == 6500 and estimate.unclassified_pages == 0
    with pytest.raises(ValueError):
        PricingSettings(text_page=-1)


@pytest.mark.ui
def test_pricing_screen_saves_prices_and_shows_totals(data_dir):
    project_service.create_project("Livre", ["fra"])
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    at.switch_page(common.SCREEN_PRICING).run()
    assert not at.exception
    at.number_input[0].set_value(300.0)
    at.number_input[1].set_value(800.0)
    at.number_input[2].set_value(1500.0)
    at.button[0].click().run()
    assert not at.exception
    assert pricing_service.load_prices().image_page == 800
    assert any(metric.label == "Total des prix" for metric in at.metric)


@pytest.mark.ui
def test_dashboard_edits_deletes_and_restores_a_project(data_dir):
    project = project_service.create_project("Livre", ["fra"])
    at = AppTest.from_file(str(ROOT / "app.py"), default_timeout=120).run()
    [field for field in at.text_input if field.label == "Nom du livre"][-1].set_value("Livre modifié")
    [button for button in at.button if button.label == "Enregistrer les modifications"][0].click().run()
    assert project_service.open_project(project.id).name == "Livre modifié"
    at.checkbox(key=f"confirm_delete_{project.id}").check().run()
    [button for button in at.button if button.label == "Mettre à la corbeille"][0].click().run()
    assert project_service.list_projects() == []
    [button for button in at.button if button.label == "Restaurer"][0].click().run()
    assert [summary.name for summary in project_service.list_projects()] == ["Livre modifié"]
