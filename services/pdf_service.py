"""PDF : rendu direct PyMuPDF de la page composée, fac-similé recherchable, conversion DOCX -> PDF, validation.

Le PDF est fidèle à la reconstruction : chaque élément (même « flow », « header » ou « footer ») est dessiné
à son `rect`. Le texte est du vrai texte (`insert_htmlbox`) dans une police TrueType intégrée et sous-ensemblée.

Débordement : la hauteur nécessaire est mesurée avec `pymupdf.Story` avant l'écriture. Politique "shrink" :
la taille de police est réduite (pas de 3 %) jusqu'à 70 % ; au-delà, le texte est écrit à 70 % dans une boîte
prolongée vers le bas. Politique "report" : taille inchangée, boîte prolongée. Dans tous les cas le texte est
écrit en entier (jamais tronqué en silence) et le bloc figure dans `ExportReport.overflows`.
"""
from __future__ import annotations

import difflib
import html
import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

import numpy as np
import pymupdf
from PIL import Image

import config
from models.blocks import Alignment
from models.composition import ComposedPage, ImageElement, Rect, ResolvedStyle, TextElement
from models.errors import ExportError
from models.reports import ConversionResult, ExportReport, Issue, PageValidation, Severity, ValidationReport
from services import fonts

logger = logging.getLogger("bookocr.pdf")

OverflowPolicy = Literal["shrink", "report"]

MIN_FONT_SCALE = 0.7            # réduction maximale de la police (politique "shrink")
SHRINK_STEP = 0.03
FIT_TOLERANCE_PT = 0.5
MEASURE_HEIGHT_PT = 20_000.0
FACSIMILE_MIN_FONT_PT = 1.0
BLANK_DPI = 40
BLANK_INK_RATIO = 0.002         # même seuil que le script check_pdf.py
BLANK_GRAY_LEVEL = 235
TEXT_SIMILARITY_WARNING = 0.85
SIZE_TOLERANCE_PT = 0.5

CSS_ALIGN = {Alignment.LEFT: "left", Alignment.CENTER: "center", Alignment.RIGHT: "right",
             Alignment.JUSTIFY: "justify"}
TEXTBOX_ALIGN = {Alignment.LEFT: pymupdf.TEXT_ALIGN_LEFT, Alignment.CENTER: pymupdf.TEXT_ALIGN_CENTER,
                 Alignment.RIGHT: pymupdf.TEXT_ALIGN_RIGHT, Alignment.JUSTIFY: pymupdf.TEXT_ALIGN_JUSTIFY}


# --------------------------------------------------------------------------- fonctions pures (CSS, HTML)


def paragraph_html(text: str) -> str:
    """Un bloc = un paragraphe HTML ; les retours à la ligne internes deviennent <br>."""
    lines = [html.escape(line, quote=False) for line in text.split("\n")]
    return "<p>" + "<br/>".join(lines) + "</p>"


def css_line_height(style: ResolvedStyle, natural_ratio: float) -> float:
    """`line_spacing` est un multiple de l'interligne simple (convention Word) ; le CSS attend un multiple
    de la taille de police : on multiplie par l'interligne naturel de la police."""
    return round(style.line_spacing * natural_ratio, 4)


def style_css(style: ResolvedStyle, css_family: str, natural_ratio: float, font_scale: float = 1.0,
              include_spacing: bool = False) -> str:
    """Règles CSS d'un bloc. La variante (gras, italique) est portée par le fichier de police lui-même
    (@font-face dédié), d'où `font-weight: normal` : pas de graissage synthétique.

    `include_spacing=False` pour un rendu dans un rect : l'espacement entre blocs est déjà dans la position
    des rects calculée par la composition ; le réappliquer dans la boîte le compterait deux fois.
    """
    rules = [
        f'font-family: "{css_family}"',
        f"font-size: {style.font_size_pt * font_scale:.2f}pt",
        "font-weight: normal",
        "font-style: normal",
        f"color: {style.color}",
        f"text-align: {CSS_ALIGN[style.alignment]}",
        f"line-height: {css_line_height(style, natural_ratio)}",
        f"text-indent: {style.first_line_indent_pt * font_scale:.2f}pt",
        f"margin-left: {style.left_indent_pt:.2f}pt",
        f"margin-right: {style.right_indent_pt:.2f}pt",
        f"margin-top: {style.space_before_pt if include_spacing else 0:.2f}pt",
        f"margin-bottom: {style.space_after_pt if include_spacing else 0:.2f}pt",
        "padding: 0",
    ]
    if style.underline:
        rules.append("text-decoration: underline")
    return "body { margin: 0; padding: 0; } p { " + "; ".join(rules) + "; }"


