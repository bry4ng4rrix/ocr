"""PDF reconstruction, facsimile, preview and validation (no OCR: projects are built by hand)."""
from __future__ import annotations

import io

import numpy as np
import pypdfium2 as pdfium
import pypdfium2.raw as pdfium_c
import pytest
from PIL import Image

from core import fonts, pdf_generator, pdf_validator
from core.coordinates import mm_to_pt, norm_to_pdf_rect, page_sheet_geometry
from core.document_models import (
    Alignment, BlockType, ImageBlock, OCRLine, OCRWord, OverflowPolicy, Page, PageSize, Project, RenderSettings,
    TextBlock,
)
from core.errors import PDFGenerationError
from core.pdf_generator import generate_pdf, render_page_png, split_paragraphs

FRAME = (1240, 1754)                   # pixels, A4 proportions
UNICODE_TEXT = "Le cœur de l'été — « Élise » paie 12 € à l'œuvre."


# --------------------------------------------------------------------------- helpers

def make_page(blocks=(), frame=FRAME, **kw) -> Page:
    return Page(original_path="originals/p.png", original_width=frame[0], original_height=frame[1],
                frame_width=frame[0], frame_height=frame[1], blocks=list(blocks),
                font_reference_height_pt=frame[1] / 300 * 72, **kw)


def make_project(pages, **render) -> Project:
    return Project(name="Livre de test", pages=list(pages), render=RenderSettings(**render))


def text_block(text, x=0.1, y=0.1, w=0.8, h=0.2, **kw) -> TextBlock:
    return TextBlock(x=x, y=y, width=w, height=h, text=text, ocr_text=kw.pop("ocr_text", text), **kw)


def image_block(x=0.2, y=0.5, w=0.6, h=0.3, **kw) -> ImageBlock:
    return ImageBlock(x=x, y=y, width=w, height=h, image_path="illustrations/i.png", **kw)


def gradient(width=400, height=200) -> Image.Image:
    yy, xx = np.mgrid[0:height, 0:width]
    arr = np.stack([60 + 120 * yy / height, 120 + 80 * xx / width, 230 - 100 * yy / height], axis=-1)
    return Image.fromarray(arr.astype(np.uint8))


def provider_of(image: Image.Image):
    return lambda page, block: image.copy()


def no_image(page, block):
    raise AssertionError("aucune illustration ne devait être demandée")


def build(project, mode="reconstruction", provider=no_image, page_provider=None, **kw):
    buffer = io.BytesIO()
    report = generate_pdf(project, buffer, mode, provider, page_provider, **kw)
    return buffer.getvalue(), report


def page_text(data: bytes, index: int = 0) -> str:
    pdf = pdfium.PdfDocument(data)
    tp = pdf[index].get_textpage()
    return tp.get_text_range()


def char_boxes(data: bytes, index: int = 0) -> list[tuple[str, tuple[float, float, float, float]]]:
    """(character, (left, bottom, right, top)) for every non-space character of the page."""
    pdf = pdfium.PdfDocument(data)
    tp = pdf[index].get_textpage()
    out = []
    for i in range(tp.count_chars()):
        ch = tp.get_text_range(i, 1)
        if ch.strip():
            out.append((ch, tp.get_charbox(i)))
    return out


def rows_of(boxes) -> list[list]:
    """Characters grouped by text line (top line first)."""
    rows: list[list] = []
    for item in sorted(boxes, key=lambda b: -b[1][1]):
        if rows and abs(rows[-1][0][1][1] - item[1][1]) < 2:
            rows[-1].append(item)
        else:
            rows.append([item])
    return rows


def bounds(boxes) -> tuple[float, float, float, float]:
    return (min(b[0] for _, b in boxes), min(b[1] for _, b in boxes),
            max(b[2] for _, b in boxes), max(b[3] for _, b in boxes))


def images_of(data: bytes, index: int = 0):
    pdf = pdfium.PdfDocument(data)
    return list(pdf[index].get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_IMAGE], max_depth=15))


def page_sizes(data: bytes) -> list[tuple[float, float]]:
    pdf = pdfium.PdfDocument(data)
    return [pdf[i].get_size() for i in range(len(pdf))]


# --------------------------------------------------------------------------- fonts

