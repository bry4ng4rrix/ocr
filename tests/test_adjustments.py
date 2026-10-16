"""Photometric adjustments and local corrections (core/image_adjustments.py)."""
import tracemalloc

import cv2
import numpy as np
import pytest

from core.coordinates import norm_to_px
from core.document_models import (AdjustmentTarget, Binarization, Box, ImageAdjustments, LocalAdjustment,
                                  WhiteBalance)
from core.image_adjustments import (apply_adjustments, apply_local_adjustments, estimate_white_balance_gains,
                                    histogram, illumination_gain)
from tests.synthetic import color_cast, illustrated_page, simple_page


@pytest.fixture(scope="module")
def page():
    return illustrated_page()


@pytest.fixture(scope="module")
def dark_page():
    return illustrated_page(dark=True)


def region(img, box):
    l, t, r, b = box
    return img[t:b, l:r]


def pic_box(p):
    return p.of_kind("illustration")[0].box


def luma(img):
    return cv2.cvtColor(img, cv2.COLOR_RGB2GRAY).astype(np.float64)


def chroma(img):
    return float((img.max(axis=2).astype(int) - img.min(axis=2)).mean())


def paper_mask(img):
    return luma(img) >= 250


# --------------------------------------------------------------------------- neutral

def test_neutral_returns_identical_image(page):
    img = page.image
    out = apply_adjustments(img, ImageAdjustments())
    assert np.array_equal(out, img)
    assert out.dtype == np.uint8 and out.shape == img.shape


def test_disabled_set_is_neutral(page):
    img = page.image
    adj = ImageAdjustments(enabled=False, exposure=2, saturation=-100, grayscale=True,
                           binarization=Binarization.OTSU, illumination_correction=True)
    assert np.array_equal(apply_adjustments(img, adj), img)


def test_only_radius_changed_is_identity(page):
    # sharpen_radius alone does nothing while sharpness is 0
    assert np.array_equal(apply_adjustments(page.image, ImageAdjustments(sharpen_radius=3)), page.image)


def test_input_never_modified(page):
    img = page.image.copy()
    before = img.copy()
    apply_adjustments(img, ImageAdjustments(exposure=1, shadows=50, saturation=40, sharpness=100,
                                            illumination_correction=True, local_contrast=2))
    loc = LocalAdjustment(box=Box(x=0.2, y=0.2, width=0.4, height=0.4), adjustments=ImageAdjustments(brightness=40))
    apply_local_adjustments(img, [loc], AdjustmentTarget.PRINT)
    assert np.array_equal(img, before)


# --------------------------------------------------------------------------- each slider, right direction

def test_brightness(page):
    img = page.image
    pic = region(img, pic_box(page))
    up = region(apply_adjustments(img, ImageAdjustments(brightness=40)), pic_box(page))
    down = region(apply_adjustments(img, ImageAdjustments(brightness=-40)), pic_box(page))
    assert luma(up).mean() > luma(pic).mean() + 10
    assert luma(down).mean() < luma(pic).mean() - 10


def test_contrast(page):
    pic = region(page.image, pic_box(page))
    more = apply_adjustments(pic.copy(), ImageAdjustments(contrast=50))
    less = apply_adjustments(pic.copy(), ImageAdjustments(contrast=-50))
    assert luma(more).std() > luma(pic).std() * 1.2
    assert luma(less).std() < luma(pic).std() * 0.8


def test_gamma_brightens_mid_tones(page):
    pic = region(page.image, pic_box(page))
    out = apply_adjustments(pic.copy(), ImageAdjustments(gamma=2.0))
    assert luma(out).mean() > luma(pic).mean() + 10
    darker = apply_adjustments(pic.copy(), ImageAdjustments(gamma=0.5))
    assert luma(darker).mean() < luma(pic).mean() - 10


def test_exposure(page):
    img = page.image
    box = pic_box(page)
    up = apply_adjustments(img, ImageAdjustments(exposure=1.0))
    down = apply_adjustments(img, ImageAdjustments(exposure=-1.0))
    assert luma(region(up, box)).mean() > luma(region(img, box)).mean() + 10
    assert luma(region(down, box)).mean() < luma(region(img, box)).mean() - 10
    paper = paper_mask(img)
    assert up[paper].min() >= 254          # white paper stays white, nothing wraps around