def font_scales(minimum: float = MIN_FONT_SCALE, step: float = SHRINK_STEP) -> list[float]:
    """Facteurs de réduction essayés, du plus grand au plus petit (1 exclu, `minimum` inclus)."""
    scales = []
    value = 1.0 - step
    while value > minimum + 1e-9:
        scales.append(round(value, 4))
        value -= step
    scales.append(minimum)
    return scales


def to_pymupdf_rect(rect: Rect) -> pymupdf.Rect:
    return pymupdf.Rect(rect.x_pt, rect.y_pt, rect.right_pt, rect.bottom_pt)


def normalized_text(text: str) -> str:
    return " ".join(text.split())


def text_similarity(expected: str, actual: str) -> float:
    expected_norm, actual_norm = normalized_text(expected), normalized_text(actual)
    if not expected_norm and not actual_norm:
        return 1.0
    return difflib.SequenceMatcher(None, expected_norm, actual_norm, autojunk=False).ratio()


def expected_page_text(page: ComposedPage) -> str:
    return "\n".join(element.text for element in page.text_elements if element.text.strip())


def expected_image_files(page: ComposedPage, searchable_facsimile: bool = False) -> set[str]:
    if not page.analysed:
        return set()
    if searchable_facsimile:
        return {page.source_image} if page.source_image else set()
    return {image.file for image in [*page.backgrounds, *page.image_elements]}


# --------------------------------------------------------------------------- polices dans l'archive PyMuPDF


@dataclass
class _FontFace:
    css_family: str
    face_css: str
    choice: fonts.FontChoice
    natural_ratio: float
    pymupdf_name: str


class _FontRegistry:
    """Fichiers de police servis à `insert_htmlbox` par une archive en mémoire (un nom unique par fichier)."""

    def __init__(self) -> None:
        self.archive = pymupdf.Archive()
        self._faces: dict[tuple[str, bool, bool], _FontFace] = {}
        self._by_path: dict[Path, tuple[str, str]] = {}
        self._reported: set[tuple[str, bool, bool]] = set()

    def face(self, style: ResolvedStyle) -> _FontFace:
        key = (style.font_family, style.bold, style.italic)
        if key not in self._faces:
            choice = fonts.choose_font(*key)
            if choice.path not in self._by_path:
                index = len(self._by_path)
                file_name = f"bookfont{index}{choice.path.suffix.lower()}"
                self.archive.add((choice.path.read_bytes(), file_name))
                self._by_path[choice.path] = (f"bookfont{index}", file_name)
            css_family, file_name = self._by_path[choice.path]
            self._faces[key] = _FontFace(
                css_family=css_family,
                face_css=f'@font-face {{ font-family: "{css_family}"; src: url({file_name}); }}',
                choice=choice,
                natural_ratio=fonts.natural_line_height(choice.path),
                pymupdf_name=f"F{css_family}",
            )
        return self._faces[key]

    def fallback_issues(self, style: ResolvedStyle, face: _FontFace, page_id: str,
                        block_id: str | None) -> list[Issue]:
        """Replis de police à signaler (une fois par famille et variante dans le document)."""
        key = (style.font_family, style.bold, style.italic)
        if key in self._reported:
            return []
        self._reported.add(key)
        issues = []
        if face.choice.family_fallback:
            issues.append(Issue(code="font_fallback", severity=Severity.WARNING, page_id=page_id,
                                block_id=block_id,
                                message=f"Police « {style.font_family} » absente : « {face.choice.family} » "
                                        "est utilisée dans le PDF."))
        if face.choice.variant_fallback:
            variant = "gras italique" if style.bold and style.italic else "gras" if style.bold else "italique"
            issues.append(Issue(code="font_variant_fallback", severity=Severity.WARNING, page_id=page_id,
                                block_id=block_id,
                                message=f"Variante {variant} absente pour « {face.choice.family} » : "
                                        f"{face.choice.path.name} est utilisé à la place."))
        return issues