def test_fonts_generic_aliases_and_fallbacks():
    families = fonts.available_families()
    assert {"Serif", "Sans", "Mono"} <= set(families)
    assert families["Serif"].alias_of
    assert fonts.family_names()[:3] == ["Serif", "Sans", "Mono"]
    regular = fonts.resolve("Serif")
    assert fonts.resolve("Serif") == regular                      # registered once
    assert fonts.resolve("Police inexistante") == regular        # unknown family -> Serif
    assert fonts.resolve("Serif", bold=True) != regular
    assert fonts.missing_characters(regular, UNICODE_TEXT) == set()
    assert "漢" in fonts.missing_characters(regular, "a漢")


def test_missing_character_uses_fallback_font_and_warns():
    sample = "Échec ♞ cavalier"
    if not fonts.missing_characters(fonts.resolve("Serif"), sample):
        pytest.skip("la police Serif dessine déjà ce caractère")
    data, report = build(make_project([make_page([text_block(sample)])]))
    assert any("police de remplacement" in w for w in report.warnings)
    assert "♞" in page_text(data)


# --------------------------------------------------------------------------- text

def test_text_is_extractable_unicode_and_fonts_are_embedded():
    project = make_project([make_page([text_block(UNICODE_TEXT)])])
    data, report = build(project)
    text = page_text(data)
    for ch in "éœ«»—€":
        assert ch in text
    assert pdf_validator.similarity(UNICODE_TEXT, text) > 0.95
    assert b"FontFile2" in data                                  # embedded TrueType (subset)
    assert report.pages[0].reconstructed and report.pages[0].status == "reconstruit"


def test_reconstruction_does_not_export_a_page_sized_drawing_when_ocr_found_no_text():
    page = make_page([image_block(x=0, y=0, w=1, h=1, detection="drawing")])
    data, report = build(make_project([page]))

    assert page_text(data).strip() == ""
    assert not images_of(data)
    assert not report.pages[0].reconstructed
    assert any("Relancez l'OCR" in warning for warning in report.warnings)


def test_metadata_title_author_creator():
    project = make_project([make_page([text_block("Bonjour")])], author="Victor Hugo")
    data, _ = build(project)
    meta = pdfium.PdfDocument(data).get_metadata_dict()
    assert meta["Title"] == "Livre de test"
    assert meta["Author"] == "Victor Hugo"
    assert meta["Creator"] == "LivreOCR Streamlit"


@pytest.mark.parametrize("render", [dict(), dict(page_size=PageSize.A4, margin_mm=20)], ids=["auto", "a4-marge"])
def test_text_position_and_y_axis(render):
    top_block = text_block("HAUTDEPAGE", x=0.1, y=0.05, w=0.5, h=0.06, font_size=14)
    bottom_block = text_block("BASDEPAGE", x=0.4, y=0.85, w=0.5, h=0.06, font_size=14)
    page = make_page([top_block, bottom_block])
    project = make_project([page], **render)
    data, _ = build(project)
    sheet = page_sheet_geometry(project.render, page)
    boxes = char_boxes(data)
    found = {}
    for block, word in ((top_block, "HAUTDEPAGE"), (bottom_block, "BASDEPAGE")):
        x, y, w, h = norm_to_pdf_rect(block.box, sheet)
        mine = [b for b in boxes if b[0] in word]
        mine = [b for b in mine if y - 3 <= b[1][1] and b[1][3] <= y + h + 3]
        assert "".join(ch for ch, _ in mine) == word
        left, bottom, right, top = bounds(mine)
        assert abs(left - x) < 3                                 # starts at the left edge
        assert top <= y + h + 1 and top > y + h - 0.5 * h - 14   # first line at the top of the block
        assert right <= x + w + 1 and bottom >= y - 1
        found[word] = (left, bottom, right, top)
    top_y = found["HAUTDEPAGE"][1]
    bottom_y = found["BASDEPAGE"][3]
    assert top_y > bottom_y                                      # top of the frame = top of the PDF page
    assert top_y > sheet.page_height / 2


def test_alignment_justify_and_last_line():
    words = " ".join(["mot"] * 60)
    block = text_block(words, x=0.1, y=0.1, w=0.6, h=0.4, alignment=Alignment.JUSTIFY, font_size=12)
    page = make_page([block])
    project = make_project([page])
    data, _ = build(project)
    x, y, w, h = norm_to_pdf_rect(block.box, page_sheet_geometry(project.render, page))
    rows = rows_of(char_boxes(data))
    assert len(rows) >= 3
    for row in rows[:-1]:
        assert abs(bounds(row)[2] - (x + w)) < 2                 # stretched to the right edge
    assert bounds(rows[-1])[2] < x + w - 10                      # the last line is not stretched


