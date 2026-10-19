"""Composition : page résolue en points, source unique de l'aperçu, du DOCX et du PDF (D-08).

Ne lit aucun pixel et ne dépend pas de `fonts`. Calcul déterministe : les mêmes entrées donnent la même
`ComposedPage` (aucune horloge, aucun hasard, ordre stable).

- Format de sortie : `project.target_format(page)`. Les boîtes normalisées sont projetées sur la page cible
  (origine en haut à gauche). Si le format diffère du format détecté et que `export.scale_fonts_to_format` est
  actif, les tailles **estimées** (police, espacements, retraits) sont multipliées par le rapport des largeurs ;
  les valeurs fixées par l'utilisateur (types, pages, surcharges) sont gardées telles quelles.
- Marges : `export.margins_override_mm` sinon complément de `page.content_box`. Elles servent au flux (sections
  DOCX) ; les rectangles gardent la position d'origine (aucune uniformisation entre pages).
- Modes (D-06) :
    fidelity : tout le texte et les images en `absolute` ;
    editable : texte et images en `flow` (bande, colonne et espace d'origine fournis) ;
    hybrid   : texte courant en `flow`, texte posé sur une image ou un fond en `absolute`, images en `absolute`.
  En editable et hybrid, en-têtes, pieds et numéros passent en `header` (bande haute) ou `footer` (bande basse).
"""
from __future__ import annotations

from pathlib import Path

from models import (MARGINAL_KINDS, MM_TO_PT, BlockKind, Box, ColumnBand, ComposedPage, ImageBlock,
                    ImageElement, Issue, Margins, Page, PageCategory, PageFormat, PageStatus, Project,
                    ReconstructionMode, Rect, ResolvedStyle, Severity, TextBlock, TextElement)
from services.typography_service import resolve_style_with_sources

FULL_WIDTH = 0.6            # part de la largeur utile à partir de laquelle un élément occupe toute la bande
MIN_RECT_PT = 0.5
SCALED_STYLE_FIELDS = ("font_size_pt", "space_before_pt", "space_after_pt", "first_line_indent_pt",
                       "left_indent_pt", "right_indent_pt")
ESTIMATED_SOURCES = {"default", "estimated"}
TEXT_Z, IMAGE_Z, BACKGROUND_Z = 10, 5, 0
# Métriques moyennes d'une police de labeur (Liberation Serif) en em : la composition ne dépend pas de `fonts`.
NATURAL_LINE_EM = 1.149     # interligne simple (ascendante + descendante + interligne de la police)
CONTENT_EM = 1.107          # ascendante + descendante (boîte de ligne CSS sans interlignage)
ASCENT_EM = 0.891
INK_TOP_EM = 0.70           # haut de l'encre d'une ligne (capitales, ascendantes) au-dessus de la ligne de base
HEIGHT_SLACK_EM = 0.3       # réserve verticale (arrondis du moteur de rendu)
WIDTH_SLACK_EM = 0.6        # marge de largeur : arrondis et crénage ne doivent pas faire passer un mot à la ligne


# --------------------------------------------------------------------------- conversions


def box_to_rect(box: Box, width_pt: float, height_pt: float) -> Rect:
    """Boîte normalisée -> rectangle en points sur une page de `width_pt` × `height_pt`."""
    return Rect(x_pt=round(box.x * width_pt, 3), y_pt=round(box.y * height_pt, 3),
                width_pt=round(max(box.w * width_pt, MIN_RECT_PT), 3),
                height_pt=round(max(box.h * height_pt, MIN_RECT_PT), 3))


def font_scale(project: Project, page: Page, target: PageFormat) -> float:
    """Facteur appliqué aux tailles estimées quand le format de sortie diffère du format détecté."""
    detected = page.format_override or page.detected_format
    if not project.export.scale_fonts_to_format or detected is None:
        return 1.0
    if abs(detected.width_mm - target.width_mm) < 0.05 and abs(detected.height_mm - target.height_mm) < 0.05:
        return 1.0
    return target.width_pt / detected.width_pt


