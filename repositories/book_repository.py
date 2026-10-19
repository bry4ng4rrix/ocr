"""Dépôt SQLite pour la gestion des livres (création, mise à jour, lecture et indexation)."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import config
from models import BlockKind, Box, ImageBlock, ImageKind, Page, PageCategory, PageFormat, PageStatus, Project, TextBlock, TextStyle
from repositories.database import get_connection, init_db, transaction

logger = logging.getLogger("bookocr.db.books")


def _dt_to_iso(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _iso_to_dt(iso_str: str) -> datetime:
    try:
        return datetime.fromisoformat(iso_str)
    except Exception:
        return datetime.now(timezone.utc)


def save_book(project: Project, *, db_path: Path | None = None) -> None:
    """Enregistre ou met à jour le livre et ses pages dans SQLite."""
    init_db(db_path)
    with transaction(db_path) as conn:
        settings_payload = {
            "typography": project.typography.model_dump(mode="json"),
            "export": project.export.model_dump(mode="json"),
            "ocr_psm": project.ocr_psm,
            "autosave_seconds": project.autosave_seconds,
            "ollama_allowed": project.ollama_allowed,
            "cloud_ai_allowed": project.cloud_ai_allowed,
        }
        conn.execute(
            """
            INSERT INTO books (id, name, author, expected_pages, languages, created_at, updated_at, settings_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                name = excluded.name,
                author = excluded.author,
                expected_pages = excluded.expected_pages,
                languages = excluded.languages,
                updated_at = excluded.updated_at,
                settings_json = excluded.settings_json
            """,
            (
                project.id,
                project.name,
                project.author,
                project.expected_page_count,
                json.dumps(project.languages),
                _dt_to_iso(project.created_at),
                _dt_to_iso(project.updated_at),
                json.dumps(settings_payload),
            ),
        )

        # Synchroniser les pages
        current_page_ids = {page.id for page in project.pages}
        # Supprimer les pages supprimées du projet
        conn.execute(
            f"DELETE FROM pages WHERE book_id = ? AND id NOT IN ({','.join('?' for _ in current_page_ids) if current_page_ids else "''"})",
            [project.id, *current_page_ids] if current_page_ids else [project.id],
        )

        for page in project.pages:
            meta = {
                "source_index": page.source_index,
                "category": page.category.value,
                "category_override": page.category_override.value if page.category_override else None,
                "detected_format": page.detected_format.model_dump(mode="json") if page.detected_format else None,
                "format_override": page.format_override.model_dump(mode="json") if page.format_override else None,
                "body_font_size_pt": page.body_font_size_pt,
                "columns": page.columns,
                "content_box": page.content_box.model_dump(mode="json") if page.content_box else None,
                "printed_page_number": page.printed_page_number,
                "background_regions": [b.model_dump(mode="json") for b in page.background_regions],
                "background_files": page.background_files,
                "style_rules": {k.value: v.model_dump(mode="json") for k, v in page.style_rules.items()},
                "ocr_signature": page.ocr_signature,
                "ocr_languages": page.ocr_languages,
                "geometry": page.geometry.model_dump(mode="json"),
                "enhancement": page.enhancement.model_dump(mode="json"),
                "issues": [issue.model_dump(mode="json") for issue in page.issues],
            }
            conn.execute(
                """
                INSERT INTO pages (
                    id, book_id, page_number, source_file, source_sha256, original_name,
                    status, route_band, mean_confidence, review_required, error, metadata_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    page_number = excluded.page_number,
                    source_file = excluded.source_file,
                    source_sha256 = excluded.source_sha256,
                    original_name = excluded.original_name,
                    status = excluded.status,
                    route_band = excluded.route_band,
                    mean_confidence = excluded.mean_confidence,
                    review_required = excluded.review_required,
                    error = excluded.error,
                    metadata_json = excluded.metadata_json
                """,
                (
                    page.id,
                    project.id,
                    project.page_number(page.id),
                    page.source_file,
                    page.source_sha256,
                    page.original_name,
                    page.status.value,
                    page.route_band,
                    page.mean_confidence,
                    1 if page.review_required else 0,
                    page.error,
                    json.dumps(meta),
                ),
            )

            # Éléments de la page (texte et images)
            conn.execute("DELETE FROM page_elements WHERE page_id = ?", (page.id,))
            for block in page.text_blocks:
                conn.execute(
                    """
                    INSERT INTO page_elements (
                        id, page_id, kind, order_idx, box_x, box_y, box_w, box_h,
                        text, style_json, hidden, needs_review
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        block.id,
                        page.id,
                        block.kind.value,
                        block.order,
                        block.box.x,
                        block.box.y,
                        block.box.w,
                        block.box.h,
                        block.text,
                        json.dumps({
                            "ocr_text": block.ocr_text,
                            "corrected_text": block.corrected_text,
                            "confidence": block.confidence,
                            "estimated_style": block.estimated_style.model_dump(mode="json"),
                            "style_override": block.style_override.model_dump(mode="json"),
                            "lines": [line.model_dump(mode="json") for line in block.lines],
                            "on_image": block.on_image,
                            "user_created": block.user_created,
                            "user_modified": block.user_modified,
                        }),
                        1 if block.hidden else 0,
                        1 if block.needs_review else 0,
                    ),
                )
            for img in page.image_blocks:
                conn.execute(
                    """
                    INSERT INTO page_elements (
                        id, page_id, kind, order_idx, box_x, box_y, box_w, box_h,
                        image_file, hidden, needs_review, style_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        img.id,
                        page.id,
                        img.kind.value,
                        img.order,
                        img.box.x,
                        img.box.y,
                        img.box.w,
                        img.box.h,
                        img.file,
                        1 if img.hidden else 0,
                        1 if img.uncertain else 0,
                        json.dumps({
                            "raw_file": img.raw_file,
                            "caption_block_id": img.caption_block_id,
                            "confidence": img.confidence,
                            "user_created": img.user_created,
                            "user_modified": img.user_modified,
                        }),
                    ),
                )


def get_book(book_id: str, *, db_path: Path | None = None) -> Project | None:
    """Lit un livre complet depuis SQLite."""
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        book_row = conn.execute("SELECT * FROM books WHERE id = ?", (book_id,)).fetchone()
        if not book_row:
            return None
        settings = json.loads(book_row["settings_json"]) if book_row["settings_json"] else {}
        project = Project(
            id=book_row["id"],
            name=book_row["name"],
            author=book_row["author"],
            expected_page_count=book_row["expected_pages"],
            languages=json.loads(book_row["languages"]),
            created_at=_iso_to_dt(book_row["created_at"]),
            updated_at=_iso_to_dt(book_row["updated_at"]),
            typography=settings.get("typography", {}),
            export=settings.get("export", {}),
            ocr_psm=settings.get("ocr_psm", 3),
            autosave_seconds=settings.get("autosave_seconds", 30),
            ollama_allowed=settings.get("ollama_allowed", False),
            cloud_ai_allowed=settings.get("cloud_ai_allowed", False),
        )

        page_rows = conn.execute("SELECT * FROM pages WHERE book_id = ? ORDER BY page_number ASC", (book_id,)).fetchall()
        for p_row in page_rows:
            meta = json.loads(p_row["metadata_json"]) if p_row["metadata_json"] else {}
            page = Page(
                id=p_row["id"],
                source_file=p_row["source_file"],
                source_sha256=p_row["source_sha256"],
                source_index=meta.get("source_index", 0),
                original_name=p_row["original_name"],
                status=PageStatus(p_row["status"]),
                route_band=p_row["route_band"],
                mean_confidence=p_row["mean_confidence"],
                review_required=bool(p_row["review_required"]),
                error=p_row["error"],
                category=PageCategory(meta.get("category", "unknown")),
                category_override=PageCategory(meta["category_override"]) if meta.get("category_override") else None,
                detected_format=PageFormat(**meta["detected_format"]) if meta.get("detected_format") else None,
                format_override=PageFormat(**meta["format_override"]) if meta.get("format_override") else None,
                body_font_size_pt=meta.get("body_font_size_pt"),
                columns=meta.get("columns", 1),
                content_box=Box(**meta["content_box"]) if meta.get("content_box") else None,
                printed_page_number=meta.get("printed_page_number"),
                background_regions=[Box(**b) for b in meta.get("background_regions", [])],
                background_files=meta.get("background_files", []),
                style_rules={BlockKind(k): TextStyle(**v) for k, v in meta.get("style_rules", {}).items()},
                ocr_signature=meta.get("ocr_signature"),
                ocr_languages=meta.get("ocr_languages", []),
                geometry=meta.get("geometry", {}),
                enhancement=meta.get("enhancement", {}),
                issues=meta.get("issues", []),
            )

            # Charger les éléments
            elem_rows = conn.execute(
                "SELECT * FROM page_elements WHERE page_id = ? ORDER BY order_idx ASC",
                (page.id,),
            ).fetchall()
            for e_row in elem_rows:
                kind = e_row["kind"]
                box = Box(x=e_row["box_x"], y=e_row["box_y"], w=e_row["box_w"], h=e_row["box_h"])
                payload = json.loads(e_row["style_json"]) if e_row["style_json"] else {}
                if kind in [k.value for k in BlockKind]:
                    page.text_blocks.append(TextBlock(
                        id=e_row["id"],
                        kind=BlockKind(kind),
                        box=box,
                        order=e_row["order_idx"],
                        ocr_text=payload.get("ocr_text", e_row["text"] or ""),
                        corrected_text=payload.get("corrected_text"),
                        lines=payload.get("lines", []),
                        confidence=payload.get("confidence", -1.0),
                        estimated_style=payload.get("estimated_style", {}),
                        style_override=payload.get("style_override", {}),
                        on_image=payload.get("on_image", False),
                        user_created=payload.get("user_created", False),
                        user_modified=payload.get("user_modified", False),
                        hidden=bool(e_row["hidden"]),
                        needs_review=bool(e_row["needs_review"]),
                    ))
                elif kind in [k.value for k in ImageKind]:
                    page.image_blocks.append(ImageBlock(
                        id=e_row["id"],
                        kind=ImageKind(kind),
                        box=box,
                        order=e_row["order_idx"],
                        file=e_row["image_file"] or "",
                        raw_file=payload.get("raw_file", ""),
                        caption_block_id=payload.get("caption_block_id"),
                        confidence=payload.get("confidence", 1.0),
                        uncertain=bool(e_row["needs_review"]),
                        user_created=payload.get("user_created", False),
                        user_modified=payload.get("user_modified", False),
                        hidden=bool(e_row["hidden"]),
                    ))
            project.pages.append(page)

        return project
    finally:
        conn.close()


def list_books(*, db_path: Path | None = None) -> list[dict[str, Any]]:
    """Liste tous les livres avec statistiques agrégées (pages, pages en erreur, relecture)."""
    init_db(db_path)
    conn = get_connection(db_path)
    try:
        query = """
        SELECT
            b.id, b.name, b.author, b.expected_pages, b.languages, b.created_at, b.updated_at,
            COUNT(p.id) as total_pages,
            SUM(CASE WHEN p.status = 'analysed' THEN 1 ELSE 0 END) as analysed_pages,
            SUM(CASE WHEN p.status = 'error' THEN 1 ELSE 0 END) as error_pages,
            SUM(CASE WHEN p.review_required = 1 THEN 1 ELSE 0 END) as review_pages,
            AVG(p.mean_confidence) as avg_confidence
        FROM books b
        LEFT JOIN pages p ON b.id = p.book_id
        GROUP BY b.id
        ORDER BY b.updated_at DESC
        """
        rows = conn.execute(query).fetchall()
        result = []
        for row in rows:
            result.append({
                "id": row["id"],
                "name": row["name"],
                "author": row["author"],
                "expected_pages": row["expected_pages"],
                "languages": json.loads(row["languages"]) if row["languages"] else [],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "total_pages": row["total_pages"],
                "analysed_pages": row["analysed_pages"],
                "error_pages": row["error_pages"],
                "review_pages": row["review_pages"],
                "avg_confidence": round(row["avg_confidence"], 1) if row["avg_confidence"] is not None else None,
            })
        return result
    finally:
        conn.close()


def delete_book(book_id: str, *, db_path: Path | None = None) -> bool:
    """Supprime un livre et ses enregistrements en cascade de la base."""
    init_db(db_path)
    with transaction(db_path) as conn:
        cursor = conn.execute("DELETE FROM books WHERE id = ?", (book_id,))
        return cursor.rowcount > 0