def test_right_and_center_alignment():
    page = make_page([text_block("droite", y=0.1, h=0.05, alignment=Alignment.RIGHT),
                      text_block("centre", y=0.3, h=0.05, alignment=Alignment.CENTER)])
    project = make_project([page])
    data, _ = build(project)
    x, _, w, _ = norm_to_pdf_rect(page.blocks[0].box, page_sheet_geometry(project.render, page))
    boxes = char_boxes(data)
    right = bounds([b for b in boxes if b[0] in "droite" and b[1][1] > 300])
    centre = bounds([b for b in boxes if b[0] in "centre" and b[1][1] < 300])
    assert abs(right[2] - (x + w)) < 2
    assert abs((centre[0] + centre[2]) / 2 - (x + w / 2)) < 2


def test_paragraph_reflow_hyphenation_and_lists():
    assert split_paragraphs("Une exem-\nple ligne\nsuite.\n\nAutre") == [["Une exemple ligne suite."], ["Autre"]]
    assert split_paragraphs("Jean-\nPierre") == [["Jean-Pierre"]]
    assert split_paragraphs("vers un\nvers deux", keep_line_breaks=True) == [["vers un", "vers deux"]]
    items = split_paragraphs("- premier élément\nqui continue\n- second\n2) troisième", is_list=True)
    assert items == [["- premier élément qui continue", "- second", "2) troisième"]]


def test_list_items_and_first_line_indent_are_rendered():
    block = text_block("- un\n- deux", type=BlockType.LIST, h=0.2)
    para = text_block("Alinéa en retrait", y=0.5, h=0.1, first_line_indent=30)
    page = make_page([block, para])
    project = make_project([page])
    data, _ = build(project)
    sheet = page_sheet_geometry(project.render, page)
    rows = rows_of(char_boxes(data))
    assert len([r for r in rows if min(r, key=lambda c: c[1][0])[0] == "-"]) == 2          # two items, two lines
    x = norm_to_pdf_rect(para.box, sheet)[0]
    first = next(b for ch, b in char_boxes(data) if ch == "A")
    scale = pdf_generator.font_scale(project.render, page, sheet)
    assert abs(first[0] - (x + 30 * scale)) < 2


def test_rotated_block_still_extractable():
    data, report = build(make_project([make_page([text_block("Tourné", rotation=90, x=0.4, y=0.4, w=0.2, h=0.1)])]))
    assert "Tourné" in page_text(data)
    assert not report.pages[0].overflows


# --------------------------------------------------------------------------- page sizes

@pytest.mark.parametrize("frame, render, expected", [
    (FRAME, dict(), (1240 / 300 * 72, 1754 / 300 * 72)),
    (FRAME, dict(auto_dpi=150), (1240 / 150 * 72, 1754 / 150 * 72)),
    (FRAME, dict(page_size=PageSize.A4), (mm_to_pt(210), mm_to_pt(297))),
    ((1754, 1240), dict(page_size=PageSize.A4), (mm_to_pt(297), mm_to_pt(210))),
    (FRAME, dict(page_size=PageSize.CUSTOM, custom_width_mm=100, custom_height_mm=150), (mm_to_pt(100), mm_to_pt(150))),
], ids=["auto300", "auto150", "a4", "a4-paysage", "personnalise"])
def test_page_sizes(frame, render, expected):
    project = make_project([make_page([text_block("x")], frame=frame)], **render)
    data, report = build(project)
    (w, h), = page_sizes(data)
    assert abs(w - expected[0]) < 0.5 and abs(h - expected[1]) < 0.5
    assert abs(report.pages[0].page_width - expected[0]) < 0.5


# --------------------------------------------------------------------------- images

def test_illustration_position_ratio_and_resolution():
    block = image_block(x=0.2, y=0.5, w=0.6, h=0.3)
    page = make_page([block])
    project = make_project([page], image_dpi=72)
    data, report = build(project, provider=provider_of(gradient(4000, 2000)))
    (obj,) = images_of(data)
    left, bottom, right, top = obj.get_bounds()
    x, y, w, h = norm_to_pdf_rect(block.box, page_sheet_geometry(project.render, page))
    assert abs((right - left) / (top - bottom) - 2.0) < 0.02         # ratio kept
    assert abs((left + right) / 2 - (x + w / 2)) < 1 and abs((bottom + top) / 2 - (y + h / 2)) < 1   # centred
    assert left >= x - 0.5 and right <= x + w + 0.5 and bottom >= y - 0.5 and top <= y + h + 0.5
    px_w, px_h = obj.get_px_size()
    assert px_w <= (right - left) / 72 * 72 + 1 and px_h <= (top - bottom) / 72 * 72 + 1   # <= image_dpi
    assert report.pages[0].image_blocks == 1