# --------------------------------------------------------------------------- rendu d'une page


@dataclass
class _PageOutcome:
    issues: list[Issue] = field(default_factory=list)
    fallbacks: list[Issue] = field(default_factory=list)
    overflows: list[Issue] = field(default_factory=list)
    text_blocks: int = 0
    images: int = 0


def _measure_height(html_text: str, css: str, archive: pymupdf.Archive, width: float) -> float:
    """Hauteur occupée par le HTML composé sur la largeur donnée (points)."""
    story = pymupdf.Story(html=html_text, user_css=css, archive=archive)
    more, filled = story.place(pymupdf.Rect(0, 0, width, MEASURE_HEIGHT_PT))
    return float("inf") if more else float(filled[3])


def _overflow_issue(page: ComposedPage, element: TextElement, severity: Severity, message: str) -> Issue:
    return Issue(code="text_overflow", severity=severity, page_id=page.page_id, block_id=element.block_id,
                 message=f"Page {page.page_number}, bloc {element.block_id} : {message}")


def _draw_text(pdf_page: pymupdf.Page, page: ComposedPage, element: TextElement, registry: _FontRegistry,
               policy: OverflowPolicy, outcome: _PageOutcome) -> None:
    face = registry.face(element.style)
    outcome.fallbacks.extend(registry.fallback_issues(element.style, face, page.page_id, element.block_id))
    missing = fonts.missing_glyphs(face.choice.family, element.text)
    if missing:
        outcome.fallbacks.append(Issue(
            code="missing_glyphs", severity=Severity.WARNING, page_id=page.page_id, block_id=element.block_id,
            message=f"Caractères absents de « {face.choice.family} » (police de secours de MuPDF) : "
                    f"{' '.join(sorted(missing))}"))

    html_text = paragraph_html(element.text)
    rect = to_pymupdf_rect(element.rect)
    available = rect.height

    def css_at(scale: float) -> str:
        return face.face_css + style_css(element.style, face.css_family, face.natural_ratio, scale)

    scale = 1.0
    needed = _measure_height(html_text, css_at(scale), registry.archive, rect.width)
    target = rect
    if needed > available + FIT_TOLERANCE_PT:
        if policy == "shrink":
            for candidate in font_scales():
                scale = candidate
                needed = _measure_height(html_text, css_at(scale), registry.archive, rect.width)
                if needed <= available + FIT_TOLERANCE_PT:
                    break
        if needed <= available + FIT_TOLERANCE_PT:
            outcome.overflows.append(_overflow_issue(
                page, element, Severity.INFO,
                f"texte réduit à {scale:.0%} pour tenir dans sa boîte ({available:.1f} pt)."))
        else:
            target = pymupdf.Rect(rect.x0, rect.y0, rect.x1, rect.y0 + needed + FIT_TOLERANCE_PT)
            outcome.overflows.append(_overflow_issue(
                page, element, Severity.WARNING,
                f"le texte dépasse sa boîte : {needed:.1f} pt nécessaires pour {available:.1f} pt "
                f"(police à {scale:.0%}) ; boîte prolongée vers le bas, chevauchement possible."))

    page_bottom = pdf_page.rect.height
    if target.y1 > page_bottom:
        # ni coupé ni perdu : réduit au-delà de la limite pour rester sur la page, et signalé
        target = pymupdf.Rect(target.x0, target.y0, target.x1, max(page_bottom, target.y0 + 1))
        spare, fitted = pdf_page.insert_htmlbox(target, html_text, css=css_at(scale), archive=registry.archive,
                                                scale_low=0)
        outcome.overflows.append(_overflow_issue(
            page, element, Severity.WARNING,
            f"le texte sortirait de la page ; réduit à {scale * fitted:.0%} pour rester visible."))
        outcome.text_blocks += 1
        return

    spare, fitted = pdf_page.insert_htmlbox(target, html_text, css=css_at(scale), archive=registry.archive,
                                            scale_low=1)
    if spare < 0:
        # la mesure et l'écriture diffèrent (mot plus large que la boîte) : réduction libre, signalée
        spare, fitted = pdf_page.insert_htmlbox(target, html_text, css=css_at(scale), archive=registry.archive,
                                                scale_low=0)
        outcome.overflows.append(_overflow_issue(
            page, element, Severity.WARNING,
            f"un mot est plus large que la boîte ; contenu réduit à {scale * fitted:.0%}."))
    outcome.text_blocks += 1