def test_saturation(page):
    pic = region(page.image, pic_box(page))
    grey = apply_adjustments(pic.copy(), ImageAdjustments(saturation=-100))
    assert int((grey.max(axis=2).astype(int) - grey.min(axis=2)).max()) <= 1
    # lightness kept (luma preserved by the YCbCr matrix)
    assert abs(luma(grey).mean() - luma(pic).mean()) < 1.5
    vivid = apply_adjustments(pic.copy(), ImageAdjustments(saturation=60))
    assert chroma(vivid) > chroma(pic) * 1.2


def test_hue_turns_colours_keeps_lightness(page):
    pic = region(page.image, pic_box(page))
    out = apply_adjustments(pic.copy(), ImageAdjustments(hue=180))
    hue_in = cv2.cvtColor(pic, cv2.COLOR_RGB2HSV)[..., 0].astype(int)
    hue_out = cv2.cvtColor(out, cv2.COLOR_RGB2HSV)[..., 0].astype(int)
    diff = np.abs(hue_in - hue_out)
    diff = np.minimum(diff, 180 - diff)                 # OpenCV hue: 0..179
    assert np.median(diff) > 45
    assert abs(luma(out).mean() - luma(pic).mean()) < 6


def test_temperature(page):
    img = page.image
    warm = apply_adjustments(img, ImageAdjustments(temperature=50)).astype(float)
    cool = apply_adjustments(img, ImageAdjustments(temperature=-50)).astype(float)
    base = img.astype(float)
    rb = lambda a: (a[..., 0] - a[..., 2]).mean()
    assert rb(warm) > rb(base) + 5
    assert rb(cool) < rb(base) - 5


def test_tint(page):
    pic = region(page.image, pic_box(page)).copy()
    g = lambda a: (a[..., 1].astype(float) - (a[..., 0].astype(float) + a[..., 2]) / 2).mean()
    assert g(apply_adjustments(pic, ImageAdjustments(tint=60))) < g(pic) - 3     # magenta
    assert g(apply_adjustments(pic, ImageAdjustments(tint=-60))) > g(pic) + 3    # green


def test_white_balance_removes_cast():
    cast = color_cast(simple_page().image)
    paper_in = cast[luma(cast) > 200].mean(axis=0)
    assert paper_in[0] - paper_in[2] > 50                    # clearly yellow before
    for mode in (WhiteBalance.WHITE_PATCH, WhiteBalance.GRAY_WORLD):
        out = apply_adjustments(cast, ImageAdjustments(white_balance=mode))
        paper = out[luma(out) > 200].mean(axis=0)
        assert paper.max() - paper.min() < 4, (mode, paper)
    gains = estimate_white_balance_gains(cast, WhiteBalance.WHITE_PATCH)
    assert gains[0] == pytest.approx(1.0) and gains[2] > gains[1] > 1.0
    assert estimate_white_balance_gains(cast, WhiteBalance.NONE) == (1.0, 1.0, 1.0)
    out = apply_adjustments(cast, ImageAdjustments(white_balance=WhiteBalance.WHITE_PATCH))
    assert out[paper_mask(out) | (luma(cast) > 200)].mean() > 250   # the paper is white again


def test_shadows_and_exposure_brighten_dark_illustration_without_clipping_paper(dark_page):
    img = dark_page.image
    box = pic_box(dark_page)
    paper = paper_mask(img)
    before = luma(region(img, box)).mean()
    for adj in (ImageAdjustments(shadows=80), ImageAdjustments(exposure=1.5)):
        out = apply_adjustments(img, adj)
        pic = region(out, box)
        assert luma(pic).mean() > before + 12, adj
        assert pic.max() < 255                         # the picture itself is not blown out
        assert out[luma(img) == 255].min() == 255       # white paper stays exactly white
        assert (luma(out)[paper] >= luma(img)[paper] - 1).all()   # nothing near white is darkened
        assert chroma(region(out, box)) >= chroma(region(img, box))   # colours not washed out