def test_small_illustration_is_not_upsampled_and_stretch_mode():
    block = image_block(preserve_aspect_ratio=False)
    page = make_page([block])
    project = make_project([page])
    data, _ = build(project, provider=provider_of(gradient(40, 20)))
    (obj,) = images_of(data)
    assert tuple(obj.get_px_size()) == (40, 20)
    x, y, w, h = norm_to_pdf_rect(block.box, page_sheet_geometry(project.render, page))
    left, bottom, right, top = obj.get_bounds()
    assert abs(right - left - w) < 0.5 and abs(top - bottom - h) < 0.5   # stretched to the block


def test_one_image_per_image_block_and_jpeg_option():
    page = make_page([image_block(y=0.1, h=0.2), image_block(y=0.5, h=0.2)])
    data, report = build(make_project([page]), provider=provider_of(gradient()))
    assert len(images_of(data)) == 2 and report.pages[0].image_blocks == 2
    assert b"/DCTDecode" not in data                                 # lossless by default
    data_jpeg, _ = build(make_project([page], jpeg_quality=80), provider=provider_of(gradient()))
    assert b"/DCTDecode" in data_jpeg


def _mean_colour(project, page, provider, block) -> np.ndarray:
    image, _ = render_page_png(project, page, provider, width_px=600)
    sheet = page_sheet_geometry(project.render, page)
    x, y, w, h = norm_to_pdf_rect(block.box, sheet)
    k = 600 / sheet.page_width
    top = (sheet.page_height - (y + h)) * k
    arr = np.asarray(image)[int(top + 5 * k):int(top + (h - 5) * k), int((x + 5) * k):int((x + w - 5) * k)]
    return arr.reshape(-1, 3).mean(axis=0)


def test_modified_illustration_is_the_one_in_the_pdf():
    block = image_block(preserve_aspect_ratio=False)
    page = make_page([block])
    project = make_project([page])
    original = gradient()
    red = Image.blend(original, Image.new("RGB", original.size, (255, 0, 0)), 0.7)
    plain = _mean_colour(project, page, provider_of(original), block)
    tinted = _mean_colour(project, page, provider_of(red), block)
    assert tinted[0] > plain[0] + 40 and tinted[2] < plain[2] - 40
    assert tinted[0] > 180 and tinted[1] < 120


def test_missing_illustration_is_reported_not_fatal():
    def broken(page, block):
        raise FileNotFoundError("illustrations/i.png")
    page = make_page([image_block(), text_block("Texte")])
    data, report = build(make_project([page]), provider=broken)
    assert any("illustration indisponible" in w for w in report.warnings)
    assert not images_of(data) and report.pages[0].reconstructed


# --------------------------------------------------------------------------- overflow and overlaps

LONG = " ".join(["Un texte assez long pour déborder du petit bloc prévu."] * 12)


def test_overflow_shrinks_then_reports():
    block = text_block(LONG[:260], y=0.1, h=0.12, font_size=11)
    page = make_page([block, text_block("Bloc suivant", y=0.24, h=0.05)])
    project = make_project([page], overflow_policy=OverflowPolicy.SHRINK)
    data, report = build(project)
    assert any("taille réduite" in w for w in report.pages[0].warnings)
    assert not report.pages[0].overflows                              # reduction was enough
    tiny = text_block(LONG * 3, y=0.1, h=0.12, font_size=11)
    data, report = build(make_project([make_page([tiny])], overflow_policy=OverflowPolicy.SHRINK))
    page_report = report.pages[0]
    assert len(page_report.overflows) == 1 and "ligne(s) perdue(s)" in page_report.overflows[0]
    assert page_report.lost_lines > 0 and page_report.overflow_blocks == [tiny.id]


def test_overflow_extends_into_free_space_below():
    block = text_block(LONG * 3, y=0.1, h=0.12, font_size=11)
    project = make_project([make_page([block])])                    # SHRINK_THEN_EXTEND by default
    data, report = build(project)
    assert any("prolongé" in w for w in report.pages[0].warnings)
    assert not report.pages[0].overflows
    assert pdf_validator.similarity(block.text, page_text(data)) > 0.95


