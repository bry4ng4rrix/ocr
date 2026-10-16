"""Layout analysis: blocks, types, styles, captions, reading order, on real OCR of synthetic pages."""
import cv2
import numpy as np
import pytest
from PIL import Image, ImageDraw

from core import layout_analysis as la
from core.coordinates import iou, overlap_ratio
from core.document_models import BlockType, Box, ImageBlock, ProcessingSettings, TextBlock
from core.illustration_extraction import DetectedRegion, detect_illustrations
from core.ocr_engine import OCRResult, OCRWordPx, read_page
from tests import synthetic
from tests.conftest import requires_tesseract
from tests.test_illustrations import ruled_table

W, H = synthetic.PAGE_W, synthetic.PAGE_H
PAGE_HEIGHT_PT = 842.0          # A4: font sizes are expected for this page height
SETTINGS = ProcessingSettings()


def analyse(image, gray=None):
    gray = cv2.cvtColor(image, cv2.COLOR_RGB2GRAY) if gray is None else gray
    ocr = read_page(gray, ["fra", "eng"])
    return la.analyse_layout(ocr, image, detect_illustrations(image), PAGE_HEIGHT_PT, SETTINGS)


@pytest.fixture(scope="module")
def pages():
    out = {}
    for name, page in (("simple", synthetic.simple_page()), ("two", synthetic.two_column_page()),
                       ("illustrated", synthetic.illustrated_page())):
        out[name] = (page, analyse(page.image))
    return out


def _truth_box(drawn) -> Box:
    return Box(**dict(zip(("x", "y", "width", "height"), drawn.norm())))


def _match(result, drawn) -> TextBlock:
    texts = [b for b in result.blocks if isinstance(b, TextBlock)]
    return max(texts, key=lambda b: iou(b.box, _truth_box(drawn)))


def _texts(result):
    return [b for b in result.blocks if isinstance(b, TextBlock)]


# --------------------------------------------------------------------------- pure rules (no OCR)

def test_font_size_calibration_on_drawn_words():
    """EM_* constants: a word's ink height divided by its expected em gives the drawn size."""
    image = Image.new("L", (10, 10))
    draw = ImageDraw.Draw(image)
    words = ["Chapitre", "château", "pavée,", "library", "Élise", "Figure", "vallée", "12", "bibliothèque",
             "Seconde", "aujourd'hui,", "enough", "Hélène"]
    errors = []
    for style in ("serif", "serif-bold", "sans", "sans-bold"):
        for size in (20, 30, 56):
            fnt = synthetic.font(size, style)
            for word in words:
                top, bottom = draw.textbbox((0, 0), word, font=fnt)[1::2]
                em = la._word_em(word)
                errors.append(((bottom - top) / em - size) / size)
    errors = np.abs(errors)
    assert errors.max() < 0.12, errors.max()
    assert np.median(errors) < 0.05


@pytest.mark.parametrize("text", ["12", "xiv", "XIV", "— 7 —", "- 152 -", "iii", "p. 12", "Page 3"])
def test_page_number_pattern_accepts(text):
    assert la.PAGE_NUMBER.match(text)


@pytest.mark.parametrize("text", ["mix", "civil", "dim", "MIX", "Chapitre", "XiV", "12345", "lili", "—", "- -", ""])
def test_page_number_pattern_rejects_words(text):
    assert not la.PAGE_NUMBER.match(text)


def _tb(x, y, w, h, **kw) -> TextBlock:
    return TextBlock(x=x, y=y, width=w, height=h, text="t", **kw)


def test_reading_order_columns_and_edges():
    title = _tb(0.35, 0.10, 0.30, 0.04)
    a = _tb(0.08, 0.20, 0.38, 0.20)       # left column, top
    c = _tb(0.54, 0.20, 0.38, 0.30)       # right column, top
    b = _tb(0.08, 0.45, 0.38, 0.20)       # left column, bottom: no neighbour side by side
    full = _tb(0.08, 0.70, 0.84, 0.10)    # full width: new band
    number = _tb(0.48, 0.95, 0.04, 0.02, type=BlockType.PAGE_NUMBER)
    header = _tb(0.08, 0.02, 0.40, 0.02, type=BlockType.HEADER)
    blocks = [number, full, c, b, a, header, title]
    assert la.reading_order(blocks) == 2
    assert blocks == [header, title, a, b, c, full, number]
    assert [x.reading_order for x in blocks] == list(range(7))
    assert (a.column, b.column, c.column) == (0, 0, 1)


def test_reading_order_single_column():
    blocks = [_tb(0.1, 0.5, 0.8, 0.1), _tb(0.1, 0.1, 0.8, 0.1), _tb(0.3, 0.3, 0.2, 0.05)]
    assert la.reading_order(blocks) == 1
    assert [round(b.y, 2) for b in blocks] == [0.1, 0.3, 0.5]