def _draw_image(pdf_page: pymupdf.Page, page: ComposedPage, image: ImageElement, outcome: _PageOutcome,
                keep_proportion: bool = True) -> None:
    path = Path(image.file)
    if page.source_image and path == Path(page.source_image):
        outcome.issues.append(Issue(
            code="source_photo_refused", severity=Severity.ERROR, page_id=page.page_id, block_id=image.block_id,
            message=f"Page {page.page_number} : la photo source de la page n'est pas insérée comme image "
                    "(le texte ne doit jamais être remplacé par une image)."))
        return
    if not path.is_file():
        outcome.issues.append(Issue(
            code="image_missing", severity=Severity.ERROR, page_id=page.page_id, block_id=image.block_id,
            message=f"Page {page.page_number} : image introuvable ({path.name})."))
        return
    pdf_page.insert_image(to_pymupdf_rect(image.rect), filename=str(path), keep_proportion=keep_proportion)
    outcome.images += 1


def _not_analysed_issue(page: ComposedPage) -> Issue:
    return Issue(code="page_not_analysed", severity=Severity.WARNING, page_id=page.page_id,
                 message=f"Page {page.page_number} non analysée : page vide dans l'export.")


def _render_reconstruction(pdf_page: pymupdf.Page, page: ComposedPage, registry: _FontRegistry,
                           policy: OverflowPolicy) -> _PageOutcome:
    outcome = _PageOutcome(issues=list(page.issues))
    if not page.analysed:
        outcome.issues.append(_not_analysed_issue(page))
        return outcome
    for background in page.backgrounds:
        # un fond est la découpe de sa propre région : il remplit son rect (comme l'ancre DOCX)
        _draw_image(pdf_page, page, background, outcome, keep_proportion=False)
    # images avant le texte, puis ordre z (tri stable : l'ordre de lecture est conservé à z égal)
    for element in sorted(page.elements, key=lambda item: item.z_index):
        if isinstance(element, ImageElement):
            _draw_image(pdf_page, page, element, outcome)
        elif element.text.strip():
            _draw_text(pdf_page, page, element, registry, policy, outcome)
        else:
            outcome.issues.append(Issue(code="empty_text_block", severity=Severity.INFO, page_id=page.page_id,
                                        block_id=element.block_id, message="Bloc de texte vide ignoré."))
    return outcome


def _insert_invisible_text(pdf_page: pymupdf.Page, element: TextElement, face: _FontFace) -> bool:
    """Texte invisible (render_mode=3) dans le rect du bloc ; taille réduite jusqu'à ce qu'il tienne."""
    rect = to_pymupdf_rect(element.rect)
    size = element.style.font_size_pt
    while size >= FACSIMILE_MIN_FONT_PT:
        result = pdf_page.insert_textbox(rect, element.text, fontsize=size, fontname=face.pymupdf_name,
                                         fontfile=str(face.choice.path), render_mode=3,
                                         align=TEXTBOX_ALIGN[element.style.alignment])
        if result >= 0:
            return True
        size *= 0.85
    # dernier recours : une seule ligne au coin du bloc (texte toujours recherchable)
    pdf_page.insert_text(rect.tl + (0, FACSIMILE_MIN_FONT_PT), normalized_text(element.text),
                         fontsize=FACSIMILE_MIN_FONT_PT, fontname=face.pymupdf_name,
                         fontfile=str(face.choice.path), render_mode=3)
    return False