def test_extension_stops_at_next_block():
    block = text_block(LONG * 6, y=0.1, h=0.12, font_size=11)
    below = text_block("Bloc suivant", y=0.3, h=0.05)
    page = make_page([block, below])
    project = make_project([page])
    data, report = build(project)
    assert report.pages[0].overflows                                  # not enough room until the next block
    sheet = page_sheet_geometry(project.render, page)
    limit = sum(norm_to_pdf_rect(below.box, sheet)[1::2])             # top of the next block
    boxes = [b for ch, b in char_boxes(data) if ch not in "Bloc suivant"]
    assert min(b[1] for b in boxes) >= limit - 1
    assert report.pages[0].overlaps == 0


def test_clip_policy_keeps_size_and_reports():
    block = text_block(LONG, y=0.1, h=0.05, font_size=11)
    data, report = build(make_project([make_page([block])], overflow_policy=OverflowPolicy.CLIP))
    page_report = report.pages[0]
    assert page_report.overflows and not any("taille réduite" in w for w in page_report.warnings)
    assert page_report.lost_lines > 0


def test_overlaps_are_counted():
    page = make_page([text_block("un", y=0.1, h=0.2), text_block("deux", y=0.15, h=0.2)])
    _, report = build(make_project([page]))
    assert report.pages[0].overlaps == 1


# --------------------------------------------------------------------------- hidden and empty pages

def test_hidden_blocks_are_left_out():
    page = make_page([text_block("Visible"), text_block("SECRET", y=0.4, hidden=True), image_block(hidden=True)])
    data, report = build(make_project([page]))
    text = page_text(data)
    assert "Visible" in text and "SECRET" not in text
    assert not images_of(data) and report.pages[0].image_blocks == 0
    data, _ = build(make_project([page], include_hidden_blocks=True), provider=provider_of(gradient()))
    assert "SECRET" in page_text(data) and len(images_of(data)) == 1


@pytest.mark.parametrize("blocks", [[], [text_block("caché", hidden=True)], [text_block("   ")]],
                         ids=["sans-bloc", "tout-masque", "texte-vide"])
def test_page_without_displayable_block_is_blank(blocks):
    project = make_project([make_page(blocks)])
    data, report = build(project)
    page_report = report.pages[0]
    assert page_report.reconstructed is False and page_report.status != "reconstruit"
    assert any("page blanche" in w for w in page_report.warnings)
    assert not images_of(data) and not page_text(data).strip()
    (w, h), = page_sizes(data)
    assert abs(h - FRAME[1] / 300 * 72) < 0.5
    image, _ = render_page_png(project, project.pages[0], no_image, width_px=200)
    assert np.asarray(image).min() == 255                             # really white


def test_unanalysed_page_is_blank_a4():
    page = Page(original_path="originals/p.png")
    data, report = build(make_project([page]))
    assert report.pages[0].reconstructed is False and not images_of(data)
    (w, h), = page_sizes(data)
    assert abs(w - mm_to_pt(210)) < 0.5


# --------------------------------------------------------------------------- facsimile

def _facsimile_page(corrected: bool) -> Page:
    words = [OCRWord(text="Bonjour", x=0.1, y=0.1, width=0.2, height=0.03),
             OCRWord(text="mondo", x=0.32, y=0.1, width=0.15, height=0.03)]
    words2 = [OCRWord(text="seconde", x=0.1, y=0.14, width=0.2, height=0.03),
              OCRWord(text="ligne", x=0.32, y=0.14, width=0.12, height=0.03)]
    lines = [OCRLine(x=0.1, y=0.1, width=0.37, height=0.03, words=words),
             OCRLine(x=0.1, y=0.14, width=0.34, height=0.03, words=words2)]
    ocr = "Bonjour mondo\nseconde ligne"
    block = text_block("Bonjour monde\nseconde ligne" if corrected else ocr, ocr_text=ocr,
                       x=0.1, y=0.1, w=0.4, h=0.07, lines=lines)
    return make_page([block])


