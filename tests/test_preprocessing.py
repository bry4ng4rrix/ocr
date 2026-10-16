"""Page frame geometry and mode rendering (core/image_preprocessing.py)."""
import cv2
import numpy as np
import pytest

from core.coordinates import norm_to_px, rotate_box_quarter
from core.document_models import (Box, GeometrySettings, ImageAdjustments, LocalAdjustment, AdjustmentTarget,
                                  ProcessingMode)
from core.image_preprocessing import (FrameResult, apply_quad, binarize_sauvola, build_frame, crop_margins_box,
                                      detect_page_quad, downscale, estimate_skew, load_oriented,
                                      render_ocr_image, render_print_image, suggest_mode)
from tests.synthetic import (illustrated_page, illustration, photographed, rotate_page, save, simple_page,
                             two_column_page)

NO_AUTO = dict(auto_crop=False, auto_deskew=False, auto_orientation=False)


@pytest.fixture(scope="module")
def page():
    return simple_page()


@pytest.fixture(scope="module")
def ill():
    return illustrated_page()


def expected_quad(page_img, margin=0.12, warp=0.04):
    """Corners used by synthetic.photographed, as 0..1 of the photo."""
    h, w = page_img.shape[:2]
    out_w, out_h = int(w * (1 + 2 * margin)), int(h * (1 + 2 * margin))
    mx, my = w * margin, h * margin
    dst = np.float32([[mx + w * warp, my], [mx + w - w * warp * 0.5, my + h * warp * 0.3],
                      [mx + w, my + h], [mx, my + h - h * warp * 0.2]])
    return dst / np.float32([out_w, out_h])


def to_box(px, w, h):
    l, t, r, b = px
    return Box(x=l / w, y=t / h, width=(r - l) / w, height=(b - t) / h)


# --------------------------------------------------------------------------- detection

def test_detect_page_quad_on_photo(page):
    photo = photographed(page.image)
    quad = detect_page_quad(photo)
    assert quad is not None and len(quad) == 4
    assert np.abs(np.array(quad) - expected_quad(page.image)).max() < 0.02


def test_detect_page_quad_none_on_full_scan(page, ill):
    assert detect_page_quad(page.image) is None
    assert detect_page_quad(ill.image) is None
    assert detect_page_quad(rotate_page(page.image, 4.0)) is None


@pytest.mark.parametrize("angle", [3.0, -3.0, 1.5, -6.0])
def test_estimate_skew(page, angle):
    assert estimate_skew(rotate_page(page.image, angle)) == pytest.approx(angle, abs=0.3)


def test_estimate_skew_straight_and_uncertain(page):
    assert abs(estimate_skew(page.image)) < 0.2
    assert abs(estimate_skew(two_column_page().image)) < 0.2
    blank = np.full((800, 600, 3), 255, np.uint8)
    assert estimate_skew(blank) == 0.0
    assert estimate_skew(cv2.cvtColor(rotate_page(page.image, 3.0), cv2.COLOR_RGB2GRAY)) == pytest.approx(3.0, abs=0.3)


# --------------------------------------------------------------------------- frame

def test_build_frame_straightens_skewed_scan(page):
    skewed = rotate_page(page.image, 3.0)
    result = build_frame(skewed, GeometrySettings())
    assert isinstance(result, FrameResult)
    assert result.geometry.detected_skew == pytest.approx(3.0, abs=0.3)
    assert result.geometry.detected_quad is None            # a scan: no sheet cut-out invented
    assert abs(estimate_skew(result.image)) < 0.3
    assert result.image.shape == skewed.shape
    corners = np.concatenate([result.image[:20, :20].reshape(-1, 3), result.image[-20:, -20:].reshape(-1, 3)])
    assert corners.min() > 200                                # no black border after the rotation