def page_margins(project: Project, page: Page, width_pt: float, height_pt: float) -> Margins:
    override = project.export.margins_override_mm
    if override is not None:
        top, right, bottom, left = (value * MM_TO_PT for value in override)
        return Margins(top_pt=round(top, 3), right_pt=round(right, 3), bottom_pt=round(bottom, 3),
                       left_pt=round(left, 3))
    content = page.content_box or _union([block.box for block in _visible_body(page)])
    if content is None:
        return Margins()
    return Margins(top_pt=round(content.y * height_pt, 3), right_pt=round((1 - content.right) * width_pt, 3),
                   bottom_pt=round((1 - content.bottom) * height_pt, 3), left_pt=round(content.x * width_pt, 3))


def _union(boxes: list[Box]) -> Box | None:
    if not boxes:
        return None
    return Box.from_edges(min(b.x for b in boxes), min(b.y for b in boxes),
                          max(b.right for b in boxes), max(b.bottom for b in boxes))


def _visible_body(page: Page) -> list[TextBlock | ImageBlock]:
    return [block for block in page.blocks_in_order()
            if not block.hidden and not (isinstance(block, TextBlock) and block.kind in MARGINAL_KINDS)]


def scaled_style(project: Project, page: Page, block: TextBlock, scale: float) -> ResolvedStyle:
    """Style résolu ; seules les valeurs estimées (ou par défaut) sont mises à l'échelle du format."""
    style, sources = resolve_style_with_sources(project, page, block)
    if abs(scale - 1.0) < 1e-9:
        return style
    update = {}
    for name in SCALED_STYLE_FIELDS:
        if sources.get(name, "default") in ESTIMATED_SOURCES:
            update[name] = round(getattr(style, name) * scale, 2)
    return style.model_copy(update=update)


def _line_count(block: TextBlock) -> int:
    if block.kind == BlockKind.TABLE:
        return max(1, block.text.count("\n") + 1)
    return max(1, len(block.lines))


def _rect_to_box(rect: Rect, width_pt: float, height_pt: float) -> Box:
    return Box.from_edges(rect.x_pt / width_pt, rect.y_pt / height_pt, rect.right_pt / width_pt,
                          rect.bottom_pt / height_pt)


def text_frame(block: TextBlock, style: ResolvedStyle, width_pt: float, height_pt: float) -> Rect:
    """Cadre typographique d'un bloc : la boîte OCR entoure l'encre, le cadre doit contenir les lignes entières.

    Hauteur = lignes × interligne (multiple de l'interligne simple) ; le haut remonte de la demi-interlignage et
    de l'écart entre l'ascendante et le haut de l'encre ; la largeur reçoit une petite marge du côté libre selon
    l'alignement. Un bloc sans lignes OCR (créé par l'utilisateur) garde sa boîte telle quelle.
    """
    ink = box_to_rect(block.box, width_pt, height_pt)
    if not block.lines:
        return ink
    size = style.font_size_pt
    pitch = style.line_spacing * NATURAL_LINE_EM * size
    half_leading = (pitch - CONTENT_EM * size) / 2
    top = ink.y_pt - (half_leading + (ASCENT_EM - INK_TOP_EM) * size)
    height = max(ink.height_pt, _line_count(block) * pitch + HEIGHT_SLACK_EM * size)
    slack = WIDTH_SLACK_EM * size
    left, right = ink.x_pt, ink.right_pt
    if style.alignment == "center":
        left, right = left - slack / 2, right + slack / 2
    elif style.alignment == "right":
        left -= slack
    else:
        right += slack
    left, right = max(0.0, left), min(width_pt, right)
    top = min(max(0.0, top), max(0.0, height_pt - MIN_RECT_PT))
    height = min(height, height_pt - top)
    return Rect(x_pt=round(left, 3), y_pt=round(top, 3), width_pt=round(max(right - left, MIN_RECT_PT), 3),
                height_pt=round(max(height, MIN_RECT_PT), 3))


# --------------------------------------------------------------------------- bandes et colonnes


