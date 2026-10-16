import pytest
from pydantic import ValidationError

from core.coordinates import (font_scale, norm_to_pdf_rect, norm_to_px, pdf_rect_to_norm, rotate_box_quarter,
                              sheet_geometry)
from core.document_models import (SCHEMA_VERSION, Box, GeometrySettings, ImageAdjustments, ImageBlock, Page,
                                  PageSize, Project, RenderSettings, TextBlock)


def make_project() -> Project:
    page = Page(original_path="originals/p1.png", frame_width=1000, frame_height=1500, font_reference_height_pt=360)
    page.blocks.append(TextBlock(x=0.1, y=0.1, width=0.8, height=0.1, text="Élise — « œuvre »", ocr_text="Elise"))
    page.blocks.append(ImageBlock(x=0.2, y=0.4, width=0.5, height=0.3, image_path="illustrations/a.png",
                                  adjustments=ImageAdjustments(saturation=20)))
    return Project(name="Test", pages=[page])


def test_json_roundtrip_keeps_blocks_types_and_unicode():
    project = make_project()
    again = Project.from_json(project.to_json())
    assert again == project
    assert isinstance(again.pages[0].blocks[0], TextBlock)
    assert isinstance(again.pages[0].blocks[1], ImageBlock)
    assert again.pages[0].blocks[0].text == "Élise — « œuvre »"
    assert again.pages[0].blocks[0].is_corrected
    assert again.schema_version == SCHEMA_VERSION


def test_newer_schema_and_unknown_fields_are_refused():
    raw = make_project().model_dump(mode="json")
    raw["schema_version"] = SCHEMA_VERSION + 1
    with pytest.raises(ValidationError):
        Project.model_validate(raw)
    raw = make_project().model_dump(mode="json")
    raw["pages"][0]["blocks"][0]["evil"] = 1
    with pytest.raises(ValidationError):
        Project.model_validate(raw)


def test_blocks_must_stay_inside_the_frame_and_colors_are_checked():
    with pytest.raises(ValidationError):
        TextBlock(x=0.9, y=0.1, width=0.2, height=0.1)
    with pytest.raises(ValidationError):
        TextBlock(x=0.1, y=0.1, width=0.2, height=0.1, color="red")
    block = TextBlock(x=0.1, y=0.1, width=0.2, height=0.1)
    with pytest.raises(ValidationError):
        block.set_box(Box.model_construct(x=0.95, y=0.1, width=0.2, height=0.1))
    assert block.x == 0.1
    block.set_box(Box.clamped(0.95, 0.1, 0.2, 0.1))
    assert block.x + block.width <= 1.0


def test_neutral_adjustments():
    assert ImageAdjustments().is_neutral()
    assert not ImageAdjustments(contrast=10).is_neutral()
    assert ImageAdjustments(contrast=10, enabled=False).is_neutral()


def test_geometry_signature_ignores_photometry_and_scale_but_not_geometry():
    g = GeometrySettings()
    base = g.signature()
    assert GeometrySettings(scale_percent=50).signature() == base
    assert GeometrySettings(rotation=90).signature() != base
    assert GeometrySettings(deskew_angle=1.5).signature() != base
    page = Page(original_path="x.png", ocr_geometry_signature=base)
    assert not page.ocr_outdated
    page.print_adjustments = ImageAdjustments(contrast=30)
    assert not page.ocr_outdated
    page.geometry = GeometrySettings(trim_left=5)
    assert page.ocr_outdated


def test_pixel_and_pdf_conversions_round_trip():
    box = Box(x=0.1, y=0.2, width=0.3, height=0.4)
    assert norm_to_px(box, 1000, 2000) == (100, 400, 400, 1200)
    render = RenderSettings(page_size=PageSize.AUTO, auto_dpi=300)
    sheet = sheet_geometry(render, 2480, 3508)
    assert sheet.page_width == pytest.approx(595.2, abs=0.1)
    x, y, w, h = norm_to_pdf_rect(box, sheet)
    # Top of the box is 20 % down from the top: in PDF (bottom-left origin) y + h = 80 % of the height.
    assert y + h == pytest.approx(0.8 * sheet.page_height)
    back = pdf_rect_to_norm(x, y, w, h, sheet)
    for a, b in zip((back.x, back.y, back.width, back.height), (0.1, 0.2, 0.3, 0.4)):
        assert a == pytest.approx(b)


def test_fixed_format_keeps_proportions_and_turns_landscape():
    render = RenderSettings(page_size=PageSize.A4, margin_mm=10)
    sheet = sheet_geometry(render, 1000, 1500)
    assert sheet.content_width / sheet.content_height == pytest.approx(1000 / 1500)
    assert sheet.page_height > sheet.page_width
    landscape = sheet_geometry(render, 1500, 1000)
    assert landscape.page_width > landscape.page_height
    page = Page(original_path="x.png", frame_width=1000, frame_height=1500, font_reference_height_pt=sheet.content_height / 2)
    assert font_scale(render, page, sheet) == pytest.approx(2.0)


@pytest.mark.parametrize("turns", [90, 180, 270])
def test_quarter_turn_matches_pixel_rotation(turns):
    import numpy as np
    w, h = 40, 60
    img = np.zeros((h, w), np.uint8)
    box = Box(x=0.1, y=0.2, width=0.25, height=0.3)
    l, t, r, b = norm_to_px(box, w, h)
    img[t:b, l:r] = 255
    rotated = np.rot90(img, k=-(turns // 90))     # clockwise
    turned = rotate_box_quarter(box, turns)
    rh, rw = rotated.shape
    ys, xs = np.nonzero(rotated)
    assert turned.x == pytest.approx(xs.min() / rw, abs=0.03)
    assert turned.y == pytest.approx(ys.min() / rh, abs=0.03)
    assert turned.width == pytest.approx((xs.max() + 1 - xs.min()) / rw, abs=0.03)
    assert turned.height == pytest.approx((ys.max() + 1 - ys.min()) / rh, abs=0.03)
