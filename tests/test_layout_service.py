"""Mise en page : blocs, types, colonnes, ordre de lecture, catégories, images, fonds, styles, fusion.

OCR « parfait » issu de la vérité terrain (`SyntheticPage.truth_ocr`) : on teste la mise en page sans dépendre de
la qualité de Tesseract. Un test marqué `tesseract` vérifie la chaîne sur un vrai OCR.
"""
from __future__ import annotations

import shutil
import subprocess
from functools import cache

import cv2
import numpy as np
import pytest

from models import (BlockKind, Box, ImageBlock, ImageKind, LayoutSettings, OcrResult, OcrWord, Page, PageCategory,
                    PageFormat, STANDARD_FORMATS, TextBlock, TextStyle)
from services import layout_service
from tests.conftest import requires_languages
from tests.fixtures import synthetic

BOX_TOLERANCE = 0.02
SETTINGS = LayoutSettings(languages=["fra"], low_confidence=60)


@cache
def _page(name: str) -> synthetic.SyntheticPage:
    return getattr(synthetic, name)()


@cache
def _analysed(name: str):
    page = _page(name)
    return layout_service.analyse_page(page.truth_ocr(), page.image, SETTINGS)


def _assert_box_close(found: Box, expected: Box, tolerance: float = BOX_TOLERANCE) -> None:
    for attribute in ("x", "y", "right", "bottom"):
        assert abs(getattr(found, attribute) - getattr(expected, attribute)) <= tolerance, (attribute, found, expected)


def _match(result, truth_block, page) -> TextBlock:
    expected = page.box(truth_block.box_px)
    return max(result.text_blocks, key=lambda block: block.box.iou(expected))


def _flat_ocr(page: synthetic.SyntheticPage, fuse_columns: bool = False) -> OcrResult:
    """OCR sans structure Tesseract fiable : un seul bloc et un seul paragraphe ; avec `fuse_columns`, les lignes
    des deux colonnes situées à la même hauteur partagent un numéro de ligne (gouttière fusionnée)."""
    ocr = page.truth_ocr()
    tops: dict[tuple[int, int], int] = {}
    for truth in page.words:
        key = (truth.block_index, truth.line_index)
        tops[key] = min(tops.get(key, 10 ** 9), truth.box_px[1])
    rows: dict[tuple[int, int], tuple[int, int]] = {}
    anchors: list[tuple[int, tuple[int, int]]] = []
    for key, top in sorted(tops.items(), key=lambda item: item[1]):
        anchor = next((anchor for value, anchor in anchors if abs(value - top) <= 6), None)
        if anchor is None:
            anchor = key
            anchors.append((top, key))
        rows[key] = anchor
    words = []
    for word, truth in zip(ocr.words, page.words):
        key = (truth.block_index, truth.line_index)
        paragraph = page.blocks[truth.block_index].kind == BlockKind.PARAGRAPH
        row = rows[key] if (fuse_columns and paragraph) else key
        words.append(word.model_copy(update={"block_num": 1, "par_num": 1, "line_num": 1 + row[0] * 100 + row[1]}))
    return ocr.model_copy(update={"words": words})


# --------------------------------------------------------------------------- exigence 8 : trois catégories


@pytest.mark.parametrize("name, category", [
    ("text_page", PageCategory.TEXT),
    ("graphic_background_page", PageCategory.GRAPHIC_BACKGROUND),
    ("photo_text_page", PageCategory.PHOTO_TEXT),
])
def test_page_category(name, category):
    result = _analysed(name)
    assert result.category == category == _page(name).category


@pytest.mark.parametrize("name", ["text_page", "two_column_page", "graphic_background_page", "photo_text_page"])
def test_blocks_match_truth(name):
    """Un bloc détecté par bloc de vérité : même type, même texte, boîte à ±2 %."""
    page, result = _page(name), _analysed(name)
    assert len(result.text_blocks) == len(page.blocks)
    for truth in page.blocks:
        block = _match(result, truth, page)
        assert block.kind == truth.kind, (truth.text, block.kind)
        assert block.ocr_text == truth.text
        assert block.on_image == truth.on_image
        _assert_box_close(block.box, page.box(truth.box_px))
        assert len(block.lines) == len(truth.lines)
        assert all(line.words for line in block.lines)