def _render_facsimile(pdf_page: pymupdf.Page, page: ComposedPage, registry: _FontRegistry,
                      policy: OverflowPolicy) -> _PageOutcome:
    if not page.analysed:
        outcome = _PageOutcome(issues=list(page.issues))
        outcome.issues.append(_not_analysed_issue(page))
        return outcome
    if not page.source_image or not Path(page.source_image).is_file():
        outcome = _render_reconstruction(pdf_page, page, registry, policy)
        outcome.fallbacks.append(Issue(
            code="facsimile_image_missing", severity=Severity.WARNING, page_id=page.page_id,
            message=f"Page {page.page_number} : image de page absente, page reconstruite au lieu du fac-similé."))
        return outcome
    outcome = _PageOutcome(issues=list(page.issues))
    pdf_page.insert_image(pdf_page.rect, filename=page.source_image, keep_proportion=False)
    outcome.images += 1
    for element in page.text_elements:
        if not element.text.strip():
            continue
        face = registry.face(element.style)
        if not _insert_invisible_text(pdf_page, element, face):
            outcome.fallbacks.append(Issue(
                code="facsimile_text_compressed", severity=Severity.INFO, page_id=page.page_id,
                block_id=element.block_id,
                message="Texte invisible du bloc écrit sur une seule ligne (boîte trop petite)."))
        outcome.text_blocks += 1
    return outcome


def _render_into(document: pymupdf.Document, page: ComposedPage, registry: _FontRegistry,
                 searchable_facsimile: bool, policy: OverflowPolicy) -> _PageOutcome:
    if page.width_pt <= 0 or page.height_pt <= 0:
        raise ExportError("Dimensions de page invalides.", detail=f"{page.page_id}: {page.width_pt}x{page.height_pt}")
    pdf_page = document.new_page(width=page.width_pt, height=page.height_pt)
    if searchable_facsimile:
        return _render_facsimile(pdf_page, page, registry, policy)
    return _render_reconstruction(pdf_page, page, registry, policy)


