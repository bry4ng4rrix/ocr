"""Rendu HTML pour l'aperçu en direct (page blanche sans fond).

Fonctions pures sans dépendance à Streamlit (testables unitairement) :
- `render_positioned_html` : disposition exacte sur feuille blanche selon les coordonnées des blocs.
- `render_flow_html` : mise en page en flux de lecture structuré sur feuille blanche.
- `draw_overlay_boxes` : surlignage des boîtes détectées sur une copie RGB de l'image.
"""
from __future__ import annotations

import html
from typing import TYPE_CHECKING

import cv2
import numpy as np

from models.blocks import BlockKind, ImageBlock, TextBlock
from models.geometry import STANDARD_FORMATS
from services import typography_service

if TYPE_CHECKING:
    from models.page import Page
    from models.project import Project


def _aspect_ratio(page: Page, fallback: float = 0.707) -> float:
    """Ratio largeur / hauteur de la page."""
    page_format = page.format_override or page.detected_format
    if page_format is not None and page_format.height_pt > 0:
        return max(0.3, min(3.0, page_format.width_pt / page_format.height_pt))
    return fallback


def render_positioned_html(project: Project, page: Page, *, aspect_ratio: float | None = None,
                           image_data_uris: dict[str, str] | None = None) -> str:
    """Génère le code HTML d'une feuille blanche sans fond avec les blocs positionnés à leurs coordonnées exactes."""
    ratio = aspect_ratio if aspect_ratio is not None else _aspect_ratio(page)
    target_format = page.format_override or page.detected_format or STANDARD_FORMATS["A5"]
    target_width_pt = max(50.0, target_format.width_pt)
    uris = image_data_uris or {}

    blocks_html: list[str] = []
    ordered = page.blocks_in_order()

    if not ordered or not any(not block.hidden for block in ordered):
        return (
            f'<div style="width: 100%; max-width: 650px; margin: 0 auto;">'
            f'  <div style="position: relative; width: 100%; aspect-ratio: {ratio:.4f}; background-color: #ffffff; '
            f'border: 1px solid #e2e8f0; border-radius: 6px; box-shadow: 0 4px 14px rgba(0,0,0,0.08); '
            f'display: flex; align-items: center; justify-content: center; color: #94a3b8; font-family: serif; font-size: 14px;">'
            f'    <span>Aucun bloc extrait pour cette page.</span>'
            f'  </div>'
            f'</div>'
        )

    for block in ordered:
        if block.hidden:
            continue
        left = max(0.0, min(100.0, block.box.x * 100.0))
        top = max(0.0, min(100.0, block.box.y * 100.0))
        width = max(0.5, min(100.0 - left, block.box.w * 100.0))
        height = max(0.5, min(100.0 - top, block.box.h * 100.0))

        if isinstance(block, TextBlock):
            style = typography_service.resolve_style(project, page, block)
            font_size_pt = max(4.0, style.font_size_pt)
            cqw_size = (font_size_pt / target_width_pt) * 100.0
            font_size_css = f"clamp(7px, {cqw_size:.2f}cqw, 36px)"
            text_align = style.alignment.value if style.alignment else "left"
            font_weight = "bold" if style.bold else "normal"
            font_style = "italic" if style.italic else "normal"
            text_decoration = "underline" if style.underline else "none"
            line_height = f"{max(1.05, style.line_spacing):.2f}"
            color = style.color or "#0f172a"
            font_family = f'"{style.font_family}", "Liberation Serif", serif' if style.font_family else '"Liberation Serif", serif'
            escaped_text = html.escape(block.text)

            blocks_html.append(
                f'<div style="position: absolute; left: {left:.2f}%; top: {top:.2f}%; width: {width:.2f}%; '
                f'min-height: {height:.2f}%; box-sizing: border-box; background: transparent; '
                f'font-family: {font_family}; font-size: {font_size_css}; font-weight: {font_weight}; '
                f'font-style: {font_style}; text-decoration: {text_decoration}; line-height: {line_height}; '
                f'color: {color}; text-align: {text_align}; overflow-wrap: break-word; word-break: break-word; '
                f'white-space: pre-wrap; padding: 1px 2px;">{escaped_text}</div>'
            )
        elif isinstance(block, ImageBlock):
            if block.id in uris:
                blocks_html.append(
                    f'<div style="position: absolute; left: {left:.2f}%; top: {top:.2f}%; width: {width:.2f}%; '
                    f'height: {height:.2f}%; box-sizing: border-box; display: flex; align-items: center; '
                    f'justify-content: center; background: transparent; overflow: hidden;">'
                    f'<img src="{uris[block.id]}" style="width: 100%; height: 100%; object-fit: contain; '
                    f'border-radius: 2px;" alt="Illustration"/></div>'
                )
            else:
                blocks_html.append(
                    f'<div style="position: absolute; left: {left:.2f}%; top: {top:.2f}%; width: {width:.2f}%; '
                    f'height: {height:.2f}%; box-sizing: border-box; border: 1px dashed #cbd5e1; background: #f8fafc; '
                    f'border-radius: 3px; display: flex; align-items: center; justify-content: center; '
                    f'font-size: 11px; color: #94a3b8;"><span>📷 Illustration</span></div>'
                )

    content = "\n".join(blocks_html)
    return (
        f'<div style="width: 100%; max-width: 650px; margin: 0 auto;">\n'
        f'  <div style="position: relative; width: 100%; aspect-ratio: {ratio:.4f}; background-color: #ffffff; '
        f'border: 1px solid #e2e8f0; border-radius: 6px; box-shadow: 0 4px 14px rgba(0,0,0,0.08), 0 1px 3px rgba(0,0,0,0.05); '
        f'overflow: hidden; container-type: inline-size; box-sizing: border-box;">\n'
        f'{content}\n'
        f'  </div>\n'
        f'</div>'
    )