def test_orders_cover_all_blocks():
    result = _analysed("photo_text_page")
    orders = sorted(block.order for block in [*result.text_blocks, *result.image_blocks])
    assert orders == list(range(len(orders)))


# --------------------------------------------------------------------------- exigence 6 : colonnes et ordre


def test_reading_order_two_columns():
    page, result = _page("two_column_page"), _analysed("two_column_page")
    assert result.columns == 2
    ordered = sorted(result.text_blocks, key=lambda block: block.order)
    expected = [page.blocks[0], *[b for b in page.blocks if b.column == 0 and b.kind == BlockKind.PARAGRAPH],
                *[b for b in page.blocks if b.column == 1 and b.kind == BlockKind.PARAGRAPH], page.blocks[-1]]
    assert [block.ocr_text for block in ordered] == [truth.text for truth in expected]
    assert layout_service.reading_order(result.text_blocks) == [block.id for block in ordered]


def test_lines_fused_across_gutter_are_split():
    page = _page("two_column_page")
    result = layout_service.analyse_page(_flat_ocr(page, fuse_columns=True), page.image, SETTINGS)
    paragraphs = sorted((b for b in result.text_blocks if b.kind == BlockKind.PARAGRAPH), key=lambda b: b.order)
    truth = [b.text for b in page.blocks if b.kind == BlockKind.PARAGRAPH and b.column == 0] + \
            [b.text for b in page.blocks if b.kind == BlockKind.PARAGRAPH and b.column == 1]
    assert [block.ocr_text for block in paragraphs] == truth
    assert result.columns == 2


def test_reading_order_headers_first_footers_last():
    def text(kind, y, x=0.1, w=0.8, h=0.02):
        return TextBlock(kind=kind, box=Box(x=x, y=y, w=w, h=h), ocr_text=kind.value)

    footer = text(BlockKind.PAGE_NUMBER, 0.95, x=0.45, w=0.1)
    right = text(BlockKind.PARAGRAPH, 0.30, x=0.55, w=0.35, h=0.4)
    left = text(BlockKind.PARAGRAPH, 0.32, x=0.1, w=0.35, h=0.4)
    title = text(BlockKind.HEADING_1, 0.15)
    header = text(BlockKind.HEADER, 0.03)
    picture = ImageBlock(box=Box(x=0.1, y=0.75, w=0.8, h=0.15))
    order = layout_service.reading_order([footer, right, picture, left, title, header])
    assert order == [header.id, title.id, left.id, right.id, picture.id, footer.id]


# --------------------------------------------------------------------------- exigence 7 : paragraphes séparés


def test_paragraphs_are_separate_without_tesseract_structure():
    """Même sans les blocs Tesseract, la géométrie (retrait, ligne courte, interligne) sépare les paragraphes."""
    page = _page("text_page")
    result = layout_service.analyse_page(_flat_ocr(page), page.image, SETTINGS)
    body = [block for block in result.text_blocks if block.kind not in (BlockKind.HEADER, BlockKind.PAGE_NUMBER)]
    assert len(body) > 1
    paragraphs = sorted((b for b in result.text_blocks if b.kind == BlockKind.PARAGRAPH), key=lambda b: b.order)
    assert [b.ocr_text for b in paragraphs] == [t.text for t in page.blocks if t.kind == BlockKind.PARAGRAPH]
    assert [b.kind for b in sorted(result.text_blocks, key=lambda b: b.order)] == [t.kind for t in page.blocks]
    assert not any(issue.code == "layout_single_block" for issue in result.issues)


