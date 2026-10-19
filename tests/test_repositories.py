"""Tests unitaires pour la persistance relationnelle SQLite (repositories)."""
from __future__ import annotations

from pathlib import Path

import pytest

from models import (Alignment, BlockKind, Box, ImageBlock, ImageKind, OcrResult, OcrWord, Page,
                    PageCategory, PageFormat, PageStatus, Project, TextBlock, TextStyle)
from models.quality import PageQuality, RouteBand
from repositories import book_repository, database, job_repository, page_repository


@pytest.fixture
def test_db(tmp_path) -> Path:
    db = tmp_path / "test_bookocr.db"
    database.init_db(db)
    return db


def _sample_project() -> Project:
    project = Project(
        id="book_123",
        name="Livre d'essai SQLite",
        author="Victor Hugo",
        expected_page_count=2,
        languages=["fra", "eng"],
    )
    p1 = Page(
        id="p1",
        source_file="originals/photo1.jpg",
        source_sha256="sha111",
        original_name="page_001.jpg",
        status=PageStatus.ANALYSED,
        route_band="A",
        mean_confidence=92.5,
        review_required=False,
    )
    p1.text_blocks.append(TextBlock(
        id="blk1",
        kind=BlockKind.HEADING_1,
        box=Box(x=0.1, y=0.1, w=0.8, h=0.1),
        ocr_text="Titre du Chapitre",
        confidence=95.0,
        order=0,
    ))
    p1.image_blocks.append(ImageBlock(
        id="img1",
        kind=ImageKind.PHOTO,
        box=Box(x=0.2, y=0.3, w=0.6, h=0.4),
        file="images/img1.png",
        order=1,
    ))

    p2 = Page(
        id="p2",
        source_file="originals/photo2.jpg",
        source_sha256="sha222",
        original_name="page_002.jpg",
        status=PageStatus.ERROR,
        route_band="E",
        error="Image corrompue",
        review_required=True,
    )
    project.pages = [p1, p2]
    return project


def test_init_db(test_db):
    conn = database.get_connection(test_db)
    tables = [row["name"] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    conn.close()
    assert "books" in tables
    assert "pages" in tables
    assert "ocr_results" in tables
    assert "page_elements" in tables
    assert "processing_jobs" in tables
    assert "quality_checks" in tables
    assert "exports" in tables


def test_save_and_get_book(test_db):
    project = _sample_project()
    book_repository.save_book(project, db_path=test_db)

    reloaded = book_repository.get_book(project.id, db_path=test_db)
    assert reloaded is not None
    assert reloaded.id == project.id
    assert reloaded.name == project.name
    assert reloaded.author == "Victor Hugo"
    assert reloaded.expected_page_count == 2
    assert reloaded.languages == ["fra", "eng"]
    assert len(reloaded.pages) == 2

    # Vérification des pages et blocs
    page1 = reloaded.pages[0]
    assert page1.id == "p1"
    assert page1.status == PageStatus.ANALYSED
    assert page1.route_band == "A"
    assert page1.mean_confidence == 92.5
    assert len(page1.text_blocks) == 1
    assert page1.text_blocks[0].text == "Titre du Chapitre"
    assert len(page1.image_blocks) == 1
    assert page1.image_blocks[0].file == "images/img1.png"

    page2 = reloaded.pages[1]
    assert page2.id == "p2"
    assert page2.status == PageStatus.ERROR
    assert page2.review_required is True


def test_save_book_fills_missing_route_band(test_db):
    project = _sample_project()
    project.pages[0].route_band = None
    project.pages[1].route_band = None
    book_repository.save_book(project, db_path=test_db)

    reloaded = book_repository.get_book(project.id, db_path=test_db)
    assert reloaded is not None
    assert reloaded.pages[0].route_band == "B"
    assert reloaded.pages[1].route_band == "B"


def test_list_books_statistics(test_db):
    project = _sample_project()
    book_repository.save_book(project, db_path=test_db)

    books = book_repository.list_books(db_path=test_db)
    assert len(books) == 1
    b = books[0]
    assert b["id"] == "book_123"
    assert b["total_pages"] == 2
    assert b["analysed_pages"] == 1
    assert b["error_pages"] == 1
    assert b["review_pages"] == 1


def test_delete_book_cascade(test_db):
    project = _sample_project()
    book_repository.save_book(project, db_path=test_db)

    assert book_repository.delete_book(project.id, db_path=test_db) is True
    assert book_repository.get_book(project.id, db_path=test_db) is None

    conn = database.get_connection(test_db)
    pages_count = conn.execute("SELECT COUNT(*) as c FROM pages WHERE book_id = ?", (project.id,)).fetchone()["c"]
    conn.close()
    assert pages_count == 0


def test_page_repository_pagination_and_filters(test_db):
    project = _sample_project()
    book_repository.save_book(project, db_path=test_db)

    pages, total = page_repository.list_pages(project.id, limit=1, offset=0, db_path=test_db)
    assert total == 2
    assert len(pages) == 1
    assert pages[0]["id"] == "p1"

    # Filtre par statut
    error_pages, err_total = page_repository.list_pages(project.id, status="error", db_path=test_db)
    assert err_total == 1
    assert error_pages[0]["id"] == "p2"


def test_ocr_result_persistence(test_db):
    project = _sample_project()
    book_repository.save_book(project, db_path=test_db)

    ocr = OcrResult(
        engine="tesseract",
        words=[OcrWord(text="Exemple", box=Box(x=0.1, y=0.1, w=0.2, h=0.05), confidence=98.0)],
        width=1000,
        height=1500,
    )
    page_repository.save_ocr_result("p1", ocr, db_path=test_db)

    loaded_ocr = page_repository.get_latest_ocr_result("p1", db_path=test_db)
    assert loaded_ocr is not None
    assert loaded_ocr.engine == "tesseract"
    assert loaded_ocr.text == "Exemple"
    assert len(loaded_ocr.words) == 1
    assert loaded_ocr.words[0].text == "Exemple"


def test_quality_check_persistence(test_db):
    project = _sample_project()
    book_repository.save_book(project, db_path=test_db)

    quality = PageQuality(
        page_id="p1",
        band=RouteBand.RELIABLE,
        reasons=["Excellente netteté"],
        mean_confidence=95.0,
    )
    page_repository.save_quality_check("p1", quality, ssim_score=0.92, db_path=test_db)

    conn = database.get_connection(test_db)
    row = conn.execute("SELECT * FROM quality_checks WHERE page_id = 'p1'").fetchone()
    conn.close()
    assert row is not None
    assert row["band"] == "A"
    assert row["ssim_score"] == 0.92


def test_job_repository_lifecycle(test_db):
    project = _sample_project()
    book_repository.save_book(project, db_path=test_db)

    jid = job_repository.create_job("book_123", total_pages=50, current_step="geometry", db_path=test_db)
    job = job_repository.get_job(jid, db_path=test_db)
    assert job is not None
    assert job["state"] == "processing"
    assert job["total_pages"] == 50
    assert job["done_pages"] == 0

    job_repository.update_job(jid, done_pages=25, current_step="ocr", db_path=test_db)
    updated = job_repository.get_job(jid, db_path=test_db)
    assert updated["done_pages"] == 25
    assert updated["current_step"] == "ocr"

    # Test de reprise sur incident
    recovered = job_repository.recover_interrupted_jobs(db_path=test_db)
    assert recovered == 1
    interrupted = job_repository.get_job(jid, db_path=test_db)
    assert interrupted["state"] == "interrupted"
    assert interrupted["finished_at"] is not None