def test_highlights_recover_bright_tones(page):
    pic = region(page.image, pic_box(page))
    out = apply_adjustments(pic.copy(), ImageAdjustments(highlights=-80))
    bright = (luma(pic) > 150) & (luma(pic) < 245)
    assert bright.sum() > 1000
    assert luma(out)[bright].mean() < luma(pic)[bright].mean() - 3
    up = apply_adjustments(pic.copy(), ImageAdjustments(highlights=80))
    assert luma(up)[bright].mean() > luma(pic)[bright].mean() + 3


def test_sharpness_increases_detail():
    img = simple_page().image[100:700, 100:1100].copy()
    soft = cv2.GaussianBlur(img, (0, 0), 1.5)
    sharp = apply_adjustments(soft, ImageAdjustments(sharpness=150, sharpen_radius=2))
    lap = lambda a: cv2.Laplacian(luma(a), cv2.CV_64F).var()
    assert lap(sharp) > lap(soft) * 1.5


def test_denoise_reduces_noise(page):
    pic = region(page.image, pic_box(page))[:200, :300]
    rng = np.random.default_rng(0)
    noisy = np.clip(pic.astype(int) + rng.normal(0, 15, pic.shape), 0, 255).astype(np.uint8)
    out = apply_adjustments(noisy, ImageAdjustments(denoise=15))
    err = lambda a: np.abs(a.astype(float) - pic).mean()
    assert err(out) < err(noisy) * 0.7


def test_local_contrast_keeps_solid_black():
    img = simple_page().image.copy()
    img[1300:1500, 200:600] = 0                 # a solid black plate
    img[1300:1500, 700:1100] = 120              # a flat grey area
    out = apply_adjustments(img, ImageAdjustments(local_contrast=4))
    assert out[1320:1480, 220:580].max() <= 3
    assert not np.array_equal(out, img)