def test_hyphenation_is_rejoined():
    width, height = 1000, 1400
    rgb = np.full((height, width, 3), 250, np.uint8)

    def word(text, left, top, right, line):
        return OcrWord(text=text, box=Box.from_pixels(left, top, right, top + 30, width, height), confidence=95,
                       block_num=1, par_num=1, line_num=line)

    words = [word("Le", 100, 400, 140, 1), word("long", 155, 400, 230, 1), word("voya-", 245, 400, 900, 1),
             word("geur", 100, 440, 170, 2), word("et", 185, 440, 215, 2), word("Jean-", 230, 440, 900, 2),
             word("Pierre", 100, 480, 190, 3), word("arrivent.", 205, 480, 330, 3)]
    result = layout_service.analyse_page(OcrResult(words=words, width=width, height=height), rgb, SETTINGS)
    assert [block.ocr_text for block in result.text_blocks] == ["Le long voyageur et Jean-Pierre arrivent."]


# --------------------------------------------------------------------------- types et styles (cas A)


def test_text_page_kinds_styles_and_format():
    page, result = _page("text_page"), _analysed("text_page")
    assert result.detected_format == STANDARD_FORMATS["A5"]
    assert result.body_font_size_pt == pytest.approx(11, abs=0.6)
    assert result.printed_page_number == "12"
    assert result.columns == 1 and not result.image_blocks and not result.background_regions
    by_kind = {}
    for truth in page.blocks:
        by_kind.setdefault(truth.kind, []).append(_match(result, truth, page))
    heading_1, = by_kind[BlockKind.HEADING_1]
    heading_2, = by_kind[BlockKind.HEADING_2]
    assert heading_1.estimated_style.bold is True
    assert heading_2.estimated_style.italic is True
    assert heading_1.estimated_style.alignment == heading_2.estimated_style.alignment == "center"
    assert heading_1.estimated_style.font_size_pt == pytest.approx(20, rel=0.08)
    for paragraph in by_kind[BlockKind.PARAGRAPH]:
        style = paragraph.estimated_style
        assert style.bold is False and style.italic is False and style.color == "#000000"
        assert style.font_size_pt == pytest.approx(11, rel=0.06)
        assert style.alignment == "left"
        assert style.first_line_indent_pt == pytest.approx(14, abs=1.5)
        assert style.line_spacing == pytest.approx(1.2 / 1.15, abs=0.06)       # multiple de l'interligne simple
        assert paragraph.language == "fra" and paragraph.confidence == pytest.approx(95)
        assert not paragraph.needs_review
    assert len(by_kind[BlockKind.LIST_ITEM]) == 3
    first, second = sorted(by_kind[BlockKind.PARAGRAPH], key=lambda b: b.order)[:2]
    assert first.estimated_style.space_after_pt == pytest.approx(6, abs=1.5)
    content = result.content_box
    assert content is not None and content.y < heading_1.box.y + 1e-6 and content.bottom < 0.9


def test_page_number_only_digits_or_short_roman():
    width, height = 1165, 1654
    rgb = np.full((height, width, 3), 250, np.uint8)

    def ocr_with_footer(text: str) -> OcrResult:
        words = [OcrWord(text=w, box=Box.from_pixels(150 + 90 * i, 400, 230 + 90 * i, 430, width, height),
                         confidence=95, block_num=1, par_num=1, line_num=1) for i, w in enumerate("Une ligne de texte".split())]
        words.append(OcrWord(text=text, box=Box.from_pixels(560, 1560, 610, 1585, width, height), confidence=95,
                             block_num=2, par_num=1, line_num=1))
        return OcrResult(words=words, width=width, height=height)

    for text, expected in [("xiv", "xiv"), ("128", "128"), ("mix", None), ("civil", None), ("12345", None)]:
        result = layout_service.analyse_page(ocr_with_footer(text), rgb, SETTINGS)
        footer = next(block for block in result.text_blocks if block.ocr_text == text)
        assert result.printed_page_number == expected, text
        assert footer.kind == (BlockKind.PAGE_NUMBER if expected else BlockKind.FOOTER), text