def _save_atomically(document: pymupdf.Document, output: Path) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_name(f".{output.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        document.subset_fonts()
        document.save(temporary, garbage=4, deflate=True)  # 4 : fusionne les copies identiques des polices
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()


def _mode_label(pages: list[ComposedPage], searchable_facsimile: bool) -> str:
    if searchable_facsimile:
        return "facsimile"
    modes = sorted({str(page.mode) for page in pages})
    return modes[0] if len(modes) == 1 else ",".join(modes)


# --------------------------------------------------------------------------- API publique


def render_pdf(pages: list[ComposedPage], output: Path, searchable_facsimile: bool = False,
               overflow_policy: OverflowPolicy = "shrink") -> ExportReport:
    """Une page PDF par page composée, aux dimensions exactes ; relit le fichier écrit (nombre de pages)."""
    if not pages:
        raise ExportError("Aucune page à exporter.")
    output = Path(output)
    report = ExportReport(output=str(output), format="pdf", mode=_mode_label(pages, searchable_facsimile))
    registry = _FontRegistry()
    document = pymupdf.open()
    try:
        document.set_metadata({"creator": "book-ocr", "producer": f"PyMuPDF {pymupdf.VersionBind}"})
        for page in pages:
            outcome = _render_into(document, page, registry, searchable_facsimile, overflow_policy)
            report.issues.extend(outcome.issues)
            report.fallbacks.extend(outcome.fallbacks)
            report.overflows.extend(outcome.overflows)
            report.text_blocks += outcome.text_blocks
            report.images += outcome.images
        _save_atomically(document, output)
    finally:
        document.close()

    with pymupdf.open(output) as written:
        report.page_count = written.page_count
    if report.page_count != len(pages):
        report.issues.append(Issue(code="page_count_mismatch", severity=Severity.ERROR,
                                   message=f"Le PDF relu a {report.page_count} pages au lieu de {len(pages)}."))
    logger.info("PDF écrit : %s (%d pages, %d blocs, %d images, %d débordements)", output, report.page_count,
                report.text_blocks, report.images, len(report.overflows))
    return report


def render_preview(page: ComposedPage, width_px: int = 900,
                   overflow_policy: OverflowPolicy = "shrink") -> tuple[Image.Image, list[Issue]]:
    """Aperçu = rendu PDF (même code que `render_pdf`) rasterisé à la largeur demandée."""
    if width_px <= 0:
        raise ValueError("width_px doit être positif")
    registry = _FontRegistry()
    with pymupdf.open() as document:
        outcome = _render_into(document, page, registry, searchable_facsimile=False, policy=overflow_policy)
        zoom = width_px / page.width_pt
        pixmap = document[0].get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    return image, [*outcome.issues, *outcome.fallbacks, *outcome.overflows]


def convert_docx_to_pdf(docx: Path, output_dir: Path, timeout: int | None = None) -> ConversionResult:
    """Conversion LibreOffice headless avec un profil utilisateur temporaire. Sans soffice : available=False."""
    executable = shutil.which(config.SOFFICE_CMD)
    if executable is None:
        return ConversionResult(available=False,
                                message=f"LibreOffice ({config.SOFFICE_CMD}) est absent : conversion DOCX -> PDF "
                                        "impossible. Installation : sudo pacman -S libreoffice-fresh")
    docx = Path(docx)
    if not docx.is_file():
        raise ExportError("Fichier DOCX introuvable pour la conversion.", detail=str(docx))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    limit = timeout if timeout is not None else config.SOFFICE_TIMEOUT_S
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="bookocr_lo_") as profile:
        command = [executable, "--headless", "--norestore", f"-env:UserInstallation={Path(profile).as_uri()}",
                   "--convert-to", "pdf", "--outdir", str(output_dir), str(docx)]
        try:
            done = subprocess.run(command, capture_output=True, text=True, timeout=limit)
        except subprocess.TimeoutExpired:
            return ConversionResult(available=True, duration_s=time.monotonic() - started,
                                    message=f"LibreOffice n'a pas terminé en {limit} s.")
    duration = time.monotonic() - started
    produced = output_dir / f"{docx.stem}.pdf"
    if done.returncode != 0 or not produced.is_file():
        details = (done.stderr or done.stdout or "").strip()[-500:]
        return ConversionResult(available=True, duration_s=duration,
                                message=f"Conversion LibreOffice échouée (code {done.returncode}) : {details}")
    return ConversionResult(available=True, output=str(produced), duration_s=duration,
                            message="Conversion LibreOffice terminée.")


def is_visually_blank(pdf_page: pymupdf.Page) -> bool:
    pixmap = pdf_page.get_pixmap(dpi=BLANK_DPI, colorspace=pymupdf.csGRAY, alpha=False)
    samples = np.frombuffer(pixmap.samples, dtype=np.uint8)
    return float((samples < BLANK_GRAY_LEVEL).mean()) < BLANK_INK_RATIO if samples.size else True