def test_facsimile_one_image_per_page_and_invisible_text():
    pages = [_facsimile_page(False), _facsimile_page(True)]
    project = make_project(pages)
    scan = Image.new("RGB", FRAME, (240, 235, 220))
    data, report = build(project, mode="facsimile", page_provider=lambda p: scan)
    assert [len(images_of(data, i)) for i in range(2)] == [1, 1]
    assert "Bonjour" in page_text(data, 0) and "mondo" in page_text(data, 0)
    assert "monde" in page_text(data, 1) and "mondo" not in page_text(data, 1)   # corrected text
    assert all(p.status == "fac-similé : image de la page originale" and not p.reconstructed for p in report.pages)
    pdf = pdfium.PdfDocument(data)
    texts = list(pdf[0].get_objects(filter=[pdfium_c.FPDF_PAGEOBJ_TEXT]))
    assert texts and all(pdfium_c.FPDFTextObj_GetTextRenderMode(t.raw) == pdfium_c.FPDF_TEXTRENDERMODE_INVISIBLE
                         for t in texts)                                          # render mode 3
    image, _ = render_page_png(project, pages[0], no_image, mode="facsimile", width_px=300,
                               page_image_provider=lambda p: scan)
    arr = np.asarray(image).reshape(-1, 3)
    assert arr.min() >= 200                                                       # the text is not visible
    result = pdf_validator.validate_pdf(data, project, "facsimile")
    assert result["ok"] and result["summary"]["images_found"] == 2


def test_facsimile_without_page_images_is_an_error():
    with pytest.raises(PDFGenerationError):
        build(make_project([_facsimile_page(False)]), mode="facsimile")


# --------------------------------------------------------------------------- regeneration, preview, validator

def test_regeneration_reflects_block_edits():
    block = text_block("Premier jet", x=0.1, y=0.1, w=0.5, h=0.1, font_size=12)
    page = make_page([block])
    project = make_project([page])
    before, _ = build(project)
    block.text = "Texte corrigé"
    block.font_size = 24
    block.set_box(block.box.model_copy(update={"y": 0.6}))
    after, _ = build(project)
    assert "Premier" in page_text(before) and "Texte corrigé" in page_text(after)
    old, new = bounds(char_boxes(before)), bounds(char_boxes(after))
    assert new[3] < old[1]                                              # moved down the page
    assert (new[3] - new[1]) > 1.5 * (old[3] - old[1])                  # bigger font


def test_render_page_png_size_and_ratio():
    page = make_page([text_block("Aperçu")])
    project = make_project([page], page_size=PageSize.A4, margin_mm=10)
    image, report = render_page_png(project, page, no_image, width_px=500)
    assert image.mode == "RGB" and image.width == 500
    assert abs(image.height / image.width - 297 / 210) < 0.01
    assert report.reconstructed


def test_page_ids_filter_keeps_project_order():
    pages = [make_page([text_block(f"page{i}")]) for i in range(3)]
    project = make_project(pages)
    data, report = build(project, page_ids=[pages[2].id, pages[0].id])
    assert [p.page_id for p in report.pages] == [pages[0].id, pages[2].id]
    assert "page0" in page_text(data, 0) and "page2" in page_text(data, 1)


def test_validator_is_consistent_and_detects_missing_page(tmp_path):
    pages = [make_page([text_block(UNICODE_TEXT), image_block()]), make_page([])]
    project = make_project(pages)
    path = tmp_path / "livre.pdf"
    report = generate_pdf(project, path, "reconstruction", provider_of(gradient()))
    assert path.exists() and not list(tmp_path.glob("*.part"))
    result = pdf_validator.validate_pdf(path, project, "reconstruction")
    import json
    json.dumps(result)
    assert result["page_count_ok"] and result["ok"], result["errors"]
    first, second = result["pages"]
    assert first["size_ok"] and first["text_similarity"] > 0.95 and first["images_found"] == 1
    assert first["reconstructed"] and not second["reconstructed"]
    assert result["summary"]["blank_pages"] == [1] and result["summary"]["empty_pages"] == [1]
    assert result["summary"]["text_similarity_mean"] > 0.95
    assert len(report.pages) == 2
    wrong = make_project(pages + [make_page([text_block("en plus")])])
    bad = pdf_validator.validate_pdf(path, wrong, "reconstruction")
    assert not bad["page_count_ok"] and not bad["ok"]
    assert any("manquante" in e for e in bad["errors"])


def test_write_error_raises_pdf_generation_error(tmp_path):
    project = make_project([make_page([text_block("x")])])
    with pytest.raises(PDFGenerationError, match="n'existe pas"):
        generate_pdf(project, tmp_path / "absent" / "livre.pdf", "reconstruction", no_image)
    with pytest.raises(PDFGenerationError):
        generate_pdf(project, tmp_path / "x.pdf", "inconnu", no_image)