def test_low_confidence_blocks_need_review():
    page = _page("text_page")
    result = layout_service.analyse_page(page.truth_ocr(confidence=40), page.image, SETTINGS)
    assert all(block.needs_review for block in result.text_blocks)
    assert any(issue.code == "layout_low_confidence" for issue in result.issues)
    assert result.mean_confidence == pytest.approx(40)
    # un seul mot faible suffit à signaler son bloc
    ocr = page.truth_ocr()
    words = list(ocr.words)
    weak_index = next(i for i, w in enumerate(words) if w.text == "rivière,")
    words[weak_index] = words[weak_index].model_copy(update={"confidence": 20})
    result = layout_service.analyse_page(ocr.model_copy(update={"words": words}), page.image, SETTINGS)
    flagged = [block for block in result.text_blocks if block.needs_review]
    assert len(flagged) == 1 and "rivière" in flagged[0].ocr_text


# --------------------------------------------------------------------------- cas B et C, texte sur image


def test_graphic_background_regions_and_text_on_background():
    page, result = _page("graphic_background_page"), _analysed("graphic_background_page")
    assert len(result.background_regions) == 1
    _assert_box_close(result.background_regions[0], page.box(page.background_regions_px[0]))
    assert not result.image_blocks                       # fonds et images sont disjoints
    on_background = [block for block in result.text_blocks if block.on_image]
    assert {block.kind for block in on_background} == {BlockKind.HEADING_1, BlockKind.PARAGRAPH}
    assert all(block.estimated_style.color == "#FFFFFF" for block in on_background)
    below = next(block for block in result.text_blocks if block.kind == BlockKind.PARAGRAPH and not block.on_image)
    assert below.estimated_style.color == "#000000"
    assert result.detected_format == STANDARD_FORMATS["A5"]


def test_photo_detected_within_two_percent_and_caption_linked():
    page, result = _page("photo_text_page"), _analysed("photo_text_page")
    assert len(result.image_blocks) == 1
    image = result.image_blocks[0]
    assert image.kind == ImageKind.PHOTO and image.file == ""
    _assert_box_close(image.box, page.box(page.images[0].box_px))
    caption = next(block for block in result.text_blocks if block.kind == BlockKind.CAPTION)
    assert image.caption_block_id == caption.id
    assert caption.estimated_style.italic is True
    assert image.order < caption.order


def test_illustrations_on_plain_text_page():
    assert layout_service.detect_illustrations(_page("text_page").image, []) == []
    regions = layout_service.detect_illustrations(_page("photo_text_page").image, [])
    assert [region.kind for region in regions] == [ImageKind.PHOTO]
    assert 0 < regions[0].confidence <= 1


def _text_on_photo_page() -> synthetic.SyntheticPage:
    page = synthetic.blank_page("text_on_photo")
    writer = synthetic.PageWriter(page)
    writer.y = int(0.08 * synthetic.PAGE_H)
    picture = writer.picture(int(0.3 * synthetic.PAGE_H), seed=4)
    writer.block(BlockKind.HEADING_1, "Le col au printemps", 22, bold=True, style="sans", color=(255, 255, 255),
                 align="center", on_image=True, y=picture.box_px[1] + 60)
    writer.skip(20)
    for paragraph in synthetic.FRENCH_PARAGRAPHS[:2]:
        writer.block(BlockKind.PARAGRAPH, paragraph, 11, indent_pt=14)
        writer.skip(6)
    return writer.finish()


def test_text_on_photo_is_kept_and_marked():
    page = _text_on_photo_page()
    result = layout_service.analyse_page(page.truth_ocr(confidence=30), page.image, SETTINGS)
    overlay = [block for block in result.text_blocks if block.on_image]
    assert [block.ocr_text for block in overlay] == ["Le col au printemps"]     # jamais jeté, même peu sûr
    assert overlay[0].needs_review
    assert len(result.image_blocks) == 1 and not result.background_regions
    assert overlay[0].box.fraction_inside(result.image_blocks[0].box) > 0.9
    assert result.image_blocks[0].caption_block_id is None
    assert result.category == PageCategory.MIXED