def test_illumination_flattens_paper_but_not_dark_picture(dark_page):
    img = dark_page.image
    shade = np.linspace(0.65, 1.0, img.shape[1], dtype=np.float32)[None, :, None]
    shaded = (img.astype(np.float32) * shade).astype(np.uint8)
    out = apply_adjustments(shaded, ImageAdjustments(illumination_correction=True))
    paper = paper_mask(img)
    left, right = paper.copy(), paper.copy()
    left[:, img.shape[1] // 3:] = False
    right[:, :2 * img.shape[1] // 3] = False
    gap_in = luma(shaded)[right].mean() - luma(shaded)[left].mean()
    gap_out = luma(out)[right].mean() - luma(out)[left].mean()
    assert gap_in > 40 and abs(gap_out) < 10
    assert luma(out)[paper].mean() > 240
    box = pic_box(dark_page)
    # the dark picture gets the gain of the paper around it, it is not whitened
    assert luma(region(out, box)).mean() < luma(region(img, box)).mean() + 25
    gain = illumination_gain(shaded.astype(np.float32) / 255)
    assert gain.shape == img.shape[:2] and gain.max() <= 2.2 + 1e-5


def test_grayscale_and_binarization_return_rgb(page):
    img = page.image
    for adj in (ImageAdjustments(grayscale=True), ImageAdjustments(binarization=Binarization.OTSU),
                ImageAdjustments(binarization=Binarization.SAUVOLA)):
        out = apply_adjustments(img, adj)
        assert out.shape == img.shape and out.dtype == np.uint8
        assert np.array_equal(out[..., 0], out[..., 1]) and np.array_equal(out[..., 1], out[..., 2])
    for mode in (Binarization.OTSU, Binarization.SAUVOLA):
        out = apply_adjustments(img, ImageAdjustments(binarization=mode))
        assert set(np.unique(out)) <= {0, 255}


def test_histogram(page):
    hist = histogram(page.image)
    assert set(hist) == {"R", "G", "B", "L"}
    n = page.image.shape[0] * page.image.shape[1]
    assert all(h.shape == (256,) and h.sum() == n for h in hist.values())


# --------------------------------------------------------------------------- local corrections

def _outside_equal(a, b, boxes_px):
    mask = np.ones(a.shape[:2], bool)
    for l, t, r, bt in boxes_px:
        mask[t:bt, l:r] = False
    return np.array_equal(a[mask], b[mask])


def test_local_only_changes_its_rectangle(page):
    img = page.image
    h, w = img.shape[:2]
    box = Box(x=0.1, y=0.3, width=0.5, height=0.3)
    loc = LocalAdjustment(box=box, adjustments=ImageAdjustments(brightness=-50, saturation=50), feather=0.1)
    out = apply_local_adjustments(img, [loc], AdjustmentTarget.PRINT)
    px = norm_to_px(box, w, h)
    assert _outside_equal(out, img, [px])
    inside = region(out, px).astype(int) - region(img, px)
    assert inside.min() < -20
    # feathered edge: the border row moves less than the centre
    l, t, r, b = px
    edge = np.abs(out[t, l:r].astype(int) - img[t, l:r]).mean()
    centre = np.abs(out[(t + b) // 2, l:r].astype(int) - img[(t + b) // 2, l:r]).mean()
    assert edge < centre


def test_local_target_filtering(page):
    img = page.image
    box = Box(x=0.1, y=0.1, width=0.3, height=0.3)
    ocr_only = LocalAdjustment(box=box, adjustments=ImageAdjustments(brightness=-50), target=AdjustmentTarget.OCR)
    disabled = LocalAdjustment(box=box, adjustments=ImageAdjustments(brightness=-50), enabled=False)
    assert apply_local_adjustments(img, [ocr_only, disabled], AdjustmentTarget.PRINT) is img
    both = LocalAdjustment(box=box, adjustments=ImageAdjustments(brightness=-50), target=AdjustmentTarget.BOTH)
    assert not np.array_equal(apply_local_adjustments(img, [both], AdjustmentTarget.OCR), img)


def test_restore_original(page):
    reference = page.image
    corrected = apply_adjustments(reference, ImageAdjustments(brightness=30, temperature=40))
    h, w = reference.shape[:2]
    box = Box(x=0.2, y=0.4, width=0.4, height=0.2)
    loc = LocalAdjustment(box=box, restore_original=True)
    out = apply_local_adjustments(corrected, [loc], AdjustmentTarget.PRINT, reference=reference)
    px = norm_to_px(box, w, h)
    assert np.array_equal(region(out, px), region(reference, px))
    assert _outside_equal(out, corrected, [px])
    with pytest.raises(ValueError):
        apply_local_adjustments(corrected, [loc], AdjustmentTarget.PRINT)


def test_local_saturation_on_illustration_leaves_text_alone(page):
    img = page.image
    h, w = img.shape[:2]
    l, t, r, b = pic_box(page)
    box = Box(x=l / w, y=t / h, width=(r - l) / w, height=(b - t) / h)
    loc = LocalAdjustment(box=box, adjustments=ImageAdjustments(saturation=80))
    out = apply_local_adjustments(img, [loc], AdjustmentTarget.PRINT)
    for block in page.of_kind("paragraph") + page.of_kind("caption") + page.of_kind("title"):
        assert np.array_equal(region(out, block.box), region(img, block.box))
    assert chroma(region(out, pic_box(page))) > chroma(region(img, pic_box(page))) * 1.2


def test_local_is_computed_on_the_region():
    # White balance measured on the region only: a neutral page with one yellow rectangle.
    img = simple_page().image.copy()
    img[1200:1600, 100:1100] = color_cast(img[1200:1600, 100:1100])
    h, w = img.shape[:2]
    box = Box(x=100 / w, y=1200 / h, width=1000 / w, height=400 / h)
    loc = LocalAdjustment(box=box, adjustments=ImageAdjustments(white_balance=WhiteBalance.WHITE_PATCH))
    out = apply_local_adjustments(img, [loc], AdjustmentTarget.PRINT)
    patch = out[1210:1590, 110:1090]
    paper = patch[luma(patch) > 200].mean(axis=0)
    assert paper.max() - paper.min() < 4


# --------------------------------------------------------------------------- memory

def test_memory_on_12_megapixels(page):
    big = cv2.resize(page.image, (3000, 4000), interpolation=cv2.INTER_LINEAR)
    adj = ImageAdjustments(white_balance=WhiteBalance.GRAY_WORLD, exposure=0.5, temperature=10,
                           shadows=30, highlights=-20, contrast=15, gamma=1.1, saturation=20,
                           illumination_correction=True, local_contrast=2, sharpness=80)
    tracemalloc.start()
    try:
        out = apply_adjustments(big, adj)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert out.shape == big.shape
    assert peak < 600 * 1024 * 1024, f"pic mémoire {peak / 1e6:.0f} Mo"
