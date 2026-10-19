"""Initialisation et gestion des connexions SQLite (WAL mode, clés étrangères, transactions)."""
from __future__ import annotations

import contextlib
import logging
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import config

logger = logging.getLogger("bookocr.db")

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS books (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    author TEXT,
    expected_pages INTEGER,
    languages TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    settings_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pages (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    page_number INTEGER NOT NULL,
    source_file TEXT NOT NULL,
    source_sha256 TEXT NOT NULL,
    original_name TEXT NOT NULL,
    status TEXT NOT NULL,
    route_band TEXT NOT NULL DEFAULT 'B',
    mean_confidence REAL,
    review_required INTEGER NOT NULL DEFAULT 0,
    error TEXT,
    metadata_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_pages_book_id ON pages(book_id);
CREATE INDEX IF NOT EXISTS idx_pages_status ON pages(status);
CREATE INDEX IF NOT EXISTS idx_pages_route_band ON pages(route_band);

CREATE TABLE IF NOT EXISTS ocr_results (
    id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    engine TEXT NOT NULL,
    full_text TEXT NOT NULL,
    mean_confidence REAL,
    words_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ocr_page_id ON ocr_results(page_id);

CREATE TABLE IF NOT EXISTS page_elements (
    id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    order_idx INTEGER NOT NULL,
    box_x REAL NOT NULL,
    box_y REAL NOT NULL,
    box_w REAL NOT NULL,
    box_h REAL NOT NULL,
    text TEXT,
    style_json TEXT,
    image_file TEXT,
    hidden INTEGER NOT NULL DEFAULT 0,
    needs_review INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_elements_page_id ON page_elements(page_id);

CREATE TABLE IF NOT EXISTS processing_jobs (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    state TEXT NOT NULL,
    current_step TEXT,
    done_pages INTEGER NOT NULL DEFAULT 0,
    total_pages INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_book_id ON processing_jobs(book_id);

CREATE TABLE IF NOT EXISTS quality_checks (
    id TEXT PRIMARY KEY,
    page_id TEXT NOT NULL REFERENCES pages(id) ON DELETE CASCADE,
    band TEXT NOT NULL,
    mean_confidence REAL,
    ssim_score REAL,
    reasons_json TEXT NOT NULL,
    issues_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quality_page_id ON quality_checks(page_id);

CREATE TABLE IF NOT EXISTS exports (
    id TEXT PRIMARY KEY,
    book_id TEXT NOT NULL REFERENCES books(id) ON DELETE CASCADE,
    format TEXT NOT NULL,
    mode TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_size INTEGER NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_exports_book_id ON exports(book_id);
"""


def get_connection(db_path: Path | None = None) -> sqlite3.Connection:
    """Ouvre une connexion SQLite avec WAL mode, clés étrangères et row_factory dict-like."""
    target = db_path if db_path is not None else config.db_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=30.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    return conn


def init_db(db_path: Path | None = None) -> None:
    """Initialise le schéma de la base de données s'il n'existe pas encore."""
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA_SQL)
        conn.commit()
    finally:
        conn.close()


@contextlib.contextmanager
def transaction(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    """Gestionnaire de contexte pour exécuter une transaction atomique."""
    conn = get_connection(db_path)
    try:
        init_db(db_path)
        with conn:
            yield conn
    finally:
        conn.close()