def test_ruled_table_becomes_one_table_block():
    page = synthetic.blank_page("table")
    writer = synthetic.PageWriter(page)
    left, top, cell_w, cell_h = 180, 400, 260, 70
    rows = [["Année", "Lieu", "Habitants"], ["1850", "Village", "420"], ["1900", "Bourg", "980"],
            ["1950", "Ville", "2150"]]
    canvas = page.image
    for r in range(len(rows) + 1):
        cv2.line(canvas, (left, top + r * cell_h), (left + 3 * cell_w, top + r * cell_h), (20, 20, 20), 2)
    for c in range(4):
        cv2.line(canvas, (left + c * cell_w, top), (left + c * cell_w, top + len(rows) * cell_h), (20, 20, 20), 2)
    writer.canvas = synthetic.Image.fromarray(canvas)
    writer.draw = synthetic.ImageDraw.Draw(writer.canvas)
    for r, row in enumerate(rows):
        for c, cell in enumerate(row):
            x = left + c * cell_w + 20
            writer.block(BlockKind.TABLE, cell, 11, x_range=(x, x + cell_w - 40), y=top + r * cell_h + 20)
    writer.y = top + len(rows) * cell_h + 60
    writer.block(BlockKind.PARAGRAPH, synthetic.FRENCH_PARAGRAPHS[0], 11, indent_pt=14)
    page = writer.finish()
    result = layout_service.analyse_page(page.truth_ocr(), page.image, SETTINGS)
    tables = [block for block in result.text_blocks if block.kind == BlockKind.TABLE]
    assert len(tables) == 1 and tables[0].needs_review
    assert tables[0].ocr_text.split("\n") == ["\t".join(row) for row in rows]
    assert not result.image_blocks
    assert any(issue.code == "layout_table" for issue in result.issues)


# --------------------------------------------------------------------------- format de page


def test_detect_page_format_ratio_and_body_size():
    a5 = STANDARD_FORMATS["A5"]
    width, height = 1165, 1654                        # A5 à 200 dpi
    assert layout_service.detect_page_format(width, height, 11 * width / a5.width_pt)[0] == a5
    a4 = STANDARD_FORMATS["A4"]
    assert layout_service.detect_page_format(width, height, 11 * width / a4.width_pt)[0] == a4
    fmt, issues = layout_service.detect_page_format(1000, 1000, 30)
    assert fmt == a5 and issues and issues[0].code == "layout_format_guess"
    fmt, issues = layout_service.detect_page_format(width, height, 2.0)   # corps implausible partout
    assert fmt == a5 and issues[0].code == "layout_format_guess"


# --------------------------------------------------------------------------- fusion avec les corrections


def test_merge_layout_keeps_user_work_and_returns_a_copy():
    fresh = _analysed("text_page")
    page = Page(source_file="originals/p.png", source_sha256="0" * 64, original_name="p.png")
    page = layout_service.merge_layout(page, fresh)
    assert len(page.text_blocks) == len(fresh.text_blocks)
    paragraph = next(block for block in page.text_blocks if block.kind == BlockKind.PARAGRAPH)
    paragraph.corrected_text = "Texte corrigé à la main."
    paragraph.user_modified = True
    added = TextBlock(box=Box(x=0.1, y=0.8, w=0.3, h=0.03), ocr_text="", corrected_text="Ajout", user_created=True)
    page.text_blocks.append(added)
    styled = next(block for block in page.text_blocks if block.kind == BlockKind.LIST_ITEM)
    styled.style_override = TextStyle(bold=True)
    page.category_override = PageCategory.MIXED
    page.format_override = PageFormat(name="Perso", width_mm=140, height_mm=210)
    page.style_rules = {BlockKind.PARAGRAPH: TextStyle(font_family="Liberation Sans")}
    before = page.model_dump()

    again = _analysed("text_page")
    merged = layout_service.merge_layout(page, again)
    assert page.model_dump() == before                   # l'original n'est pas modifié
    ids = {block.id for block in merged.text_blocks}
    assert {paragraph.id, added.id, styled.id} <= ids
    kept = merged.text_block(paragraph.id)
    assert kept.text == "Texte corrigé à la main." and kept.user_modified
    assert merged.text_block(styled.id).style_override.bold is True
    assert len(merged.text_blocks) == len(again.text_blocks) + 1   # pas de doublon du paragraphe corrigé
    assert sorted(block.order for block in merged.text_blocks) == list(range(len(merged.text_blocks)))
    assert merged.category_override == PageCategory.MIXED
    assert merged.format_override.name == "Perso"
    assert merged.style_rules == page.style_rules
    assert merged.detected_format == again.detected_format and merged.body_font_size_pt == again.body_font_size_pt
    assert merged.printed_page_number == "12" and merged.mean_confidence == again.mean_confidence