def test_build_frame_cuts_out_photographed_page(page):
    photo = photographed(page.image)
    result = build_frame(photo, GeometrySettings())
    h, w = page.image.shape[:2]
    fh, fw = result.image.shape[:2]
    assert abs(fh - h) / h < 0.04 and abs(fw - w) / w < 0.04
    assert result.geometry.detected_quad is not None
    assert abs(estimate_skew(result.image)) < 0.3
    # the dark stand is gone: the frame border is paper
    border = np.concatenate([result.image[5, 50:-50], result.image[-6, 50:-50]])
    assert cv2.cvtColor(border[None], cv2.COLOR_RGB2GRAY).mean() > 150


def test_build_frame_is_reproducible(page):
    photo = photographed(rotate_page(page.image, 2.0))
    first = build_frame(photo, GeometrySettings())
    assert first.geometry.detected_quad is not None and first.geometry.detected_skew is not None
    # reload the geometry as a saved project would (JSON round trip), then rebuild without detection
    stored = GeometrySettings.model_validate_json(first.geometry.model_dump_json())
    calls = []
    second = build_frame(photo, stored, detect=False, orientation_detector=lambda img: calls.append(1) or 90)
    assert not calls
    assert np.array_equal(first.image, second.image)
    assert second.geometry == stored
    assert stored.signature() == first.geometry.signature()