def _columns_of(boxes: dict[str, Box], page_columns: int) -> tuple[list[tuple[float, float]], dict[str, int | None]]:
    """Colonnes (étendues normalisées) et colonne de chaque élément (None : pleine largeur)."""
    if not boxes:
        return [], {}
    left = min(box.x for box in boxes.values())
    width = max(max(box.right for box in boxes.values()) - left, 1e-6)
    if page_columns <= 1:
        return [], {key: None for key in boxes}

    def side_by_side(a: Box, b: Box) -> bool:
        vertical = min(a.bottom, b.bottom) - max(a.y, b.y)
        return (a.right <= b.x or b.right <= a.x) and vertical > 0.3 * min(a.h, b.h)

    columnar = [key for key, box in boxes.items() if box.w < FULL_WIDTH * width
                and any(side_by_side(box, other) for other_key, other in boxes.items() if other_key != key)]
    columns: list[list[float]] = []
    for key in sorted(columnar, key=lambda item: (boxes[item].x, item)):
        box = boxes[key]
        if columns and box.x < columns[-1][1]:
            columns[-1][1] = max(columns[-1][1], box.right)
        else:
            columns.append([box.x, box.right])
    assignment: dict[str, int | None] = {}
    for key, box in boxes.items():
        if not columns or box.w >= FULL_WIDTH * width:
            assignment[key] = None
            continue
        hits = [index for index, (col_left, col_right) in enumerate(columns)
                if min(col_right, box.right) - max(col_left, box.x) > 0.15 * min(col_right - col_left, box.w)]
        if len(hits) == 1:
            assignment[key] = hits[0]
        elif not hits:
            centre = box.x + box.w / 2
            assignment[key] = min(range(len(columns)),
                                  key=lambda index: abs(centre - (columns[index][0] + columns[index][1]) / 2))
        else:
            assignment[key] = None
    return [(a, b) for a, b in columns], assignment


def _bands(order: list[str], boxes: dict[str, Box], page_columns: int, width_pt: float, height_pt: float
           ) -> tuple[list[ColumnBand], dict[str, tuple[int, int]]]:
    """Bandes successives (dans l'ordre de lecture) : pleine largeur ou zone en colonnes."""
    columns, assignment = _columns_of(boxes, page_columns)
    groups: list[tuple[bool, list[str]]] = []          # (multicolonne ?, éléments)
    for key in order:
        multi = assignment.get(key) is not None
        if groups and groups[-1][0] == multi:
            groups[-1][1].append(key)
        else:
            groups.append((multi, [key]))
    bands: list[ColumnBand] = []
    placement: dict[str, tuple[int, int]] = {}
    for index, (multi, keys) in enumerate(groups):
        top = min(boxes[key].y for key in keys) * height_pt
        bottom = max(boxes[key].bottom for key in keys) * height_pt
        if multi:
            used = sorted({assignment[key] for key in keys})
            renumber = {column: position for position, column in enumerate(used)}
            rects = []
            for column in used:
                members = [boxes[key] for key in keys if assignment[key] == column]
                left = min(box.x for box in members)
                right = max(box.right for box in members)
                rects.append(Rect(x_pt=round(left * width_pt, 3), y_pt=round(top, 3),
                                  width_pt=round(max((right - left) * width_pt, MIN_RECT_PT), 3),
                                  height_pt=round(max(bottom - top, MIN_RECT_PT), 3)))
            gaps = [rects[i + 1].x_pt - rects[i].right_pt for i in range(len(rects) - 1)]
            gap = round(max(0.0, min(gaps)), 3) if gaps else 0.0
            for key in keys:
                placement[key] = (index, renumber[assignment[key]])
            bands.append(ColumnBand(index=index, top_pt=round(top, 3), bottom_pt=round(bottom, 3),
                                    columns=len(used), gap_pt=gap, column_rects=rects))
        else:
            left = min(boxes[key].x for key in keys)
            right = max(boxes[key].right for key in keys)
            rect = Rect(x_pt=round(left * width_pt, 3), y_pt=round(top, 3),
                        width_pt=round(max((right - left) * width_pt, MIN_RECT_PT), 3),
                        height_pt=round(max(bottom - top, MIN_RECT_PT), 3))
            for key in keys:
                placement[key] = (index, 0)
            bands.append(ColumnBand(index=index, top_pt=round(top, 3), bottom_pt=round(bottom, 3), columns=1,
                                    gap_pt=0.0, column_rects=[rect]))
    return bands, placement