def _word(text, left, top, width, height, conf, block=1, line=1, word=1):
    return OCRWordPx(text, left, top, width, height, conf, block, 1, line, word)


def test_text_inside_picture_dropped_when_unreliable_kept_when_confident():
    picture = DetectedRegion(Box(x=0.2, y=0.2, width=0.6, height=0.4), "photo", 90.0)
    words = [
        _word("Brume", 400, 500, 90, 25, 20.0, block=1), _word("vague", 500, 500, 90, 25, 25.0, block=1, word=2),
        _word("Panneau", 400, 700, 120, 25, 92.0, block=2), _word("lisible", 530, 700, 90, 25, 90.0, block=2, word=2),
        _word("Figure", 400, 880, 80, 25, 90.0, block=3), _word("2.", 490, 880, 20, 25, 91.0, block=3, word=2),
    ]
    ocr = OCRResult(words, W, H, ["fra"])
    result = la.analyse_layout(ocr, np.full((H, W, 3), 255, np.uint8), [picture], PAGE_HEIGHT_PT, SETTINGS)
    texts = {b.text: b for b in _texts(result)}
    assert "Brume vague" not in texts
    assert texts["Panneau lisible"].needs_review and "illustration" in texts["Panneau lisible"].review_reason
    caption = texts["Figure 2."]               # inside the picture box, but a caption: kept and linked
    assert caption.type == BlockType.CAPTION
    image = next(b for b in result.blocks if isinstance(b, ImageBlock))
    assert image.caption_id == caption.id and image.image_path == "" and image.detection == "photo"
    assert any("ignorée" in w for w in result.warnings)


def test_empty_ocr_gives_no_block():
    result = la.analyse_layout(OCRResult([], W, H, ["fra"]), np.full((H, W, 3), 255, np.uint8), [],
                               PAGE_HEIGHT_PT, SETTINGS)
    assert result.blocks == [] and result.mean_confidence == -1.0 and result.printed_page_number == ""
    assert result.warnings


# --------------------------------------------------------------------------- real OCR

@requires_tesseract
def test_blank_page_has_no_block():
    result = analyse(np.full((H, W, 3), 255, np.uint8))
    assert result.blocks == [] and result.columns == 1


@requires_tesseract
def test_simple_page_types_number_and_text(pages):
    page, result = pages["simple"]
    texts = _texts(result)
    assert [b.type for b in texts] == [BlockType.TITLE, BlockType.PARAGRAPH, BlockType.PARAGRAPH,
                                       BlockType.PAGE_NUMBER]
    assert result.printed_page_number == "12"
    for b in texts:
        assert b.text == b.ocr_text
        assert b.confidence > 80 and not b.needs_review
    assert texts[1].language == "fra" and texts[2].language == "eng"
    assert texts[1].text.count("\n") == len(texts[1].lines) - 1
    assert result.columns == 1 and result.mean_confidence > 80
    assert result.margins.left == pytest.approx(120 / W, abs=0.01)
    assert result.margins.top == pytest.approx(152 / H, abs=0.01)


@requires_tesseract
def test_two_columns_reading_order(pages):
    page, result = pages["two"]
    assert result.columns == 2
    texts = _texts(result)
    drawn = page.blocks
    expected = [drawn[0], drawn[1], drawn[2], drawn[3], drawn[4]]   # title, left top/bottom, right top/bottom
    assert len(texts) == 5
    for block, truth in zip(texts, expected):
        assert iou(block.box, _truth_box(truth)) > 0.6, (block.text, truth.text)
    assert texts[0].type == BlockType.TITLE
    assert [b.column for b in texts] == [0, 0, 0, 1, 1]
    assert [b.reading_order for b in result.blocks] == list(range(len(result.blocks)))


@requires_tesseract
def test_illustrated_page(pages):
    page, result = pages["illustrated"]
    images = [b for b in result.blocks if isinstance(b, ImageBlock)]
    assert len(images) == 1
    image = images[0]
    assert iou(image.box, _truth_box(page.of_kind("illustration")[0])) > 0.8
    assert image.image_path == "" and image.detection == "photo" and image.type == BlockType.ILLUSTRATION
    captions = [b for b in _texts(result) if b.type == BlockType.CAPTION]
    assert len(captions) == 1 and captions[0].text.startswith("Figure 1")
    assert image.caption_id == captions[0].id
    for b in _texts(result):
        assert overlap_ratio(b.box, image.box) < 0.5, b.text
    assert _match(result, page.of_kind("title")[0]).type == BlockType.TITLE
    order = [b.type for b in result.blocks]
    assert order.index(BlockType.ILLUSTRATION) < order.index(BlockType.CAPTION)