@pytest.mark.parametrize("turn", [90, 180, 270])
def test_quarter_turns(ill, turn):
    img = ill.image
    h, w = img.shape[:2]
    result = build_frame(img, GeometrySettings(rotation=turn, **NO_AUTO))
    out = result.image
    assert out.shape == ((w, h, 3) if turn in (90, 270) else (h, w, 3))
    oh, ow = out.shape[:2]
    pic = to_box(ill.of_kind("illustration")[0].box, w, h)
    turned = rotate_box_quarter(pic, turn)
    src = img[slice(*norm_to_px(pic, w, h)[1::2]), slice(*norm_to_px(pic, w, h)[0::2])]
    l, t, r, b = norm_to_px(turned, ow, oh)
    assert np.array_equal(out[t:b, l:r], np.rot90(src, k=-turn // 90))


def test_orientation_detector(ill):
    img = ill.image
    seen = []

    def detector(turned):
        seen.append(turned.shape)
        return 180

    result = build_frame(img, GeometrySettings(rotation=90, auto_crop=False, auto_deskew=False), orientation_detector=detector)
    h, w = img.shape[:2]
    assert seen == [(w, h, 3)]                        # called on the image turned by the user rotation
    assert result.geometry.detected_orientation == 180
    assert result.geometry.effective_rotation() == 270
    assert np.array_equal(result.image, np.rot90(img, k=-3))
    # (turn, confidence) tuples are accepted, failures become a warning
    assert build_frame(img, GeometrySettings(**{**NO_AUTO, "auto_orientation": True}),
                       orientation_detector=lambda i: (90, 12.0)).geometry.detected_orientation == 90

    def broken(_):
        raise RuntimeError("OSD absent")
    res = build_frame(img, GeometrySettings(**{**NO_AUTO, "auto_orientation": True}), orientation_detector=broken)
    assert res.warnings and np.array_equal(res.image, img)


def test_manual_quad_and_manual_skew(page):
    photo = photographed(page.image)
    quad = [tuple(map(float, p)) for p in expected_quad(page.image)]
    result = build_frame(photo, GeometrySettings(manual_quad=quad, auto_deskew=False, auto_orientation=False))
    h, w = page.image.shape[:2]
    assert abs(result.image.shape[0] - h) / h < 0.04
    assert result.geometry.detected_quad is None          # nothing detected when a manual quad is set
    skewed = rotate_page(page.image, 2.0)
    manual = build_frame(skewed, GeometrySettings(deskew_angle=2.0, auto_crop=False, auto_orientation=False))
    assert manual.geometry.detected_skew is None
    assert abs(estimate_skew(manual.image)) < 0.3
    assert apply_quad(photo, [(0, 0), (0.01, 0), (0.01, 0.01), (0, 0.01)]) is None


def test_trim_and_scale(page):
    img = page.image
    h, w = img.shape[:2]
    res = build_frame(img, GeometrySettings(trim_left=10, trim_right=5, trim_top=20, scale_percent=50, **NO_AUTO))
    exp_w, exp_h = w - round(w * 0.10) - round(w * 0.05), h - round(h * 0.20)
    assert res.image.shape[:2] == (round(exp_h * 0.5), round(exp_w * 0.5))
    res = build_frame(img, GeometrySettings(trim_left=40, trim_right=40, trim_top=40, trim_bottom=40, **NO_AUTO))
    assert res.image.shape[:2] == (h - 2 * round(h * 0.4), w - 2 * round(w * 0.4))


def test_auto_trim_keeps_every_ink_pixel(ill):
    img = ill.image
    ink_before = int((cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) < 200).sum())
    res = build_frame(img, GeometrySettings(auto_trim_margins=True, **NO_AUTO))
    out = res.image
    assert out.shape[0] < img.shape[0] and out.shape[1] < img.shape[1]       # margins were cut
    assert int((cv2.cvtColor(out, cv2.COLOR_RGB2GRAY) < 200).sum()) == ink_before
    # a nearly empty page is not trimmed
    sparse = np.full((1000, 800, 3), 255, np.uint8)
    sparse[480:500, 380:420] = 0
    assert crop_margins_box(sparse) is None
    assert build_frame(sparse, GeometrySettings(auto_trim_margins=True, **NO_AUTO)).image.shape == sparse.shape


def test_build_frame_never_modifies_original(page):
    photo = photographed(rotate_page(page.image, 2.0))
    before = photo.copy()
    res = build_frame(photo, GeometrySettings(rotation=90, trim_left=5, auto_trim_margins=True, scale_percent=80))
    assert np.array_equal(photo, before)
    neutral = build_frame(photo, GeometrySettings(**NO_AUTO))
    assert np.array_equal(neutral.image, photo) and not np.shares_memory(neutral.image, photo)
    assert not np.shares_memory(res.image, photo)


def test_load_oriented(tmp_path, page):
    path = save(page.image, tmp_path / "page.png")
    assert np.array_equal(load_oriented(path), page.image)


def test_downscale(page):
    small, factor = downscale(page.image, 500)
    assert max(small.shape[:2]) == 500 and factor == pytest.approx(500 / 1754)
    same, one = downscale(page.image, 5000)
    assert same is page.image and one == 1.0


def test_binarize_sauvola(page):
    gray = cv2.cvtColor(page.image, cv2.COLOR_RGB2GRAY)
    shade = np.linspace(0.6, 1.0, gray.shape[1], dtype=np.float32)[None, :]
    shaded = (gray * shade).astype(np.uint8)
    bw = binarize_sauvola(shaded)
    assert bw.shape == gray.shape and set(np.unique(bw)) <= {0, 255}
    paper = gray > 250
    assert (bw[paper] == 255).mean() > 0.99            # the shaded paper is white
    assert (bw[gray < 60] == 0).mean() > 0.95          # the ink is black


# --------------------------------------------------------------------------- modes

def test_print_image_text_mode_is_untouched(ill):
    out = render_print_image(ill.image, ImageAdjustments(), [], ProcessingMode.TEXT)
    assert np.array_equal(out, ill.image)


def test_mixed_mode_keeps_colours_and_cleans_text(ill):
    img = ill.image
    h, w = img.shape[:2]
    # yellowed, slightly dark paper
    aged = np.clip(img.astype(np.float32) * np.array([0.92, 0.88, 0.78], np.float32), 0, 255).astype(np.uint8)
    text_boxes = [to_box(b.box, w, h) for b in ill.of_kind("paragraph") + ill.of_kind("title")]
    out = render_print_image(aged, ImageAdjustments(), [], ProcessingMode.MIXED, text_boxes=text_boxes)
    l, t, r, b = ill.of_kind("illustration")[0].box
    assert np.array_equal(out[t:b, l:r], aged[t:b, l:r])                       # colours kept
    assert len(np.unique(out[t:b, l:r].reshape(-1, 3), axis=0)) > 100           # never binarized
    para = ill.of_kind("paragraph")[0].box
    region_in = aged[para[1]:para[3], para[0]:para[2]]
    region_out = out[para[1]:para[3], para[0]:para[2]]
    paper = cv2.cvtColor(img[para[1]:para[3], para[0]:para[2]], cv2.COLOR_RGB2GRAY) > 250
    assert region_out[paper].mean() > 252 > region_in[paper].mean() + 20       # paper whitened
    ink = cv2.cvtColor(img[para[1]:para[3], para[0]:para[2]], cv2.COLOR_RGB2GRAY) < 60
    assert region_out[ink].mean() <= region_in[ink].mean()                      # ink not lighter
    # with a colour treatment asked by the user, the picture follows it (still not binarized)
    vivid = render_print_image(aged, ImageAdjustments(saturation=40), [], ProcessingMode.MIXED, text_boxes=text_boxes)
    assert len(np.unique(vivid[t:b, l:r].reshape(-1, 3), axis=0)) > 100


def test_print_image_applies_print_locals_only(ill):
    img = ill.image
    box = Box(x=0.1, y=0.1, width=0.3, height=0.1)
    locs = [LocalAdjustment(box=box, adjustments=ImageAdjustments(brightness=-60), target=AdjustmentTarget.OCR)]
    assert np.array_equal(render_print_image(img, ImageAdjustments(), locs, ProcessingMode.ILLUSTRATION), img)
    locs[0] = LocalAdjustment(box=box, adjustments=ImageAdjustments(brightness=-60))
    assert not np.array_equal(render_print_image(img, ImageAdjustments(), locs, ProcessingMode.ILLUSTRATION), img)


def test_ocr_image(ill):
    img = ill.image
    h, w = img.shape[:2]
    pic = ill.of_kind("illustration")[0].box
    boxes = [to_box(pic, w, h)]
    before = img.copy()
    for mode in (ProcessingMode.MIXED, ProcessingMode.ILLUSTRATION):
        gray = render_ocr_image(img, ImageAdjustments(), [], mode, illustration_boxes=boxes)
        assert gray.ndim == 2 and gray.dtype == np.uint8 and gray.shape == (h, w)
        l, t, r, b = norm_to_px(boxes[0], w, h)
        assert (gray[t:b, l:r] == 255).all()
    text = render_ocr_image(img, ImageAdjustments(), [], ProcessingMode.TEXT, illustration_boxes=boxes)
    assert text[pic[1]:pic[3], pic[0]:pic[2]].mean() < 200       # pictures kept in text mode
    para = ill.of_kind("paragraph")[0].box
    assert text[para[1]:para[3], para[0]:para[2]].min() < 60      # the text is still there
    assert np.array_equal(img, before)


def test_ocr_image_flattens_lighting(page):
    img = page.image
    shade = np.linspace(0.6, 1.0, img.shape[1], dtype=np.float32)[None, :, None]
    shaded = (img.astype(np.float32) * shade).astype(np.uint8)
    gray = render_ocr_image(shaded, ImageAdjustments(), [], ProcessingMode.TEXT)
    paper = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY) > 250
    left = paper.copy()
    left[:, 300:] = False
    assert gray[left].mean() > 235


def test_suggest_mode(page, ill):
    assert suggest_mode(page.image) == ProcessingMode.TEXT
    assert suggest_mode(two_column_page().image) == ProcessingMode.TEXT
    assert suggest_mode(ill.image) == ProcessingMode.MIXED
    assert suggest_mode(illustrated_page(dark=True).image) == ProcessingMode.MIXED
    assert suggest_mode(illustration(1240, 1754)) == ProcessingMode.ILLUSTRATION