# --------------------------------------------------------------------------- composition


def _positioning(mode: ReconstructionMode, block: TextBlock | ImageBlock) -> str:
    if isinstance(block, TextBlock) and block.kind in MARGINAL_KINDS:
        if mode == ReconstructionMode.FIDELITY:
            return "absolute"
        return "header" if block.box.y + block.box.h / 2 < 0.5 else "footer"
    if mode == ReconstructionMode.FIDELITY:
        return "absolute"
    if mode == ReconstructionMode.EDITABLE:
        return "flow"
    if isinstance(block, ImageBlock):
        return "absolute"
    return "absolute" if block.on_image else "flow"


def _excluded(project: Project, block: TextBlock | ImageBlock) -> bool:
    if block.hidden:
        return True
    if isinstance(block, TextBlock):
        if block.kind in (BlockKind.HEADER, BlockKind.FOOTER) and not project.export.include_headers_footers:
            return True
        if block.kind == BlockKind.PAGE_NUMBER and not project.export.include_page_numbers:
            return True
        if not block.text.strip():
            return True
    return False


def _issue(code: str, message: str, page: Page, block_id: str | None = None,
           severity: Severity = Severity.WARNING) -> Issue:
    return Issue(code=code, message=message, severity=severity, page_id=page.id, block_id=block_id)


def _backgrounds(project: Project, page: Page, project_dir: Path, width_pt: float, height_pt: float,
                 issues: list[Issue]) -> list[ImageElement]:
    if not project.export.keep_backgrounds or not page.background_regions:
        return []
    if page.effective_category == PageCategory.TEXT:
        return []                                   # le cas A n'a jamais de fond
    elements = []
    for index, region in enumerate(page.background_regions):
        if index >= len(page.background_files) or not page.background_files[index]:
            issues.append(_issue("composition_background_missing",
                                 f"Fond graphique n° {index + 1} non extrait : relancez l'analyse de la page.", page))
            continue
        path = (project_dir / page.background_files[index]).resolve()
        if not path.exists():
            issues.append(_issue("composition_background_missing",
                                 f"Fichier du fond graphique introuvable : {page.background_files[index]}.", page))
            continue
        elements.append(ImageElement(kind="background", file=str(path), rect=box_to_rect(region, width_pt, height_pt),
                                     positioning="absolute", z_index=BACKGROUND_Z))
    return elements


def _image_element(block: ImageBlock, project_dir: Path, page: Page, positioning: str, width_pt: float,
                   height_pt: float, issues: list[Issue]) -> ImageElement | None:
    if not block.file:
        issues.append(_issue("composition_image_not_extracted",
                             "Image non extraite : relancez l'analyse de la page pour l'inclure.", page, block.id))
        return None
    path = (project_dir / block.file).resolve()
    if not path.exists():
        issues.append(_issue("composition_image_missing", f"Fichier image introuvable : {block.file}.", page,
                             block.id))
        return None
    return ImageElement(block_id=block.id, kind=block.kind.value, file=str(path),
                        rect=box_to_rect(block.box, width_pt, height_pt), positioning=positioning,
                        caption_block_id=block.caption_block_id, z_index=IMAGE_Z)


def _gaps(elements: list[TextElement | ImageElement], blocks: dict[str, TextBlock | ImageBlock],
          bands: list[ColumnBand], margins: Margins) -> None:
    """Espace vertical d'origine entre un élément et le précédent de sa colonne (ou le haut de sa bande)."""
    previous_bottom: dict[tuple[int, int], float] = {}
    for element in elements:
        if element.band_index is None or element.column_index is None:
            continue
        key = (element.band_index, element.column_index)
        if key in previous_bottom:
            reference = previous_bottom[key]
        elif element.band_index > 0:
            reference = bands[element.band_index - 1].bottom_pt
        else:
            reference = margins.top_pt
        element.gap_before_pt = round(max(0.0, element.rect.y_pt - reference), 3)
        block = blocks.get(element.block_id) if element.block_id else None
        overlay = isinstance(block, TextBlock) and block.on_image and element.positioning == "absolute"
        if not overlay:
            previous_bottom[key] = max(previous_bottom.get(key, 0.0), element.rect.bottom_pt)


