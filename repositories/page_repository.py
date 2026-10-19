"""Dépôt SQLite pour les pages, résultats OCR et contrôles qualité."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from models import OcrResult, OcrWord
from models.quality import PageQuality, RouteBand
from repositories.database import get_connection, init_db, transaction


def _dt_to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def list_pages(
    book_id: str,
    *,
    limit: int = 50,
    offset: int = 0,
    status: str | None = None,
    route_band: str | None = None,
    db_path: Path | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Liste les pages d'un livre avec pagination et filtres facultatifs.

    Renvoie (liste_des_pages, nombre_total_correspondant).
    """
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        where_clauses = ["book_id = ?"]
        params: list[Any] = [book_id]
        if status is not None:
            where_clauses.append("status = ?")
            params.append(status)
        if route_band is not None:
            where_clauses.append("route_band = ?")
            params.append(route_band)

        where_sql = " AND ".join(where_clauses)
        count_query = f"SELECT COUNT(*) as total FROM pages WHERE {where_sql}"
        total = conn.execute(count_query, params).fetchone()["total"]

        query = f"""
        SELECT id, book_id, page_number, source_file, source_sha256, original_name,
               status, route_band, mean_confidence, review_required, error
        FROM pages
        WHERE {where_sql}
        ORDER BY page_number ASC
        LIMIT ? OFFSET ?
        """
        rows = conn.execute(query, [*params, limit, offset]).fetchall()
        pages = [dict(row) for row in rows]
        return pages, total
    finally:
        conn.close()


def save_ocr_result(
    page_id: str,
    result: OcrResult,
    *,
    ocr_id: str | None = None,
    db_path: Path | None = None,
) -> str:
    """Enregistre un résultat OCR dans la base SQLite."""
    init_db(db_path)
    oid = ocr_id or f"ocr_{page_id}_{int(datetime.now(timezone.utc).timestamp())}"
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT INTO ocr_results (id, page_id, engine, full_text, mean_confidence, words_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                full_text = excluded.full_text,
                mean_confidence = excluded.mean_confidence,
                words_json = excluded.words_json
            """,
            (
                oid,
                page_id,
                result.engine,
                result.text,
                result.mean_confidence,
                json.dumps([w.model_dump(mode="json") for w in result.words]),
                _dt_to_iso(datetime.now(timezone.utc)),
            ),
        )
    return oid


def get_latest_ocr_result(page_id: str, *, db_path: Path | None = None) -> OcrResult | None:
    """Récupère le résultat OCR le plus récent pour une page."""
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT * FROM ocr_results WHERE page_id = ? ORDER BY created_at DESC LIMIT 1",
            (page_id,),
        ).fetchone()
        if not row:
            return None
        words_data = json.loads(row["words_json"]) if row["words_json"] else []
        words = [OcrWord(**w) for w in words_data]
        return OcrResult(
            engine=row["engine"],
            words=words,
            full_text=row["full_text"],
            mean_confidence=row["mean_confidence"],
            width=0,
            height=0,
        )
    finally:
        conn.close()


def save_quality_check(
    page_id: str,
    quality: PageQuality,
    *,
    ssim_score: float | None = None,
    db_path: Path | None = None,
) -> None:
    """Enregistre une vérification qualité dans SQLite."""
    init_db(db_path)
    check_id = f"chk_{page_id}_{int(datetime.now(timezone.utc).timestamp())}"
    with transaction(db_path) as conn:
        conn.execute(
            """
            INSERT INTO quality_checks (
                id, page_id, band, mean_confidence, ssim_score, reasons_json, issues_json, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                check_id,
                page_id,
                quality.band.value,
                quality.mean_confidence,
                ssim_score,
                json.dumps(quality.reasons),
                json.dumps([i.model_dump(mode="json") for i in quality.issues]),
                _dt_to_iso(datetime.now(timezone.utc)),
            ),
        )
