"""Dépôt SQLite pour les tâches de traitement par lots et la reprise sur incident."""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from repositories.database import get_connection, init_db, transaction


def _dt_to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def create_job(
    book_id: str,
    total_pages: int,
    *,
    job_id: str | None = None,
    current_step: str = "init",
    db_path: Path | None = None,
) -> str:
    """Crée une tâche de traitement persistante avec statut 'processing'."""
    init_db(db_path)
    jid = job_id or uuid.uuid4().hex[:12]
    now_iso = _dt_to_iso(datetime.now(timezone.utc))
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT INTO processing_jobs (
                id, book_id, state, current_step, done_pages, total_pages, created_at, started_at
            )
            VALUES (?, ?, 'processing', ?, 0, ?, ?, ?)
            """,
            (jid, book_id, current_step, total_pages, now_iso, now_iso),
        )
    return jid


def update_job(
    job_id: str,
    *,
    done_pages: int | None = None,
    current_step: str | None = None,
    state: str | None = None,
    error: str | None = None,
    db_path: Path | None = None,
) -> None:
    """Met à jour l'avancement ou l'état d'une tâche."""
    init_db(db_path)
    updates: list[str] = []
    params: list[Any] = []

    if done_pages is not None:
        updates.append("done_pages = ?")
        params.append(done_pages)
    if current_step is not None:
        updates.append("current_step = ?")
        params.append(current_step)
    if state is not None:
        updates.append("state = ?")
        params.append(state)
        if state in ("completed", "failed", "cancelled", "interrupted"):
            updates.append("finished_at = ?")
            params.append(_dt_to_iso(datetime.now(timezone.utc)))
    if error is not None:
        updates.append("error = ?")
        params.append(error)

    if not updates:
        return

    query = f"UPDATE processing_jobs SET {', '.join(updates)} WHERE id = ?"
    params.append(job_id)

    with transaction(db_path) as conn:
        conn.execute(query, params)


def get_job(job_id: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    """Récupère une tâche par son identifiant."""
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        row = conn.execute("SELECT * FROM processing_jobs WHERE id = ?", (job_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def get_latest_job(book_id: str, *, db_path: Path | None = None) -> dict[str, Any] | None:
    """Récupère la dernière tâche enregistrée pour un livre."""
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM processing_jobs WHERE book_id = ? ORDER BY created_at DESC LIMIT 1",
            (book_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def recover_interrupted_jobs(*, db_path: Path | None = None) -> int:
    """Marque comme 'interrupted' toute tâche restée à l'état 'processing' lors du redémarrage."""
    init_db(db_path)
    now_iso = _dt_to_iso(datetime.now(timezone.utc))
    with transaction(db_path) as conn:
        cursor = conn.execute(
            """
            UPDATE processing_jobs
            SET state = 'interrupted', finished_at = ?
            WHERE state = 'processing'
            """,
            (now_iso,),
        )
        count = cursor.rowcount
    return count