@requires_tesseract
def test_boxes_cover_the_ground_truth(pages):
    for name, (page, result) in pages.items():
        for drawn in page.blocks:
            if drawn.kind == "illustration":
                continue
            assert iou(_match(result, drawn).box, _truth_box(drawn)) > 0.6, (name, drawn.text)


@requires_tesseract
def test_font_sizes_within_15_percent(pages):
    for name, (page, result) in pages.items():
        for drawn in page.blocks:
            if drawn.kind == "illustration":
                continue
            expected_pt = drawn.size_px * PAGE_HEIGHT_PT / H
            got = _match(result, drawn).font_size
            assert abs(got - expected_pt) <= 0.15 * expected_pt, (name, drawn.kind, got, expected_pt)


@requires_tesseract
def test_title_style_bold_centered(pages):
    for name, (page, result) in pages.items():
        title = _match(result, page.of_kind("title")[0])
        assert title.font_weight == "bold", name
        assert title.alignment.value == "center", name
        assert title.color == "#000000"
        for drawn in page.of_kind("paragraph"):
            body = _match(result, drawn)
            assert body.font_weight == "normal" and body.alignment.value == "left", (name, body.text)
            assert body.line_spacing == pytest.approx(1.35, abs=0.1)
            assert body.font_family == "Serif"
            assert "size_px" in body.estimated and "stroke" in body.estimated


@requires_tesseract
def test_lines_and_words_are_normalized_and_inside_their_block(pages):
    tol = 0.005
    for name, (page, result) in pages.items():
        for b in _texts(result):
            assert b.lines
            for line in b.lines:
                values = [line.x, line.y, line.width, line.height]
                values += [v for w in line.words for v in (w.x, w.y, w.width, w.height)]
                assert all(0.0 <= v <= 1.0 for v in values), (name, b.text)
                assert line.x >= b.x - tol and line.y >= b.y - tol
                assert line.x + line.width <= b.x + b.width + tol and line.y + line.height <= b.y + b.height + tol
                for w in line.words:
                    assert w.x >= line.x - tol and w.x + w.width <= line.x + line.width + tol
                    assert w.confidence >= 0


@requires_tesseract
def test_coloured_ink_is_kept():
    image = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(image)
    synthetic.draw_paragraph(draw, synthetic.FR_PARAGRAPH, 120, 200, W - 240, 30, color=(20, 40, 170))
    synthetic.draw_paragraph(draw, synthetic.EN_PARAGRAPH, 120, 500, W - 240, 30, color=(25, 25, 25))
    texts = _texts(analyse(np.array(image)))
    assert len(texts) == 2
    blue, black = texts
    r, g, b = (int(blue.color[i:i + 2], 16) for i in (1, 3, 5))
    assert b > r + 60 and b > g + 60
    assert black.color == "#000000"


@requires_tesseract
def test_table_text_becomes_a_table_block():
    image, truth = ruled_table()
    result = analyse(image)
    tables = [b for b in result.blocks if b.type == BlockType.TABLE]
    assert len(tables) == 1 and isinstance(tables[0], TextBlock)
    table = tables[0]
    assert table.keep_line_breaks and table.needs_review
    assert table.text.split("\n")[0].split("\t") == ["Cellule 00", "Cellule 01", "Cellule 02"]
    assert len(table.text.split("\n")) == 4
    assert not [b for b in result.blocks if isinstance(b, ImageBlock)]


def test_empty_ruled_table_becomes_an_image_block():
    image = Image.new("RGB", (W, H), "white")
    draw = ImageDraw.Draw(image)
    for i in range(5):
        draw.line((150, 500 + i * 60, 1090, 500 + i * 60), fill="black", width=3)
    for j in range(4):
        draw.line((150 + j * 313, 500, 150 + j * 313, 740), fill="black", width=3)
    frame = np.array(image)
    result = la.analyse_layout(OCRResult([], W, H, ["fra"]), frame, detect_illustrations(frame), PAGE_HEIGHT_PT,
                               SETTINGS)
    assert len(result.blocks) == 1
    block = result.blocks[0]
    assert isinstance(block, ImageBlock) and block.type == BlockType.TABLE and block.detection == "table"


@requires_tesseract
def test_degraded_text_needs_review():
    page = synthetic.simple_page(size_px=20)
    gray = cv2.cvtColor(page.image, cv2.COLOR_RGB2GRAY)
    small = cv2.resize(gray, None, fx=0.25, fy=0.25, interpolation=cv2.INTER_AREA)
    degraded = cv2.resize(small, (W, H), interpolation=cv2.INTER_LINEAR)
    result = analyse(page.image, degraded)
    flagged = [b for b in _texts(result) if b.needs_review]
    assert flagged
    assert all("confiance" in b.review_reason for b in flagged)
    assert any(b.low_confidence_words for b in flagged)
    assert any("faible" in w or "vérifier" in w for w in result.warnings)