def compose_page(project: Project, page: Page, project_dir: Path,
                 mode: ReconstructionMode | None = None) -> ComposedPage:
    """Page résolue en points pour l'aperçu, le DOCX et le PDF."""
    mode = mode or project.export.mode
    project_dir = Path(project_dir)
    target = project.target_format(page)
    width_pt, height_pt = round(target.width_pt, 3), round(target.height_pt, 3)
    issues: list[Issue] = []
    corrected = project_dir / "corrected" / f"{page.id}.png"
    source_image = str(corrected.resolve()) if corrected.exists() else None
    composed = ComposedPage(page_id=page.id, page_number=project.page_number(page.id), width_pt=width_pt,
                            height_pt=height_pt, mode=mode, category=page.effective_category,
                            source_image=source_image)

    if not page.text_blocks and not page.image_blocks and page.status != PageStatus.ANALYSED:
        composed.analysed = False
        composed.issues = [_issue("composition_not_analysed",
                                  "Page non analysée : elle sera exportée vide. Lancez le traitement de la page.",
                                  page)]
        return composed
    if page.ocr_outdated:
        issues.append(_issue("composition_ocr_outdated", (
            "La correction géométrique a changé depuis la lecture du texte : relancez l'OCR de cette page."), page))

    margins = page_margins(project, page, width_pt, height_pt)
    scale = font_scale(project, page, target)
    blocks = [block for block in page.blocks_in_order() if not _excluded(project, block)]
    by_id = {block.id: block for block in blocks}
    elements: list[TextElement | ImageElement] = []
    for block in blocks:
        positioning = _positioning(mode, block)
        if isinstance(block, ImageBlock):
            element = _image_element(block, project_dir, page, positioning, width_pt, height_pt, issues)
            if element is None:
                continue
        else:
            style = scaled_style(project, page, block, scale)
            element = TextElement(block_id=block.id, kind=block.kind, text=block.text,
                                  rect=text_frame(block, style, width_pt, height_pt), style=style,
                                  positioning=positioning, on_image=block.on_image, z_index=TEXT_Z)
        elements.append(element)

    # bandes et colonnes calculées sur les rectangles composés (texte courant et images, hors marges et hors
    # texte superposé en hybride), dans l'ordre de lecture
    in_bands = [element for element in elements
                if not (isinstance(element, TextElement) and element.kind in MARGINAL_KINDS)
                and not (isinstance(element, TextElement) and element.on_image and mode == ReconstructionMode.HYBRID)]
    frames = {element.block_id: _rect_to_box(element.rect, width_pt, height_pt) for element in in_bands}
    bands, placement = _bands([element.block_id for element in in_bands], frames, page.columns, width_pt, height_pt)
    for element in elements:
        element.band_index, element.column_index = placement.get(element.block_id, (None, None))
    _gaps(elements, by_id, bands, margins)

    composed.margins = margins
    composed.backgrounds = _backgrounds(project, page, project_dir, width_pt, height_pt, issues)
    composed.elements = elements
    composed.bands = bands
    if not elements and not composed.backgrounds:
        issues.append(_issue("composition_empty_page", "Page sans contenu exportable (page blanche).", page,
                             severity=Severity.INFO))
    composed.issues = issues
    return composed


def compose_book(project: Project, project_dir: Path, mode: ReconstructionMode | None = None,
                 page_ids: list[str] | None = None) -> list[ComposedPage]:
    """Pages composées dans l'ordre du livre (une page source = une page composée). `page_number` est le rang
    de la page dans le livre (1..), même pour une sélection."""
    if page_ids is None:
        pages = list(project.pages)
    else:
        wanted = set(page_ids)
        for page_id in wanted:
            project.page(page_id)                     # PageNotFoundError si inconnu
        pages = [page for page in project.pages if page.id in wanted]
    return [compose_page(project, page, project_dir, mode) for page in pages]

