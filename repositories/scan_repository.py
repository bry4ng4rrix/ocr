"""File persistante de la numérisation fidèle : un état par page (PENDING … FAILED) dans SQLite.

Les détails (réglages, mesures, chemins) sont dans `scan/<page_id>/record.json` ; cette table ne garde que
ce qu'il faut pour reprendre après une interruption, relancer les erreurs et suivre la progression globale
(100 livres, ~8 400 pages) sans ouvrir chaque projet.
"""
from __future__ import annotations

from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from pydantic import BaseModel

from models.scanning import ScanState
from repositories.database import get_connection, init_db, transaction

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS scan_tasks (
    book_id TEXT NOT NULL,
    page_id TEXT NOT NULL,
    position INTEGER NOT NULL,
    state TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    step TEXT,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (book_id, page_id)
);
CREATE INDEX IF NOT EXISTS idx_scan_tasks_state ON scan_tasks(book_id, state);
"""


class ScanTask(BaseModel):
    book_id: str
    page_id: str
    position: int
    state: ScanState
    attempts: int = 0
    step: str | None = None
    last_error: str | None = None
    updated_at: str


class ScanProgress(BaseModel):
    total: int = 0
    counts: dict[ScanState, int] = {}

    @property
    def done(self) -> int:
        return sum(self.counts.get(state, 0) for state in (ScanState.COMPLETED, ScanState.REVIEW_REQUIRED,
                                                           ScanState.FAILED))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_schema(db_path: Path | None = None) -> None:
    init_db(db_path)
    with transaction(db_path) as conn:
        conn.executescript(SCHEMA_SQL)


def sync_book(book_id: str, page_ids: list[str], *, db_path: Path | None = None) -> None:
    """Aligne la file sur l'ordre des pages : nouvelles pages en PENDING, pages supprimées retirées."""
    ensure_schema(db_path)
    now = _now()
    with transaction(db_path) as conn:
        known = {row["page_id"] for row in conn.execute("SELECT page_id FROM scan_tasks WHERE book_id = ?",
                                                         (book_id,))}
        for position, page_id in enumerate(page_ids):
            if page_id in known:
                conn.execute("UPDATE scan_tasks SET position = ? WHERE book_id = ? AND page_id = ?",
                             (position, book_id, page_id))
            else:
                conn.execute("INSERT INTO scan_tasks (book_id, page_id, position, state, updated_at) "
                             "VALUES (?, ?, ?, ?, ?)", (book_id, page_id, position, ScanState.PENDING.value, now))
        removed = known - set(page_ids)
        conn.executemany("DELETE FROM scan_tasks WHERE book_id = ? AND page_id = ?",
                         [(book_id, page_id) for page_id in removed])


def set_state(book_id: str, page_id: str, state: ScanState, *, step: str | None = None,
              error: str | None = None, count_attempt: bool = False, db_path: Path | None = None) -> None:
    ensure_schema(db_path)
    with transaction(db_path) as conn:
        conn.execute(
            "UPDATE scan_tasks SET state = ?, step = ?, last_error = ?, updated_at = ?, "
            "attempts = attempts + ? WHERE book_id = ? AND page_id = ?",
            (state.value, step, error, _now(), 1 if count_attempt else 0, book_id, page_id))


def mark_pending(book_id: str, page_ids: Iterable[str], *, reset_attempts: bool = False,
                 db_path: Path | None = None) -> int:
    """Remet des pages dans la file (relance sélective, page modifiée)."""
    ensure_schema(db_path)
    ids = list(page_ids)
    with transaction(db_path) as conn:
        extra = ", attempts = 0" if reset_attempts else ""
        cursor = conn.executemany(
            f"UPDATE scan_tasks SET state = ?, last_error = NULL, step = NULL, updated_at = ?{extra} "
            "WHERE book_id = ? AND page_id = ?",
            [(ScanState.PENDING.value, _now(), book_id, page_id) for page_id in ids])
        return cursor.rowcount


def recover_interrupted(book_id: str | None = None, *, db_path: Path | None = None) -> int:
    """Pages restées PROCESSING (arrêt brutal) : remises en PENDING pour reprise."""
    ensure_schema(db_path)
    query = "UPDATE scan_tasks SET state = ?, step = 'interrompu', updated_at = ? WHERE state = ?"
    params: list[str] = [ScanState.PENDING.value, _now(), ScanState.PROCESSING.value]
    if book_id is not None:
        query += " AND book_id = ?"
        params.append(book_id)
    with transaction(db_path) as conn:
        return conn.execute(query, params).rowcount


def tasks(book_id: str, states: Iterable[ScanState] | None = None, *,
          db_path: Path | None = None) -> list[ScanTask]:
    ensure_schema(db_path)
    query = "SELECT * FROM scan_tasks WHERE book_id = ?"
    params: list[str] = [book_id]
    wanted = [state.value for state in states] if states is not None else None
    if wanted is not None:
        if not wanted:
            return []
        query += f" AND state IN ({','.join('?' * len(wanted))})"
        params += wanted
    conn = get_connection(db_path)
    try:
        rows = conn.execute(query + " ORDER BY position", params).fetchall()
    finally:
        conn.close()
    return [ScanTask.model_validate(dict(row)) for row in rows]


def get_task(book_id: str, page_id: str, *, db_path: Path | None = None) -> ScanTask | None:
    ensure_schema(db_path)
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM scan_tasks WHERE book_id = ? AND page_id = ?",
                           (book_id, page_id)).fetchone()
    finally:
        conn.close()
    return ScanTask.model_validate(dict(row)) if row else None


def progress(book_id: str | None = None, *, db_path: Path | None = None) -> ScanProgress:
    """Progression d'un livre, ou de toute la bibliothèque si book_id est None."""
    ensure_schema(db_path)
    query = "SELECT state, COUNT(*) AS n FROM scan_tasks"
    params: list[str] = []
    if book_id is not None:
        query += " WHERE book_id = ?"
        params.append(book_id)
    conn = get_connection(db_path)
    try:
        rows = conn.execute(query + " GROUP BY state", params).fetchall()
    finally:
        conn.close()
    counts = {ScanState(row["state"]): int(row["n"]) for row in rows}
    return ScanProgress(total=sum(counts.values()), counts=counts)


def delete_book(book_id: str, *, db_path: Path | None = None) -> int:
    """Retire un livre de la file (livre mis à la corbeille) ; ses fichiers ne sont pas touchés."""
    ensure_schema(db_path)
    with transaction(db_path) as conn:
        return conn.execute("DELETE FROM scan_tasks WHERE book_id = ?", (book_id,)).rowcount