def _validate_page(pdf_page: pymupdf.Page, expected: ComposedPage | None, searchable_facsimile: bool) -> PageValidation:
    text = pdf_page.get_text("text")
    result = PageValidation(page_number=pdf_page.number + 1, width_pt=round(pdf_page.rect.width, 2),
                            height_pt=round(pdf_page.rect.height, 2), chars=len(text.strip()),
                            images=len(pdf_page.get_images(full=True)), blank=is_visually_blank(pdf_page))
    page_id = expected.page_id if expected is not None else None
    label = f"Page {result.page_number}"

    def add(code: str, severity: Severity, message: str) -> None:
        result.issues.append(Issue(code=code, severity=severity, page_id=page_id, message=f"{label} : {message}"))

    for font in pdf_page.get_fonts(full=True):
        if font[1] == "n/a":
            add("font_not_embedded", Severity.WARNING, f"police non intégrée « {font[3]} ».")
    if expected is None:
        if result.blank:
            add("blank_page", Severity.WARNING, "page visuellement blanche.")
        return result

    if (abs(result.width_pt - expected.width_pt) > SIZE_TOLERANCE_PT
            or abs(result.height_pt - expected.height_pt) > SIZE_TOLERANCE_PT):
        add("page_size_mismatch", Severity.WARNING,
            f"taille {result.width_pt:.1f} x {result.height_pt:.1f} pt au lieu de "
            f"{expected.width_pt:.1f} x {expected.height_pt:.1f} pt.")
    if not expected.analysed:
        if result.blank:
            add("page_not_analysed", Severity.WARNING, "page non analysée, laissée vide comme prévu.")
        return result

    expected_text = expected_page_text(expected)
    if expected_text:
        result.text_similarity = round(text_similarity(expected_text, text), 4)
        if result.chars == 0:
            add("page_without_text", Severity.ERROR, "aucun texte extractible sur une page reconstruite.")
        elif result.text_similarity < TEXT_SIMILARITY_WARNING:
            add("text_similarity_low", Severity.WARNING,
                f"texte relu différent du texte attendu (similarité {result.text_similarity:.2f}).")
    expected_images = len(expected_image_files(expected, searchable_facsimile))
    if result.images < expected_images:
        add("image_missing", Severity.ERROR, f"{result.images} image(s) au lieu de {expected_images}.")
    has_content = bool(expected_text) or expected_images > 0
    if result.blank and has_content:
        add("blank_page", Severity.ERROR, "page blanche alors qu'elle devrait avoir du contenu.")
    return result


def validate_pdf(path: Path, expected: list[ComposedPage] | None = None,
                 searchable_facsimile: bool = False) -> ValidationReport:
    """Relit le PDF : pages (nombre, tailles), texte (similarité), images, pages blanches, polices.

    Les problèmes d'une page sont dans `PageValidation.issues` et recopiés dans `ValidationReport.issues`
    (dont dépend `ok`). `searchable_facsimile=True` : une image attendue par page (la photo de la page).
    """
    path = Path(path)
    try:
        document = pymupdf.open(path)
    except (pymupdf.FileDataError, pymupdf.FileNotFoundError, RuntimeError) as error:
        return ValidationReport(file=str(path), page_count=0,
                                expected_page_count=len(expected) if expected is not None else None,
                                issues=[Issue(code="pdf_unreadable", severity=Severity.ERROR,
                                              message=f"PDF illisible : {error}")])
    with document:
        report = ValidationReport(file=str(path), page_count=document.page_count,
                                  expected_page_count=len(expected) if expected is not None else None)
        if expected is not None and document.page_count != len(expected):
            report.issues.append(Issue(code="page_count_mismatch", severity=Severity.ERROR,
                                       message=f"{document.page_count} pages au lieu de {len(expected)}."))
        for index, pdf_page in enumerate(document):
            page_expected = expected[index] if expected is not None and index < len(expected) else None
            page_report = _validate_page(pdf_page, page_expected, searchable_facsimile)
            report.pages.append(page_report)
            report.issues.extend(page_report.issues)
    return report



def pdf_page_count(path: Path) -> int:
    """Nombre de pages d'un PDF existant (ExportError s'il est illisible)."""
    try:
        with pymupdf.open(Path(path)) as document:
            return document.page_count
    except (pymupdf.FileDataError, pymupdf.FileNotFoundError, RuntimeError) as error:
        raise ExportError("PDF illisible.", detail=f"{path}: {error}") from error


def rasterize_pdf_page(path: Path, index: int, width_px: int = 700) -> Image.Image:
    """Page `index` (0..) d'un PDF existant, rendue en RGB à la largeur demandée."""
    if width_px <= 0:
        raise ValueError("width_px doit être positif")
    try:
        document = pymupdf.open(Path(path))
    except (pymupdf.FileDataError, pymupdf.FileNotFoundError, RuntimeError) as error:
        raise ExportError("PDF illisible.", detail=f"{path}: {error}") from error
    with document:
        if not 0 <= index < document.page_count:
            raise IndexError(f"page {index} hors limites : le PDF a {document.page_count} page(s)")
        pdf_page = document[index]
        zoom = width_px / pdf_page.rect.width
        pixmap = pdf_page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
        return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