def render_flow_html(project: Project, page: Page, *, image_data_uris: dict[str, str] | None = None) -> str:
    """Génère le code HTML d'une feuille blanche sans fond avec le texte structuré en flux de lecture."""
    doc_font = project.typography.document_font or "Liberation Serif"
    uris = image_data_uris or {}
    ordered = page.blocks_in_order()

    header_parts: list[str] = []
    footer_parts: list[str] = []
    body_parts: list[str] = []

    for block in ordered:
        if block.hidden:
            continue
        if isinstance(block, TextBlock):
            text = block.text.strip()
            if not text:
                continue
            escaped = html.escape(text).replace("\n", "<br/>")
            if block.kind == BlockKind.HEADER:
                header_parts.append(f'<span>{escaped}</span>')
            elif block.kind in (BlockKind.FOOTER, BlockKind.PAGE_NUMBER):
                footer_parts.append(f'<span>{escaped}</span>')
            elif block.kind == BlockKind.HEADING_1:
                body_parts.append(
                    f'<h1 style="font-size: 22px; font-weight: 700; color: #0f172a; margin: 18px 0 12px 0; '
                    f'line-height: 1.25;">{escaped}</h1>'
                )
            elif block.kind == BlockKind.HEADING_2:
                body_parts.append(
                    f'<h2 style="font-size: 17px; font-weight: 600; color: #1e293b; margin: 16px 0 8px 0; '
                    f'line-height: 1.3;">{escaped}</h2>'
                )
            elif block.kind == BlockKind.CAPTION:
                body_parts.append(
                    f'<p style="font-size: 12px; font-style: italic; color: #64748b; margin: 4px 0 12px 0; '
                    f'text-align: center;">{escaped}</p>'
                )
            elif block.kind == BlockKind.NOTE:
                body_parts.append(
                    f'<div style="font-size: 12px; color: #475569; border-left: 3px solid #cbd5e1; '
                    f'padding-left: 10px; margin: 8px 0;">{escaped}</div>'
                )
            elif block.kind == BlockKind.LIST_ITEM:
                body_parts.append(
                    f'<div style="font-size: 14px; color: #1e293b; margin: 4px 0 4px 18px;">• {escaped}</div>'
                )
            elif block.kind == BlockKind.SPECIAL:
                body_parts.append(
                    f'<div style="font-size: 13px; color: #334155; background: #f8fafc; border-left: 3px solid #94a3b8; '
                    f'padding: 8px 12px; margin: 10px 0; font-style: italic;">{escaped}</div>'
                )
            else:
                body_parts.append(
                    f'<p style="font-size: 14px; color: #1e293b; margin: 0 0 12px 0; text-align: justify; '
                    f'text-indent: 1.5em; line-height: 1.6;">{escaped}</p>'
                )
        elif isinstance(block, ImageBlock):
            if block.id in uris:
                body_parts.append(
                    f'<div style="text-align: center; margin: 16px 0;">'
                    f'<img src="{uris[block.id]}" style="max-width: 85%; max-height: 300px; height: auto; '
                    f'border-radius: 3px; box-shadow: 0 1px 3px rgba(0,0,0,0.1);" alt="Illustration"/></div>'
                )
            else:
                body_parts.append(
                    f'<div style="border: 1px dashed #cbd5e1; background: #f8fafc; border-radius: 4px; '
                    f'padding: 24px; text-align: center; color: #94a3b8; font-size: 12px; margin: 16px 0;">'
                    f'📷 Illustration</div>'
                )

    if not body_parts and not header_parts and not footer_parts:
        body_content = '<p style="color: #94a3b8; text-align: center; font-style: italic;">Aucun texte extrait pour cette page.</p>'
    else:
        body_content = "\n".join(body_parts)

    header_html = ""
    if header_parts:
        header_html = (
            f'<div style="border-bottom: 1px solid #e2e8f0; padding-bottom: 8px; margin-bottom: 20px; '
            f'font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: #64748b; '
            f'display: flex; justify-content: space-between;">'
            + " ".join(header_parts)
            + "</div>"
        )

    footer_html = ""
    if footer_parts:
        footer_html = (
            f'<div style="border-top: 1px solid #e2e8f0; padding-top: 10px; margin-top: 24px; '
            f'font-size: 11px; color: #64748b; text-align: center;">'
            + " ".join(footer_parts)
            + "</div>"
        )

    return (
        f'<div style="width: 100%; max-width: 650px; margin: 0 auto;">\n'
        f'  <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 6px; '
        f'box-shadow: 0 4px 14px rgba(0,0,0,0.08), 0 1px 3px rgba(0,0,0,0.05); padding: 36px 40px; '
        f'font-family: \'{doc_font}\', \'Liberation Serif\', serif; min-height: 520px; box-sizing: border-box;">\n'
        f'{header_html}\n'
        f'{body_content}\n'
        f'{footer_html}\n'
        f'  </div>\n'
        f'</div>'
    )


def draw_overlay_boxes(image: np.ndarray, page: Page) -> np.ndarray:
    """Surligne les blocs détectés sur une copie de l'image (bleu pour texte, vert pour illustration).

    Entrée et sortie : numpy.ndarray RGB uint8 (H, W, 3).
    """
    if image is None or image.ndim != 3:
        return image
    h, w = image.shape[:2]
    canvas = image.copy()
    for block in page.blocks_in_order():
        if block.hidden:
            continue
        bx = int(round(block.box.x * w))
        by = int(round(block.box.y * h))
        bw = max(2, int(round(block.box.w * w)))
        bh = max(2, int(round(block.box.h * h)))
        is_text = isinstance(block, TextBlock)
        color = (59, 130, 246) if is_text else (34, 197, 94)  # RGB
        cv2.rectangle(canvas, (bx, by), (bx + bw, by + bh), color, 2)
    return canvas