# --------------------------------------------------------------------------- vrai OCR


def _tesseract_ocr(image: np.ndarray, tmp_path) -> OcrResult:
    path = tmp_path / "page.png"
    synthetic.Image.fromarray(image).save(path)
    done = subprocess.run(["tesseract", str(path), "stdout", "-l", "fra", "--psm", "3", "tsv"],
                          capture_output=True, text=True, timeout=300, check=True)
    height, width = image.shape[:2]
    words = []
    for row in done.stdout.splitlines()[1:]:
        cells = row.split("\t")
        if len(cells) < 12 or cells[0] != "5" or not cells[11].strip():
            continue
        left, top, box_w, box_h = (int(value) for value in cells[6:10])
        words.append(OcrWord(text=cells[11].strip(), box=Box.from_pixels(left, top, left + box_w, top + box_h,
                                                                        width, height),
                             confidence=max(-1.0, min(100.0, float(cells[10]))), block_num=int(cells[2]),
                             par_num=int(cells[3]), line_num=int(cells[4]), word_num=int(cells[5])))
    return OcrResult(words=words, width=width, height=height, languages=["fra"])


@pytest.mark.slow
@pytest.mark.tesseract
@requires_languages("fra")
def test_analyse_page_on_real_tesseract_output(tmp_path):
    assert shutil.which("tesseract")
    page = synthetic.text_page()
    result = layout_service.analyse_page(_tesseract_ocr(page.image, tmp_path), page.image, SETTINGS)
    assert result.category == PageCategory.TEXT
    assert result.detected_format == STANDARD_FORMATS["A5"]
    assert result.printed_page_number == "12"
    kinds = [block.kind for block in sorted(result.text_blocks, key=lambda b: b.order)]
    assert kinds.count(BlockKind.PARAGRAPH) == 3
    assert BlockKind.HEADING_1 in kinds and BlockKind.HEADER in kinds and kinds[-1] == BlockKind.PAGE_NUMBER
    assert kinds.count(BlockKind.LIST_ITEM) == 3
    heading = next(block for block in result.text_blocks if block.kind == BlockKind.HEADING_1)
    assert "voyage" in heading.ocr_text


@pytest.mark.slow
@pytest.mark.tesseract
@requires_languages("fra")
def test_two_columns_on_real_tesseract_output(tmp_path):
    page = synthetic.two_column_page()
    result = layout_service.analyse_page(_tesseract_ocr(page.image, tmp_path), page.image, SETTINGS)
    assert result.columns == 2
    ordered = sorted(result.text_blocks, key=lambda b: b.order)
    paragraphs = [block for block in ordered if block.kind == BlockKind.PARAGRAPH]
    assert len(paragraphs) == 4
    starts = [truth.text.split()[0] for truth in page.blocks if truth.kind == BlockKind.PARAGRAPH]
    assert [block.ocr_text.split()[0] for block in paragraphs] == starts      # colonne gauche puis droite
    assert ordered[0].kind == BlockKind.HEADING_1 and ordered[-1].kind == BlockKind.PAGE_NUMBER
