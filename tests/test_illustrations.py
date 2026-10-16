"""Illustration detection (photos, drawings, ruled tables) and lossless cropping."""
import numpy as np
from PIL import Image, ImageDraw

from core.coordinates import iou
from core.document_models import Box
from core.illustration_extraction import crop_region, detect_illustrations
from tests import synthetic

W, H = synthetic.PAGE_W, synthetic.PAGE_H


def _box(px) -> Box:
    l, t, r, b = px
    return Box(x=l / W, y=t / H, width=(r - l) / W, height=(b - t) / H)


def _blank():
    image = Image.new("RGB", (W, H), "white")
    return image, ImageDraw.Draw(image)


def ruled_table() -> tuple[np.ndarray, tuple[int, int, int, int]]:
    image, draw = _blank()
    for i in range(5):
        draw.line((150, 500 + i * 60, 1090, 500 + i * 60), fill="black", width=3)
    for j in range(4):
        draw.line((150 + j * 313, 500, 150 + j * 313, 740), fill="black", width=3)
    for i in range(4):
        for j in range(3):
            draw.text((170 + j * 313, 515 + i * 60), f"Cellule {i}{j}", font=synthetic.font(28), fill="black")
    return np.array(image), (150, 500, 1092, 742)


def test_photo_found_with_its_exact_box():
    page = synthetic.illustrated_page()
    regions = detect_illustrations(page.image)
    assert len(regions) == 1
    region = regions[0]
    assert region.kind == "photo" and not region.uncertain and region.confidence > 70
    truth = _box(page.of_kind("illustration")[0].box)
    assert iou(region.box, truth) > 0.8
    # the small margin never reaches the caption below
    caption_top = page.of_kind("caption")[0].box[1] / H
    assert region.box.y2 <= caption_top
    assert region.box.x <= truth.x and region.box.y2 >= truth.y2 - 1e-3


def test_dark_photo_is_still_found():
    page = synthetic.illustrated_page(dark=True)
    regions = detect_illustrations(page.image)
    assert len(regions) == 1 and regions[0].kind == "photo"
    assert iou(regions[0].box, _box(page.of_kind("illustration")[0].box)) > 0.8


def test_text_pages_have_no_illustration():
    for page in (synthetic.simple_page(), synthetic.two_column_page()):
        assert detect_illustrations(page.image) == []
    assert detect_illustrations(np.full((H, W, 3), 255, np.uint8)) == []


def test_rule_and_bold_text_block_are_not_photos():
    image, draw = _blank()
    draw.rectangle((100, 900, 1140, 904), fill="black")           # thin rule
    draw.rectangle((100, 1000, 1140, 1012), fill=(30, 30, 30))      # thicker rule
    synthetic.draw_paragraph(draw, synthetic.FR_PARAGRAPH * 3, 100, 150, 1040, 40, "serif-bold")
    assert detect_illustrations(np.array(image)) == []


def test_ruled_table_is_a_table():
    image, truth = ruled_table()
    regions = detect_illustrations(image)
    assert [r.kind for r in regions] == ["table"]
    assert iou(regions[0].box, _box(truth)) > 0.8


def test_line_drawing_is_a_drawing():
    image, draw = _blank()
    draw.ellipse((300, 400, 900, 900), outline="black", width=3)
    draw.line((300, 650, 900, 650), fill="black", width=2)
    draw.arc((350, 450, 850, 850), 0, 180, fill="black", width=2)
    regions = detect_illustrations(np.array(image))
    assert [r.kind for r in regions] == ["drawing"]
    assert iou(regions[0].box, _box((300, 400, 901, 901))) > 0.8


def test_readable_text_over_a_picture_makes_it_uncertain():
    page = synthetic.illustrated_page()
    l, t, r, b = page.of_kind("illustration")[0].box
    regions = detect_illustrations(page.image, text_boxes_px=[(l + 100, t + 100, l + 400, t + 150)])
    assert regions[0].uncertain and "texte" in regions[0].reason


def test_fuzzy_edges_make_it_uncertain():
    page = np.full((H, W, 3), 255, np.uint8)
    picture = synthetic.illustration(700, 500).astype(np.float32)
    yy, xx = np.mgrid[0:500, 0:700]
    alpha = np.clip(np.minimum.reduce([xx, yy, 699 - xx, 499 - yy]) / 120.0, 0, 1)[..., None]
    page[400:900, 270:970] = (picture * alpha + 255 * (1 - alpha)).astype(np.uint8)
    regions = detect_illustrations(page)
    assert len(regions) == 1 and regions[0].uncertain and "flous" in regions[0].reason


def test_crop_region_is_lossless_full_resolution():
    page = synthetic.illustrated_page()
    l, t, r, b = page.of_kind("illustration")[0].box
    crop = crop_region(page.image, _box((l, t, r, b)))
    assert crop.shape == (b - t, r - l, 3) and crop.dtype == np.uint8
    assert np.array_equal(crop, page.image[t:b, l:r])
    assert np.array_equal(crop, synthetic.illustration(r - l, b - t))
    crop[:] = 0                                                   # a copy, not a view
    assert page.image[t:b, l:r].any()
    padded = crop_region(page.image, _box((l, t, r, b)), padding=0.01)
    assert abs(padded.shape[0] - (b - t) - 2 * 0.01 * H) <= 2
    assert abs(padded.shape[1] - (r - l) - 2 * 0.01 * W) <= 2
